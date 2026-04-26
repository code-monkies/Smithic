"""SQLite ledger tests."""

from __future__ import annotations

from pathlib import Path

from smithic.budget.exceptions import BudgetExceeded
from smithic.budget.meter import BudgetCeiling, Meter
from smithic.memory.db import Memory


def _memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / ".smithic" / "smithic.db")


def test_full_run_lifecycle(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_run("rid", "/repo", "feat")
    memory.start_stage("rid", "introspect")
    memory.finish_stage("rid", "introspect", "completed")
    memory.record_cost("rid", "implement", 0.42, input_tokens=100, output_tokens=50)
    memory.finish_run("rid", "completed", branch="feat/x", pr_url="https://example/pr/1")

    assert memory.total_cost("rid") == 0.42
    assert memory.total_tokens("rid") == 150


def test_meter_blocks_when_ceiling_exceeded(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_run("rid", "/repo", "feat")

    meter = Meter(memory, "rid", BudgetCeiling(max_usd=1.00, max_tokens=1_000_000))
    meter.record("implement", 0.50)
    meter.check()  # under ceiling — fine

    meter.record("implement", 0.75)  # cumulative now 1.25
    try:
        meter.check()
    except BudgetExceeded as exc:
        assert exc.spent_usd > 1.0
    else:
        raise AssertionError("expected BudgetExceeded")


def test_remaining_usd_never_negative(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_run("rid", "/repo", "feat")
    meter = Meter(memory, "rid", BudgetCeiling(max_usd=1.00, max_tokens=1_000_000))
    meter.record("implement", 5.00)
    assert meter.remaining_usd() == 0.0
