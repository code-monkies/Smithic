"""SQLite ledger for runs, stages, and per-call cost accounting."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    target_path     TEXT NOT NULL,
    feature_seed    TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,    -- running | completed | aborted | budget_exceeded | error
    branch          TEXT,
    pr_url          TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS stages (
    run_id          TEXT NOT NULL REFERENCES runs(id),
    name            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,    -- running | completed | failed | skipped
    payload         TEXT,             -- stage-specific JSON output
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS cost_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    stage           TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    cost_usd        REAL NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    session_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id);
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Memory:
    """Thin DAO over SQLite. Connections are per-call and short-lived."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # --- runs ---

    def start_run(self, run_id: str, target_path: str, feature_seed: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, target_path, feature_seed, started_at, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (run_id, target_path, feature_seed, _utcnow()),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        branch: str | None = None,
        pr_url: str | None = None,
        notes: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, branch = ?, pr_url = ?, notes = ? "
                "WHERE id = ?",
                (_utcnow(), status, branch, pr_url, notes, run_id),
            )

    # --- stages ---

    def start_stage(self, run_id: str, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages (run_id, name, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, name, _utcnow()),
            )

    def finish_stage(
        self, run_id: str, name: str, status: str, payload: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE stages SET finished_at = ?, status = ?, payload = ? "
                "WHERE run_id = ? AND name = ?",
                (_utcnow(), status, payload, run_id, name),
            )

    # --- cost ---

    def record_cost(
        self,
        run_id: str,
        stage: str,
        cost_usd: float,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        session_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cost_events "
                "(run_id, stage, occurred_at, cost_usd, input_tokens, output_tokens, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, stage, _utcnow(), cost_usd, input_tokens, output_tokens, session_id),
            )

    def total_cost(self, run_id: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return float(row["total"])

    def total_tokens(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total "
                "FROM cost_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return int(row["total"])
