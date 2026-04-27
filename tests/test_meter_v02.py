"""v0.2 meter additions: ``would_exceed`` and ``snapshot``."""

from __future__ import annotations

from pathlib import Path

from smithic.budget.meter import BudgetCeiling, Meter, MeterSnapshot
from smithic.memory.db import Memory


def _meter(tmp_path: Path, *, enforce_usd: bool, max_usd: float = 1.0) -> Meter:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run("rid", "/repo", "feat")
    return Meter(
        memory,
        "rid",
        BudgetCeiling(max_usd=max_usd, max_tokens=1_000_000),
        enforce_usd=enforce_usd,
    )


def test_would_exceed_metered(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=True, max_usd=1.0)
    meter.record("research", 0.80)
    assert meter.would_exceed(0.30) is True
    assert meter.would_exceed(0.10) is False


def test_would_exceed_always_false_when_unmetered(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=False, max_usd=1.0)
    meter.record("research", 100.0)
    assert meter.would_exceed(1_000_000.0) is False


def test_snapshot_is_frozen(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=True, max_usd=1.0)
    meter.record("research", 0.10, input_tokens=20, output_tokens=10)
    snap = meter.snapshot()
    assert isinstance(snap, MeterSnapshot)
    assert snap.spent_usd == 0.10
    assert snap.tokens_used == 30
    assert snap.remaining_usd == 0.90
    assert snap.enforce_usd is True
