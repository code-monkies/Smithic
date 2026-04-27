"""Concurrent worktree creation — the v0.3 lock serializes the git pair."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import smithic.worktree.manager as wt_module
from smithic.worktree.manager import Worktree, WorktreeManager


@pytest.fixture(autouse=True)
def _clear_lock_table() -> None:
    """Each test gets a clean lock table — anyio.Lock is bound to its event
    loop, so reusing one across tests would crash the second test."""
    wt_module._TARGET_LOCKS.clear()


def _slow_create(sleep_s: float = 0.08):
    """Build a sync ``create`` replacement that records its run interval."""
    intervals: list[tuple[float, float]] = []

    def fake_create(self, run_id, feature, base_branch="main"):
        start = time.perf_counter()
        time.sleep(sleep_s)
        end = time.perf_counter()
        intervals.append((start, end))
        return Worktree(
            path=(self.root / run_id).resolve(),
            branch=f"smithic/{run_id}",
            base_branch=base_branch,
        )

    return fake_create, intervals


def _max_overlap(intervals: list[tuple[float, float]]) -> int:
    """How many of the recorded intervals overlap each other at any point."""
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    active = 0
    peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    return peak


@pytest.mark.anyio("asyncio")
async def test_concurrent_create_serializes_same_target(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)

    manager_a = WorktreeManager(target)
    manager_b = WorktreeManager(target)

    fake_create, intervals = _slow_create(sleep_s=0.05)
    with patch.object(WorktreeManager, "create", fake_create):
        await asyncio.gather(
            manager_a.concurrent_create("r1", "f1"),
            manager_b.concurrent_create("r2", "f2"),
            manager_a.concurrent_create("r3", "f3"),
        )

    assert len(intervals) == 3
    assert _max_overlap(intervals) == 1


@pytest.mark.anyio("asyncio")
async def test_concurrent_create_uses_separate_locks_per_target(tmp_path: Path) -> None:
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    (target_a / ".git").mkdir(parents=True)
    (target_b / ".git").mkdir(parents=True)

    manager_a = WorktreeManager(target_a)
    manager_b = WorktreeManager(target_b)

    fake_create, intervals = _slow_create(sleep_s=0.10)
    with patch.object(WorktreeManager, "create", fake_create):
        await asyncio.gather(
            manager_a.concurrent_create("r1", "f1"),
            manager_b.concurrent_create("r2", "f2"),
        )

    # Different targets → different locks → both intervals overlap.
    assert _max_overlap(intervals) == 2
