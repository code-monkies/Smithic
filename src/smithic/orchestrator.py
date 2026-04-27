"""Linear stage runner — the spine of a Smithic run.

v0.2 sequence (full autonomous-ideation loop):

    introspect → research → score → spec → implement → critique → pr

When ``feature_seed`` is supplied (the v0.1 escape hatch via ``--feature``),
``research`` and ``score`` are skipped and the supplied feature flows
straight into ``spec``.

When the critic returns ``abort``, the run ends without a PR (worktree
preserved for inspection). When it returns ``revise``, the implement stage
runs once more with critic feedback prepended; second failure → abort. When
it returns ``pass-with-concerns``, the PR is opened as a draft with a
``smithic-needs-review`` label.

v0.3 adds two optional inputs that turn ``run_once`` into the per-child entry
point of a swarm: ``parent_run_id`` (for sibling-aware DB writes) and
``cache`` (for shared research findings across siblings). Both are ``None``
on a single-run invocation, in which case behavior is identical to v0.2.

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
from smithic.memory.cache import ResearchCache
from smithic.memory.db import Memory
from smithic.rubric.loader import load_rubric
from smithic.rubric.schema import Rubric
from smithic.stages.critique import CritiqueResult, run_critique
from smithic.stages.implement import ImplementResult, run_implementation
from smithic.stages.introspect import IntrospectionReport, introspect
from smithic.stages.pr import NEEDS_REVIEW_LABEL, compose_pr_body, open_pr
from smithic.stages.research import run_research
from smithic.stages.score import feature_from_selection, run_score, write_score_artifact
from smithic.stages.spec import write_spec
from smithic.telemetry.logger import event
from smithic.worktree.manager import Worktree, WorktreeManager
from smithic.worktree.naming import new_run_id


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str
    pr_url: str | None
    branch: str | None
    cost_usd: float
    notes: str
    parent_run_id: str | None = None


def _resolve_rubric(config: SmithicConfig, config_dir: Path) -> Rubric:
    if config.rubric.path is None:
        return load_rubric(None)
    path = (
        config.rubric.path
        if config.rubric.path.is_absolute()
        else (config_dir / config.rubric.path).resolve()
    )
    return load_rubric(path)


def _critic_label_for(verdict: str) -> list[str]:
    return [NEEDS_REVIEW_LABEL] if verdict == "pass-with-concerns" else []


async def _maybe_critique(
    *,
    enable: bool,
    spec_path: Path,
    worktree: Worktree,
    introspection: IntrospectionReport,
    meter: Meter,
    auth_env: dict[str, str],
    cli_path: str | None,
    model: str | None,
    run_id: str,
    memory: Memory,
) -> CritiqueResult | None:
    if not enable:
        return None
    memory.start_stage(run_id, "critique")
    event("stage.start", run_id=run_id, stage="critique")
    result = await run_critique(
        spec_path=spec_path,
        worktree_path=worktree.path,
        base_branch=worktree.base_branch,
        introspection=introspection,
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
    )
    memory.finish_stage(run_id, "critique", "completed", payload=result.verdict.verdict)
    memory.set_critic_verdict(run_id, result.verdict.verdict)
    event(
        "stage.end",
        run_id=run_id,
        stage="critique",
        status="completed",
        verdict=result.verdict.verdict,
    )
    return result


async def run_once(
    *,
    config: SmithicConfig,
    config_dir: Path,
    feature_seed: str | None,
    db_path: Path,
    model: str | None = None,
    max_turns: int = 40,
    research_only: bool = False,
    parent_run_id: str | None = None,
    cache: ResearchCache | None = None,
) -> RunOutcome:
    """Execute one Smithic run end-to-end.

    ``feature_seed`` is the user-supplied feature description. ``None`` triggers
    the autonomous research+score loop. ``research_only=True`` writes a brief
    to ``<target>/.smithic/`` and returns without creating a worktree.

    ``parent_run_id`` and ``cache`` are populated by the v0.3 swarm coordinator
    in ``smithic.parent``. ``parent_run_id`` is recorded on the run row so the
    diversity-nudge in the score stage can read sibling selections; ``cache``
    skips synthesis when a sibling already produced findings for the same
    target + query set.
    """
    target_path = config.target.resolve_path(config_dir)
    mission = config.target.resolve_mission(config_dir)

    memory = Memory(db_path)
    run_id = new_run_id()
    memory.start_run(
        run_id, str(target_path), feature_seed, parent_run_id=parent_run_id
    )
    event(
        "run.start",
        run_id=run_id,
        target=str(target_path),
        feature=feature_seed,
        parent_run_id=parent_run_id,
    )

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
    worktree: Worktree | None = None

    try:
        # --- introspect ---
        memory.start_stage(run_id, "introspect")
        event("stage.start", run_id=run_id, stage="introspect")
        report = introspect(target_path)
        memory.finish_stage(run_id, "introspect", "completed")
        event("stage.end", run_id=run_id, stage="introspect", status="completed")

        # --- research-only short-circuit ---
        if research_only:
            return await _run_research_only(
                config=config,
                target_path=target_path,
                mission=mission,
                report=report,
                meter=meter,
                memory=memory,
                run_id=run_id,
                auth_env=auth_env,
                cli_path=config.auth.cli_path,
                model=model,
                cache=cache,
                parent_run_id=parent_run_id,
            )

        # --- worktree (created early so research can write into it) ---
        memory.start_stage(run_id, "worktree")
        event("stage.start", run_id=run_id, stage="worktree")
        base_branch = report.git_default_branch or config.pr.base_branch
        if parent_run_id is None:
            worktree = wt_manager.create(run_id, feature_seed, base_branch=base_branch)
        else:
            worktree = await wt_manager.concurrent_create(
                run_id, feature_seed, base_branch=base_branch
            )
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

        feature, rationale = await _resolve_feature(
            config=config,
            config_dir=config_dir,
            feature_seed=feature_seed,
            mission=mission,
            report=report,
            meter=meter,
            memory=memory,
            run_id=run_id,
            auth_env=auth_env,
            cli_path=config.auth.cli_path,
            model=model,
            worktree=worktree,
            target_path=target_path,
            cache=cache,
            parent_run_id=parent_run_id,
        )
        meter.check()

        # --- spec ---
        memory.start_stage(run_id, "spec")
        event("stage.start", run_id=run_id, stage="spec")
        spec_path = write_spec(
            worktree_path=worktree.path,
            feature=feature,
            mission=mission,
            introspection=report,
            run_id=run_id,
            rationale=rationale,
        )
        memory.finish_stage(run_id, "spec", "completed", payload=str(spec_path))
        event("stage.end", run_id=run_id, stage="spec", status="completed")

        # --- implement (with optional revise loop driven by the critic) ---
        impl_result = await _run_implement(
            worktree=worktree,
            feature=feature,
            meter=meter,
            model=model,
            max_turns=max_turns,
            auth_env=auth_env,
            cli_path=config.auth.cli_path,
            memory=memory,
            run_id=run_id,
            revise_feedback=None,
        )
        meter.check()

        if not impl_result.succeeded:
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
                parent_run_id=parent_run_id,
            )

        # --- critique (with at most max_revise_loops retries) ---
        critique_result: CritiqueResult | None = None
        revise_loops = 0
        while True:
            critique_result = await _maybe_critique(
                enable=config.critique.enable,
                spec_path=spec_path,
                worktree=worktree,
                introspection=report,
                meter=meter,
                auth_env=auth_env,
                cli_path=config.auth.cli_path,
                model=config.critique.model or model,
                run_id=run_id,
                memory=memory,
            )
            if critique_result is None:
                break  # critic disabled
            verdict = critique_result.verdict.verdict
            if verdict in {"pass", "pass-with-concerns"}:
                break
            if verdict == "abort":
                memory.finish_run(
                    run_id,
                    "aborted",
                    branch=worktree.branch,
                    notes=critique_result.verdict.summary,
                )
                event(
                    "run.end",
                    run_id=run_id,
                    status="aborted",
                    notes=critique_result.verdict.summary,
                )
                return RunOutcome(
                    run_id=run_id,
                    status="aborted",
                    pr_url=None,
                    branch=worktree.branch,
                    cost_usd=meter.spent(),
                    notes=critique_result.verdict.summary,
                    parent_run_id=parent_run_id,
                )
            # verdict == "revise"
            if revise_loops >= config.critique.max_revise_loops:
                memory.finish_run(
                    run_id,
                    "aborted",
                    branch=worktree.branch,
                    notes="critic still unhappy after revise loop limit",
                )
                event("run.end", run_id=run_id, status="aborted", notes="revise loop exhausted")
                return RunOutcome(
                    run_id=run_id,
                    status="aborted",
                    pr_url=None,
                    branch=worktree.branch,
                    cost_usd=meter.spent(),
                    notes="critic still unhappy after revise loop limit",
                    parent_run_id=parent_run_id,
                )
            revise_loops += 1
            event("revise.loop.begin", run_id=run_id, loop=revise_loops)
            impl_result = await _run_implement(
                worktree=worktree,
                feature=feature,
                meter=meter,
                model=model,
                max_turns=max_turns,
                auth_env=auth_env,
                cli_path=config.auth.cli_path,
                memory=memory,
                run_id=run_id,
                revise_feedback=critique_result.verdict.as_revise_feedback(),
            )
            meter.check()
            if not impl_result.succeeded:
                memory.finish_run(
                    run_id,
                    "error",
                    branch=worktree.branch,
                    notes="revise loop produced no usable output",
                )
                return RunOutcome(
                    run_id=run_id,
                    status="error",
                    pr_url=None,
                    branch=worktree.branch,
                    cost_usd=meter.spent(),
                    notes="revise loop produced no output",
                    parent_run_id=parent_run_id,
                )

        # --- pr ---
        draft = bool(critique_result and critique_result.verdict.verdict == "pass-with-concerns")
        extra_labels = (
            _critic_label_for(critique_result.verdict.verdict) if critique_result else []
        )

        memory.start_stage(run_id, "pr")
        event("stage.start", run_id=run_id, stage="pr")
        title = f"feat: {feature.strip().rstrip('.')}"[:72]
        body = compose_pr_body(
            feature=feature,
            mission_excerpt=_first_paragraph(mission),
            impl_summary=impl_result.summary,
            cost_usd=meter.spent(),
            run_id=run_id,
            critic_summary=(critique_result.verdict.summary if critique_result else None),
            rationale=rationale,
        )
        pr = open_pr(
            worktree=worktree,
            title=title,
            body=body,
            pr_config=config.pr,
            draft=draft,
            extra_labels=extra_labels,
        )
        memory.finish_stage(run_id, "pr", "completed", payload=pr.url)
        event(
            "stage.end",
            run_id=run_id,
            stage="pr",
            status="completed",
            url=pr.url,
            draft=draft,
        )

        memory.finish_run(run_id, "completed", branch=worktree.branch, pr_url=pr.url)
        event("run.end", run_id=run_id, status="completed", url=pr.url)
        return RunOutcome(
            run_id=run_id,
            status="completed",
            pr_url=pr.url,
            branch=worktree.branch,
            cost_usd=meter.spent(),
            notes="ok",
            parent_run_id=parent_run_id,
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
            parent_run_id=parent_run_id,
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
            parent_run_id=parent_run_id,
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


async def _resolve_feature(
    *,
    config: SmithicConfig,
    config_dir: Path,
    feature_seed: str | None,
    mission: str,
    report: IntrospectionReport,
    meter: Meter,
    memory: Memory,
    run_id: str,
    auth_env: dict[str, str],
    cli_path: str | None,
    model: str | None,
    worktree: Worktree,
    target_path: Path,
    cache: ResearchCache | None = None,
    parent_run_id: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(feature, rationale)``.

    If ``feature_seed`` is supplied, ``rationale`` is ``None`` and research+score
    are skipped. Otherwise we run research → score and the rationale embeds the
    scoring breakdown into the spec. Research artifacts are written into the
    *worktree* so swarm siblings don't clobber each other in the target dir.
    """
    if feature_seed is not None:
        return feature_seed, None

    # --- research ---
    memory.start_stage(run_id, "research")
    event("stage.start", run_id=run_id, stage="research")
    out_dir = worktree.path / ".smithic"
    research_result = await run_research(
        mission=mission,
        introspection=report,
        research_cfg=config.research,
        out_dir=out_dir,
        run_id=run_id,
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
        cache=cache,
        target_path=target_path,
    )
    memory.set_research_brief_path(run_id, str(research_result.brief_path))
    memory.finish_stage(
        run_id,
        "research",
        "completed",
        payload=str(research_result.brief_path),
    )
    event(
        "stage.end",
        run_id=run_id,
        stage="research",
        status="completed",
        candidates=len(research_result.findings.candidates),
        cache_hit=research_result.cache_hit,
    )

    # --- score ---
    memory.start_stage(run_id, "score")
    event("stage.start", run_id=run_id, stage="score")
    rubric = _resolve_rubric(config, config_dir)
    previously_selected: list[str] = []
    if parent_run_id is not None:
        previously_selected = memory.list_sibling_selections(parent_run_id)
    score_result = await run_score(
        findings=research_result.findings,
        rubric=rubric,
        introspection=report,
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
        previously_selected=previously_selected,
    )
    write_score_artifact(score_result.scoring, out_dir)
    if score_result.scoring.selected is None:
        memory.finish_stage(
            run_id,
            "score",
            "completed",
            payload=score_result.scoring.abort_reason or "no candidate cleared thresholds",
        )
        event(
            "stage.end",
            run_id=run_id,
            stage="score",
            status="completed",
            selected=None,
        )
        raise AbortRun(score_result.scoring.abort_reason or "no candidate cleared thresholds")

    feature, rationale = feature_from_selection(score_result.scoring.selected)
    memory.set_selected_candidate(run_id, feature)
    memory.finish_stage(run_id, "score", "completed", payload=feature)
    event(
        "stage.end",
        run_id=run_id,
        stage="score",
        status="completed",
        selected=feature,
    )
    return feature, rationale


