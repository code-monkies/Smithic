"""WAL + concurrent writers — v0.3 schema and isolation tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from smithic.memory.db import Memory


def _memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / ".smithic" / "smithic.db")


def test_wal_journal_mode_active(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    with memory._connect() as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    # SQLite returns the active mode; we want WAL.
    assert row[0].lower() == "wal"


def test_parent_run_lifecycle(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_parent_run("parent-1", "/repo", n_runs=3)

    memory.start_run("child-1", "/repo", None, parent_run_id="parent-1")
    memory.start_run("child-2", "/repo", None, parent_run_id="parent-1")
    memory.set_selected_candidate("child-1", "Add /healthz")
    memory.set_selected_candidate("child-2", "Add tracing")

    siblings = memory.list_sibling_selections("parent-1")
    assert sorted(siblings) == ["Add /healthz", "Add tracing"]

    memory.finish_parent_run("parent-1", "completed")


def test_list_sibling_selections_filters_unselected(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_parent_run("p1", "/repo", n_runs=2)
    memory.start_run("a", "/repo", None, parent_run_id="p1")
    memory.start_run("b", "/repo", None, parent_run_id="p1")
    memory.set_selected_candidate("b", "Add tracing")

    siblings = memory.list_sibling_selections("p1")
    assert siblings == ["Add tracing"]


def test_concurrent_writers_no_lock_errors(tmp_path: Path) -> None:
    """10 threads each writing 20 cost events — WAL should keep them happy."""
    memory = _memory(tmp_path)
    memory.start_parent_run("p1", "/repo", n_runs=10)

    def _worker(i: int) -> None:
        run_id = f"r-{i}"
        memory.start_run(run_id, "/repo", None, parent_run_id="p1")
        for j in range(20):
            memory.record_cost(run_id, "implement", 0.001, input_tokens=j, output_tokens=1)
        memory.finish_run(run_id, "completed")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 runs × 20 events × 0.001 = 0.20
    totals = [memory.total_cost(f"r-{i}") for i in range(10)]
    assert all(abs(t - 0.020) < 1e-9 for t in totals)


def test_v01_to_v03_migration_is_safe(tmp_path: Path) -> None:
    """Open a Memory instance twice — second open must not re-create tables or
    crash on existing v0.2 columns."""
    memory1 = _memory(tmp_path)
    memory1.start_run("r1", "/repo", "feat")
    memory1.finish_run("r1", "completed")

    # Re-open against the same DB; migration must be idempotent.
    memory2 = Memory(tmp_path / ".smithic" / "smithic.db")
    memory2.start_run("r2", "/repo", None, parent_run_id=None)


def test_old_v01_db_gets_v03_columns(tmp_path: Path) -> None:
    """Hand-build a v0.1-shaped runs table; ensure Memory adds v02+v03 columns."""
    import sqlite3

    db_path = tmp_path / "old.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE runs ("
            "id TEXT PRIMARY KEY, target_path TEXT, feature_seed TEXT, "
            "started_at TEXT, finished_at TEXT, status TEXT, branch TEXT, "
            "pr_url TEXT, notes TEXT)"
        )
        conn.execute(
            "INSERT INTO runs VALUES "
            "('r-old', '/repo', 'feat', '2026-01-01', '2026-01-01', 'completed', "
            "'br', 'http://pr/1', 'ok')"
        )
        conn.commit()

    memory = Memory(db_path)
    # No exception means the ALTERs ran and the table now has v0.3 columns.
    memory.set_selected_candidate("r-old", "back-fill works")

    with memory._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "parent_run_id" in cols
    assert "selected_candidate_title" in cols
    assert "critic_verdict" in cols


def test_finish_parent_run_with_notes(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.start_parent_run("p", "/repo", n_runs=1)
    memory.finish_parent_run("p", "partial", notes="2/3 succeeded")

    with memory._connect() as conn:
        row = conn.execute("SELECT * FROM parent_runs WHERE id = ?", ("p",)).fetchone()
    assert row["status"] == "partial"
    assert row["notes"] == "2/3 succeeded"


@pytest.mark.skip(reason="not part of public API; covered indirectly via concurrent writers")
def test_busy_timeout_recovers_from_brief_lock(tmp_path: Path) -> None:
    pass
