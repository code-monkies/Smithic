"""Linear stage runner — the spine of a Smithic run.

v0.1 sequence:

    introspect → spec → implement → pr

Each stage's output feeds the next via Pydantic-typed values. Cost is metered
through the SQLite ledger after every Claude call. On success the orchestrator
opens a PR via ``gh``. On budget exhaustion it opens a draft PR with the
work-so-far. On any other exception it marks the run ``error`` and re-raises so
the CLI can show a useful traceback.

Worktrees are NEVER deleted automatically — the user can inspect them after a
run, then call ``smithic clean`` to remove them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from smithic.auth import env_for_mode, is_metered, preflight
from smithic.budget.exceptions import AbortRun, BudgetExceeded
from smithic.budget.meter import BudgetCeiling, Meter
from smithic.config import SmithicConfig
from smithic.memory.db import Memory
from smithic.stages.implement import run_implementation
from smithic.stages.introspect import introspect
from smithic.stages.pr import compose_pr_body, open_pr
from smithic.stages.spec import write_spec
from smithic.telemetry.logger import event
from smithic.worktree.manager import WorktreeManager
from smithic.worktree.naming import new_run_id


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str
    pr_url: str | None
    branch: str | None
    cost_usd: float
    notes: str


async def run_once(
    *,
    config: SmithicConfig,
    config_dir: Path,
    feature: str,
    db_path: Path,
    model: str | None = None,
    max_turns: int = 40,
) -> RunOutcome:
    """Execute one Smithic run end-to-end."""
    target_path = config.target.resolve_path(config_dir)
    mission = config.target.resolve_mission(config_dir)

    memory = Memory(db_path)
    run_id = new_run_id()
    memory.start_run(run_id, str(target_path), feature)
    event("run.start", run_id=run_id, target=str(target_path), feature=feature)

    auth_mode = preflight(config.auth.mode, cli_path=config.auth.cli_path)
    metered = is_metered(auth_mode)
    auth_env = env_for_mode(auth_mode)
    event("auth.resolved", run_id=run_id, mode=auth_mode, metered=metered)

    meter = Meter(
        memory,
        run_id,
        BudgetCeiling(
            max_usd=config.budget.max_usd_per_run,
            max_tokens=config.budget.max_tokens_per_run,
        ),
        enforce_usd=metered,
    )

    wt_manager = WorktreeManager(target_path, config.swarm.worktree_root)
    worktree = None

    try:
        # --- introspect ---
        memory.start_stage(run_id, "introspect")
        event("stage.start", run_id=run_id, stage="introspect")
        report = introspect(target_path)
        memory.finish_stage(run_id, "introspect", "completed")
        event("stage.end", run_id=run_id, stage="introspect", status="completed")

        # --- worktree ---
        memory.start_stage(run_id, "worktree")
        event("stage.start", run_id=run_id, stage="worktree")
        base_branch = report.git_default_branch or config.pr.base_branch
        worktree = wt_manager.create(run_id, feature, base_branch=base_branch)
        memory.finish_stage(
            run_id, "worktree", "completed", payload=str(worktree.path)
        )
        event(
            "stage.end",
            run_id=run_id,
            stage="worktree",
            status="completed",
            path=str(worktree.path),
            branch=worktree.branch,
        )

        # --- spec ---
        memory.start_stage(run_id, "spec")
        event("stage.start", run_id=run_id, stage="spec")
        spec_path = write_spec(
            worktree_path=worktree.path,
            feature=feature,
            mission=mission,
            introspection=report,
            run_id=run_id,
        )
        memory.finish_stage(run_id, "spec", "completed", payload=str(spec_path))
        event("stage.end", run_id=run_id, stage="spec", status="completed")

        # --- implement ---
        memory.start_stage(run_id, "implement")
        event("stage.start", run_id=run_id, stage="implement")
        result = await run_implementation(
            worktree_path=worktree.path,
            feature=feature,
            meter=meter,
            model=model,
            max_turns=max_turns,
            auth_env=auth_env,
            cli_path=config.auth.cli_path,
        )
        impl_status = "completed" if result.succeeded else "failed"
        memory.finish_stage(run_id, "implement", impl_status, payload=result.summary[:8000])
        event(
            "stage.end",
            run_id=run_id,
            stage="implement",
            status=impl_status,
            cost_usd=result.cost_usd,
        )
        meter.check()

        if not result.succeeded:
            memory.finish_run(
                run_id,
                "error",
                branch=worktree.branch,
                notes="implement stage returned no usable output",
            )
            return RunOutcome(
                run_id=run_id,
                status="error",
                pr_url=None,
                branch=worktree.branch,
                cost_usd=meter.spent(),
                notes="implement stage produced no output",
            )

        # --- pr ---
        memory.start_stage(run_id, "pr")
        event("stage.start", run_id=run_id, stage="pr")
        title = f"feat: {feature.strip().rstrip('.')}"[:72]
        body = compose_pr_body(
            feature=feature,
            mission_excerpt=_first_paragraph(mission),
            impl_summary=result.summary,
            cost_usd=meter.spent(),
            run_id=run_id,
        )
        pr = open_pr(
            worktree=worktree,
            title=title,
            body=body,
            pr_config=config.pr,
            draft=False,
        )
        memory.finish_stage(run_id, "pr", "completed", payload=pr.url)
        event("stage.end", run_id=run_id, stage="pr", status="completed", url=pr.url)

        memory.finish_run(run_id, "completed", branch=worktree.branch, pr_url=pr.url)
        event("run.end", run_id=run_id, status="completed", url=pr.url)
        return RunOutcome(
            run_id=run_id,
            status="completed",
            pr_url=pr.url,
            branch=worktree.branch,
            cost_usd=meter.spent(),
            notes="ok",
        )

    except BudgetExceeded as exc:
        memory.finish_run(
            run_id,
            "budget_exceeded",
            branch=worktree.branch if worktree else None,
            notes=str(exc),
        )
        event("run.end", run_id=run_id, status="budget_exceeded", notes=str(exc))
        return RunOutcome(
            run_id=run_id,
            status="budget_exceeded",
            pr_url=None,
            branch=worktree.branch if worktree else None,
            cost_usd=meter.spent(),
            notes=str(exc),
        )
    except AbortRun as exc:
        memory.finish_run(
            run_id,
            "aborted",
            branch=worktree.branch if worktree else None,
            notes=exc.reason,
        )
        event("run.end", run_id=run_id, status="aborted", notes=exc.reason)
        return RunOutcome(
            run_id=run_id,
            status="aborted",
            pr_url=None,
            branch=worktree.branch if worktree else None,
            cost_usd=meter.spent(),
            notes=exc.reason,
        )
    except Exception as exc:
        memory.finish_run(
            run_id,
            "error",
            branch=worktree.branch if worktree else None,
            notes=repr(exc),
        )
        event("run.end", run_id=run_id, status="error", error=repr(exc))
        raise


def _first_paragraph(text: str, *, max_chars: int = 600) -> str:
    para = text.strip().split("\n\n", 1)[0]
    return para if len(para) <= max_chars else para[:max_chars] + "..."