async def _run_research_only(
    *,
    config: SmithicConfig,
    target_path: Path,
    mission: str,
    report: IntrospectionReport,
    meter: Meter,
    memory: Memory,
    run_id: str,
    auth_env: dict[str, str],
    cli_path: str | None,
    model: str | None,
    cache: ResearchCache | None = None,
    parent_run_id: str | None = None,
) -> RunOutcome:
    """Probe variant: write a research brief to ``<target>/.smithic/`` and stop.

    No worktree, no spec, no implement, no PR. Cheap way to inspect what the
    autonomous loop *would* propose before committing to a build. Per-run
    suffix on the brief filename so multiple probes don't clobber each other.
    """
    memory.start_stage(run_id, "research")
    event("stage.start", run_id=run_id, stage="research")
    out_dir = target_path / ".smithic"
    research_result = await run_research(
        mission=mission,
        introspection=report,
        research_cfg=config.research,
        out_dir=out_dir,
        run_id=run_id,
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
        cache=cache,
        target_path=target_path,
    )
    # Rename the brief so multiple research-only probes don't clobber each other.
    final_brief = out_dir / f"research-{run_id}.md"
    research_result.brief_path.rename(final_brief)
    memory.set_research_brief_path(run_id, str(final_brief))
    memory.finish_stage(run_id, "research", "completed", payload=str(final_brief))
    memory.finish_run(run_id, "completed", notes=f"research-only brief at {final_brief}")
    event("stage.end", run_id=run_id, stage="research", status="completed")
    event("run.end", run_id=run_id, status="completed", brief=str(final_brief))
    return RunOutcome(
        run_id=run_id,
        status="completed",
        pr_url=None,
        branch=None,
        cost_usd=meter.spent(),
        notes=f"research brief at {final_brief}",
        parent_run_id=parent_run_id,
    )


async def _run_implement(
    *,
    worktree: Worktree,
    feature: str,
    meter: Meter,
    model: str | None,
    max_turns: int,
    auth_env: dict[str, str],
    cli_path: str | None,
    memory: Memory,
    run_id: str,
    revise_feedback: str | None,
) -> ImplementResult:
    stage_label = "implement.revise" if revise_feedback else "implement"
    memory.start_stage(run_id, stage_label)
    event("stage.start", run_id=run_id, stage=stage_label)
    result = await run_implementation(
        worktree_path=worktree.path,
        feature=feature,
        meter=meter,
        model=model,
        max_turns=max_turns,
        auth_env=auth_env,
        cli_path=cli_path,
        revise_feedback=revise_feedback,
    )
    impl_status = "completed" if result.succeeded else "failed"
    memory.finish_stage(run_id, stage_label, impl_status, payload=result.summary[:8000])
    event(
        "stage.end",
        run_id=run_id,
        stage=stage_label,
        status=impl_status,
        cost_usd=result.cost_usd,
    )
    return result


def _first_paragraph(text: str, *, max_chars: int = 600) -> str:
    para = text.strip().split("\n\n", 1)[0]
    return para if len(para) <= max_chars else para[:max_chars] + "..."
