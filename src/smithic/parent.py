"""Parent-run coordinator — fans out N child runs concurrently.

When the user passes ``--runs N`` (with N > 1), the CLI dispatches here
instead of straight to ``orchestrator.run_once``. The parent:

1. Records a ``parent_runs`` row so siblings can find each other.
2. Builds one shared ``ResearchCache`` so the second through Nth children
   reuse the synthesized findings the first child paid for.
3. Spawns N tasks under one ``anyio.task_group`` and collects their outcomes.
4. Catches each child's exceptions inside the child closure — one bad run
   never aborts the parent. ``RunOutcome(status="error", ...)`` is recorded
   for the failed child and siblings keep running.

A staggered start (200ms between launches) gives the first child time to seed
the cache before the rest hit the synthesis path. Optional but cheap and
materially reduces total cost for swarms of 3+.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import anyio

from smithic.config import SmithicConfig
from smithic.memory.cache import ResearchCache
from smithic.memory.db import Memory
from smithic.orchestrator import RunOutcome, run_once
from smithic.telemetry.logger import event
from smithic.worktree.naming import new_run_id

# Time between child launches. The first child seeds the research cache, so
# children 2..N skip the synthesis call. Tuned by feel — long enough to let
# the queries call return on warm cache, short enough that latency-bound
# users don't notice.
_STAGGER_SECONDS = 0.2


@dataclass(frozen=True)
class SwarmOutcome:
    parent_run_id: str
    n_runs: int
    outcomes: list[RunOutcome]
    total_cost_usd: float
    status: str  # completed | partial | error

    @property
    def successful(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if o.status == "completed"]

    @property
    def failed(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if o.status not in {"completed", "budget_exceeded"}]


def _resolve_status(outcomes: list[RunOutcome]) -> str:
    if not outcomes:
        return "error"
    completed = sum(1 for o in outcomes if o.status == "completed")
    if completed == len(outcomes):
        return "completed"
    if completed == 0:
        return "error"
    return "partial"


async def run_swarm(
    *,
    config: SmithicConfig,
    config_dir: Path,
    feature_seed: str | None,
    db_path: Path,
    n_runs: int,
    model: str | None = None,
    max_turns: int = 40,
) -> SwarmOutcome:
    """Run N children concurrently and aggregate their outcomes.

    Children are independent — one failing doesn't abort siblings. The shared
    ``ResearchCache`` lets later children skip synthesis when the first
    child's queries match.
    """
    if n_runs < 1:
        raise ValueError(f"n_runs must be >= 1 (got {n_runs})")

    target_path = config.target.resolve_path(config_dir)
    memory = Memory(db_path)
    parent_run_id = new_run_id()
    memory.start_parent_run(parent_run_id, str(target_path), n_runs)
    event(
        "parent.start",
        parent_run_id=parent_run_id,
        target=str(target_path),
        n_runs=n_runs,
    )

    # One cache shared by all children. Lives next to the SQLite ledger so
    # `smithic clean --cache` can wipe it without touching run history.
    cache_db = db_path.parent / "research_cache.db"
    cache = ResearchCache(cache_db)

    outcomes: list[RunOutcome] = []
    outcomes_lock = anyio.Lock()

    async def _child(index: int) -> None:
        # Stagger so the first child seeds the cache before siblings race
        # past the cache lookup.
        if index > 0:
            await anyio.sleep(_STAGGER_SECONDS * index)
        try:
            outcome = await run_once(
                config=config,
                config_dir=config_dir,
                feature_seed=feature_seed,
                db_path=db_path,
                model=model,
                max_turns=max_turns,
                research_only=False,
                parent_run_id=parent_run_id,
                cache=cache,
            )
        except Exception as exc:
            event(
                "parent.child_failed",
                parent_run_id=parent_run_id,
                index=index,
                error=repr(exc),
            )
            outcome = RunOutcome(
                run_id=f"{parent_run_id}-child-{index}",
                status="error",
                pr_url=None,
                branch=None,
                cost_usd=0.0,
                notes=f"child raised: {exc!r}",
                parent_run_id=parent_run_id,
            )
        async with outcomes_lock:
            outcomes.append(outcome)

    async with anyio.create_task_group() as tg:
        for i in range(n_runs):
            tg.start_soon(_child, i)

    status = _resolve_status(outcomes)
    notes = (
        f"{len(_successful(outcomes))}/{n_runs} succeeded"
        if status != "completed"
        else None
    )
    memory.finish_parent_run(parent_run_id, status, notes=notes)
    total = sum(o.cost_usd for o in outcomes)
    event(
        "parent.end",
        parent_run_id=parent_run_id,
        status=status,
        total_cost_usd=total,
        successful=len(_successful(outcomes)),
        failed=len(outcomes) - len(_successful(outcomes)),
    )
    return SwarmOutcome(
        parent_run_id=parent_run_id,
        n_runs=n_runs,
        outcomes=outcomes,
        total_cost_usd=total,
        status=status,
    )


def _successful(outcomes: list[RunOutcome]) -> list[RunOutcome]:
    return [o for o in outcomes if o.status == "completed"]
