"""Parent coordinator tests — concurrent child execution + failure isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import smithic.parent as parent_module
from smithic.config import (
    AuthConfig,
    BudgetConfig,
    CritiqueConfig,
    PRConfig,
    ResearchConfig,
    RubricConfig,
    SmithicConfig,
    SwarmConfig,
    TargetConfig,
)
from smithic.orchestrator import RunOutcome
from smithic.parent import SwarmOutcome, run_swarm


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed up tests — the production stagger is for the cache, not correctness."""
    monkeypatch.setattr(parent_module, "_STAGGER_SECONDS", 0.0)


def _config(target: Path) -> SmithicConfig:
    return SmithicConfig(
        target=TargetConfig(path=target, mission_text="Test mission."),
        swarm=SwarmConfig(parallel_runs=3),
        budget=BudgetConfig(),
        auth=AuthConfig(mode="api"),
        research=ResearchConfig(),
        rubric=RubricConfig(),
        critique=CritiqueConfig(),
        pr=PRConfig(),
    )


def _outcome(run_id: str, status: str = "completed", cost: float = 0.10) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        status=status,
        pr_url=f"https://github.com/x/y/pull/{run_id}" if status == "completed" else None,
        branch=f"smithic/{run_id}",
        cost_usd=cost,
        notes="ok" if status == "completed" else "failed",
        parent_run_id="parent-x",
    )


@pytest.mark.anyio("asyncio")
async def test_run_swarm_spawns_n_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    call_count = {"n": 0}
    started_at: list[float] = []

    async def fake_run_once(**kwargs):
        import time

        started_at.append(time.perf_counter())
        call_count["n"] += 1
        await asyncio.sleep(0.05)
        return _outcome(f"child-{call_count['n']}")

    monkeypatch.setattr("smithic.parent.run_once", fake_run_once)

    result = await run_swarm(
        config=_config(target),
        config_dir=target,
        feature_seed=None,
        db_path=tmp_path / "smithic.db",
        n_runs=3,
    )

    assert isinstance(result, SwarmOutcome)
    assert result.status == "completed"
    assert result.n_runs == 3
    assert len(result.outcomes) == 3
    # Children should have started near-simultaneously (stagger off in tests).
    assert max(started_at) - min(started_at) < 0.04


@pytest.mark.anyio("asyncio")
async def test_one_failing_child_does_not_kill_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    call_count = {"n": 0}

    async def fake_run_once(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated child crash")
        return _outcome(f"child-{call_count['n']}")

    monkeypatch.setattr("smithic.parent.run_once", fake_run_once)

    result = await run_swarm(
        config=_config(target),
        config_dir=target,
        feature_seed=None,
        db_path=tmp_path / "smithic.db",
        n_runs=3,
    )
    assert result.status == "partial"
    assert len(result.outcomes) == 3
    assert len(result.successful) == 2
    assert len(result.failed) == 1
    assert "simulated child crash" in result.failed[0].notes


@pytest.mark.anyio("asyncio")
async def test_all_failing_returns_error_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    async def fake_run_once(**kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("smithic.parent.run_once", fake_run_once)

    result = await run_swarm(
        config=_config(target),
        config_dir=target,
        feature_seed=None,
        db_path=tmp_path / "smithic.db",
        n_runs=2,
    )
    assert result.status == "error"
    assert all(o.status == "error" for o in result.outcomes)


@pytest.mark.anyio("asyncio")
async def test_parent_passes_parent_run_id_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    captured: list[dict] = []

    async def fake_run_once(**kwargs):
        captured.append(kwargs)
        return _outcome("child")

    monkeypatch.setattr("smithic.parent.run_once", fake_run_once)

    result = await run_swarm(
        config=_config(target),
        config_dir=target,
        feature_seed=None,
        db_path=tmp_path / "smithic.db",
        n_runs=2,
    )

    assert all(c["parent_run_id"] == result.parent_run_id for c in captured)
    cache = captured[0]["cache"]
    assert cache is not None
    # Same cache instance shared across children.
    assert all(c["cache"] is cache for c in captured)


@pytest.mark.anyio("asyncio")
async def test_total_cost_is_aggregated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    async def fake_run_once(**kwargs):
        return _outcome("c", cost=0.30)

    monkeypatch.setattr("smithic.parent.run_once", fake_run_once)

    result = await run_swarm(
        config=_config(target),
        config_dir=target,
        feature_seed=None,
        db_path=tmp_path / "smithic.db",
        n_runs=3,
    )
    assert abs(result.total_cost_usd - 0.90) < 1e-9


@pytest.mark.anyio("asyncio")
async def test_n_runs_zero_raises(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="n_runs must be >= 1"):
        await run_swarm(
            config=_config(target),
            config_dir=target,
            feature_seed=None,
            db_path=tmp_path / "smithic.db",
            n_runs=0,
        )
