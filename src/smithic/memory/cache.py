"""Research cache — keyed by ``(target_path, normalized_query_set)``.

When a parent invocation spawns N children, the second through Nth share the
same target and benefit from caching the synthesized ``ResearchFindings`` the
first child produced. v0.3 caches at *findings* granularity (one row per
query set) rather than per-source-call: in v0.2 the per-source MCP queries
happen *inside* the Claude synthesis subagent, so we don't see them from
Python. Findings-level caching is the natural seam.

Storage is a SQLite table next to the run ledger. No external deps — keeps
Smithic Windows-friendly. Embeddings + similarity lookup are an explicit
non-goal in v0.3 (would require shipping a 200MB ST model or a service).

Cache hits respect the user's ``[research].cache_ttl_hours``. Writes are
best-effort: failures warn (via the telemetry logger) but never abort a run.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smithic.types.research import ResearchFindings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_cache (
    cache_key       TEXT PRIMARY KEY,
    target_hash     TEXT NOT NULL,
    queries_json    TEXT NOT NULL,
    findings_json   TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    ttl_hours       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_cache_target ON research_cache(target_hash);
"""


def _normalize_query(q: str) -> str:
    return " ".join(q.lower().split())


def _hash_target(target_path: Path) -> str:
    return hashlib.sha256(str(target_path.resolve()).encode("utf-8")).hexdigest()[:16]


def _hash_queries(queries: Iterable[str]) -> str:
    """Order-insensitive hash of normalized queries."""
    normalized = sorted({_normalize_query(q) for q in queries if q.strip()})
    joined = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def _cache_key(target_path: Path, queries: Iterable[str]) -> str:
    return f"{_hash_target(target_path)}:{_hash_queries(queries)}"


class ResearchCache:
    """Thin SQLite-backed cache for synthesized ``ResearchFindings``."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def lookup(
        self,
        target_path: Path,
        queries: list[str],
        *,
        ttl_hours: int,
    ) -> ResearchFindings | None:
        """Return cached findings for this target + query set if fresh.

        Returns ``None`` on cache miss, expired entry, or a row whose stored
        JSON no longer parses against the current ``ResearchFindings`` schema
        (treated as a miss — schema drift is recoverable).
        """
        key = _cache_key(target_path, queries)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT findings_json, fetched_at, ttl_hours FROM research_cache "
                "WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if not _is_fresh(row["fetched_at"], row["ttl_hours"]):
            return None
        try:
            return ResearchFindings.model_validate_json(row["findings_json"])
        except Exception:
            return None

    def store(
        self,
        target_path: Path,
        queries: list[str],
        findings: ResearchFindings,
        *,
        ttl_hours: int,
    ) -> bool:
        """Persist findings for this target + query set. Returns ``True`` on success.

        Best-effort: a write failure logs a warning and returns ``False`` so
        the calling stage can keep going.
        """
        key = _cache_key(target_path, queries)
        target_hash = _hash_target(target_path)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO research_cache "
                    "(cache_key, target_hash, queries_json, findings_json, fetched_at, ttl_hours) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "queries_json = excluded.queries_json, "
                    "findings_json = excluded.findings_json, "
                    "fetched_at = excluded.fetched_at, "
                    "ttl_hours = excluded.ttl_hours",
                    (
                        key,
                        target_hash,
                        _queries_json(queries),
                        findings.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                        int(ttl_hours),
                    ),
                )
            return True
        except sqlite3.Error as exc:
            from smithic.telemetry.logger import event

            event("cache.write_failed", error=repr(exc), target=str(target_path))
            return False

    def clear(self, target_path: Path | None = None) -> int:
        """Drop cache entries. Returns the number of rows deleted.

        With ``target_path`` set, only entries for that target are cleared
        (used by ``smithic clean --cache``). With ``None``, the whole cache
        is wiped.
        """
        with self._connect() as conn:
            if target_path is None:
                cur = conn.execute("DELETE FROM research_cache")
            else:
                cur = conn.execute(
                    "DELETE FROM research_cache WHERE target_hash = ?",
                    (_hash_target(target_path),),
                )
            return cur.rowcount or 0

    def stats(self) -> dict[str, int]:
        """Diagnostic counts — fresh, expired, total."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fetched_at, ttl_hours FROM research_cache"
            ).fetchall()
        fresh = sum(1 for r in rows if _is_fresh(r["fetched_at"], r["ttl_hours"]))
        return {"total": len(rows), "fresh": fresh, "expired": len(rows) - fresh}


def _queries_json(queries: list[str]) -> str:
    import json

    return json.dumps(sorted({_normalize_query(q) for q in queries if q.strip()}))


def _is_fresh(fetched_at: str, ttl_hours: int) -> bool:
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = datetime.now(UTC) - ts
    return age <= timedelta(hours=ttl_hours)


__all__ = ["ResearchCache"]
