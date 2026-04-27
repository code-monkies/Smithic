"""SQLite ledger for runs, stages, and per-call cost accounting."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Tables: created on first open. Indexes are created separately *after*
# migrations run, because an index on a v0.3 column would fail on a v0.1 DB
# whose runs table doesn't have the column yet.
_TABLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS parent_runs (
    id              TEXT PRIMARY KEY,
    target_path     TEXT NOT NULL,
    n_runs          INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,    -- running | completed | partial | error
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id                          TEXT PRIMARY KEY,
    target_path                 TEXT NOT NULL,
    feature_seed                TEXT,
    started_at                  TEXT NOT NULL,
    finished_at                 TEXT,
    status                      TEXT NOT NULL,
    branch                      TEXT,
    pr_url                      TEXT,
    notes                       TEXT,
    research_brief_path         TEXT,
    selected_candidate_title    TEXT,
    critic_verdict              TEXT,
    parent_run_id               TEXT REFERENCES parent_runs(id)
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
"""

_INDEXES_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_cost_events_run ON cost_events(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
"""

# v0.2 added three columns to ``runs``. Existing v0.1 databases need them
# back-filled — sqlite has no ``ADD COLUMN IF NOT EXISTS`` so we read the
# current schema and only run ALTERs for missing columns.
_RUNS_V02_COLUMNS: tuple[tuple[str, str], ...] = (
    ("research_brief_path", "TEXT"),
    ("selected_candidate_title", "TEXT"),
    ("critic_verdict", "TEXT"),
)

# v0.3 added swarm support: parent_run_id on runs, plus the parent_runs table
# (which CREATE TABLE IF NOT EXISTS handles on its own).
_RUNS_V03_COLUMNS: tuple[tuple[str, str], ...] = (
    ("parent_run_id", "TEXT"),
)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Memory:
    """Thin DAO over SQLite. Connections are per-call and short-lived.

    Concurrency: in v0.3 we run multiple child runs in parallel against the
    same DB. WAL mode allows concurrent readers + one writer; ``busy_timeout``
    backs off on contention so a contended write doesn't immediately raise
    ``database is locked``.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # journal_mode is persistent — set once on first connect.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_TABLES_SCHEMA)
            self._migrate_v02(conn)
            self._migrate_v03(conn)
            # Indexes go *after* migrations so a v0.1 DB has the column an
            # index references by the time the CREATE INDEX runs.
            conn.executescript(_INDEXES_SCHEMA)

    @staticmethod
    def _migrate_v02(conn: sqlite3.Connection) -> None:
        """Back-fill v0.2 columns on a v0.1 ``runs`` table."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        for column, type_ in _RUNS_V02_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {type_}")

    @staticmethod
    def _migrate_v03(conn: sqlite3.Connection) -> None:
        """Back-fill v0.3 columns on a v0.2 ``runs`` table."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        for column, type_ in _RUNS_V03_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {type_}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # busy_timeout is per-connection, not persistent — set on every open.
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    # --- parent runs ---

    def start_parent_run(self, parent_id: str, target_path: str, n_runs: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO parent_runs (id, target_path, n_runs, started_at, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (parent_id, target_path, n_runs, _utcnow()),
            )

    def finish_parent_run(
        self,
        parent_id: str,
        status: str,
        *,
        notes: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE parent_runs SET finished_at = ?, status = ?, notes = ? WHERE id = ?",
                (_utcnow(), status, notes, parent_id),
            )

    def list_sibling_selections(self, parent_id: str) -> list[str]:
        """Return titles selected by sibling runs under this parent so far.

        Used by the diversity-nudge in v0.3's score stage. Excludes nulls
        (runs that haven't reached score yet, or aborted before selecting).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT selected_candidate_title FROM runs "
                "WHERE parent_run_id = ? AND selected_candidate_title IS NOT NULL",
                (parent_id,),
            ).fetchall()
        return [row["selected_candidate_title"] for row in rows]

    # --- runs ---

    def start_run(
        self,
        run_id: str,
        target_path: str,
        feature_seed: str | None,
        *,
        parent_run_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, target_path, feature_seed, started_at, status, parent_run_id) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, target_path, feature_seed, _utcnow(), parent_run_id),
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

    def finalize_if_running(
        self,
        run_id: str,
        status: str,
        *,
        notes: str | None = None,
    ) -> bool:
        """Atomically transition the run to ``status`` only if still ``running``.

        Belt-and-suspenders against the ``running``-row leak: process killed,
        ``KeyboardInterrupt``, ``BaseException`` paths that bypass
        ``run_once``'s ``except Exception``. Called from a ``finally`` block;
        no-op when the run already reached a terminal status. Returns True
        if this call performed the transition.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, notes = ? "
                "WHERE id = ? AND status = 'running'",
                (_utcnow(), status, notes, run_id),
            )
            return cur.rowcount > 0

    def set_research_brief_path(self, run_id: str, path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET research_brief_path = ? WHERE id = ?",
                (path, run_id),
            )

    def set_selected_candidate(self, run_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET selected_candidate_title = ? WHERE id = ?",
                (title, run_id),
            )

    def set_critic_verdict(self, run_id: str, verdict: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET critic_verdict = ? WHERE id = ?",
                (verdict, run_id),
            )

    # --- stages ---

    def start_stage(self, run_id: str, name: str) -> None:
        # Idempotent: a revise loop re-runs critique/implement, so the same
        # (run_id, name) pair can be started more than once. We upsert to keep
        # the latest start time without losing the row's existing payload.
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages (run_id, name, started_at, status) "
                "VALUES (?, ?, ?, 'running') "
                "ON CONFLICT(run_id, name) DO UPDATE SET "
                "started_at = excluded.started_at, status = 'running', "
                "finished_at = NULL",
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
