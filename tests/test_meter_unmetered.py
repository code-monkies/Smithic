"""Meter behavior under non-API auth modes (subscription / Bedrock / Vertex / Foundry).

The USD ceiling must be advisory in unmetered modes — only the token ceiling
is enforced. ``remaining_usd`` should return inf so the SDK doesn't see a value
it would try to enforce against $0 cost reports.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from smithic.budget.exceptions import BudgetExceeded
from smithic.budget.meter import BudgetCeiling, Meter
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


def test_unmetered_remaining_is_infinite(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=False)
    assert math.isinf(meter.remaining_usd())


def test_unmetered_does_not_raise_on_usd_breach(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=False, max_usd=1.0)
    meter.record("implement", 100.00)  # would obliterate the ceiling
    meter.check()  # must not raise


def test_unmetered_still_enforces_token_ceiling(tmp_path: Path) -> None:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run("rid", "/repo", "feat")
    meter = Meter(
        memory,
        "rid",
        BudgetCeiling(max_usd=1.0, max_tokens=10),
        enforce_usd=False,
    )
    meter.record("implement", 0.0, input_tokens=20, output_tokens=0)
    with pytest.raises(BudgetExceeded):
        meter.check()


def test_metered_remaining_decrements(tmp_path: Path) -> None:
    meter = _meter(tmp_path, enforce_usd=True, max_usd=10.0)
    meter.record("implement", 4.0)
    assert meter.remaining_usd() == pytest.approx(6.0)
