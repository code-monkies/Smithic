"""Research cache tests — hit/miss, TTL, target isolation, ordering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from smithic.memory.cache import ResearchCache
from smithic.types.research import Evidence, FeatureCandidate, ResearchFindings


def _findings(title: str = "Add /healthz") -> ResearchFindings:
    return ResearchFindings(
        candidates=[
            FeatureCandidate(
                title=title,
                description="A description.",
                inferred_user_pain="It hurts.",
                evidence=[
                    Evidence(source="web", url=f"https://x/{i}", title=f"t{i}", snippet="s")
                    for i in range(3)
                ],
            )
        ],
        queries_run=["q1", "q2"],
        sources_used=["web"],
    )


def test_lookup_returns_none_on_miss(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    assert cache.lookup(tmp_path, ["q1"], ttl_hours=72) is None


def test_store_then_lookup_returns_findings(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["FastAPI healthz", "k8s liveness"], _findings(), ttl_hours=72)
    hit = cache.lookup(tmp_path, ["FastAPI healthz", "k8s liveness"], ttl_hours=72)
    assert hit is not None
    assert hit.candidates[0].title == "Add /healthz"


def test_query_order_does_not_affect_key(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["one", "two", "three"], _findings(), ttl_hours=72)
    hit = cache.lookup(tmp_path, ["three", "one", "two"], ttl_hours=72)
    assert hit is not None


def test_query_normalization_dedupes(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["FastAPI Healthz", "fastapi   healthz"], _findings(), ttl_hours=72)
    hit = cache.lookup(tmp_path, ["fastapi healthz"], ttl_hours=72)
    assert hit is not None


def test_different_targets_isolated(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    target_a.mkdir()
    target_b.mkdir()
    cache.store(target_a, ["q"], _findings("Add A"), ttl_hours=72)
    cache.store(target_b, ["q"], _findings("Add B"), ttl_hours=72)

    hit_a = cache.lookup(target_a, ["q"], ttl_hours=72)
    hit_b = cache.lookup(target_b, ["q"], ttl_hours=72)
    assert hit_a is not None and hit_a.candidates[0].title == "Add A"
    assert hit_b is not None and hit_b.candidates[0].title == "Add B"


def test_ttl_expiry_returns_none(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q"], _findings(), ttl_hours=1)

    # Force the row's fetched_at to 2 hours ago.
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with cache._connect() as conn:
        conn.execute(
            "UPDATE research_cache SET fetched_at = ? WHERE 1=1",
            (stale,),
        )

    assert cache.lookup(tmp_path, ["q"], ttl_hours=1) is None


def test_ttl_boundary_just_under_returns_hit(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q"], _findings(), ttl_hours=2)

    # 1h59m old should still be a hit at TTL=2h.
    almost = (datetime.now(UTC) - timedelta(hours=1, minutes=59)).isoformat()
    with cache._connect() as conn:
        conn.execute(
            "UPDATE research_cache SET fetched_at = ? WHERE 1=1",
            (almost,),
        )

    assert cache.lookup(tmp_path, ["q"], ttl_hours=2) is not None


def test_store_overwrites_existing_entry(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q"], _findings("Add v1"), ttl_hours=72)
    cache.store(tmp_path, ["q"], _findings("Add v2"), ttl_hours=72)
    hit = cache.lookup(tmp_path, ["q"], ttl_hours=72)
    assert hit is not None and hit.candidates[0].title == "Add v2"


def test_clear_target_only_drops_that_target(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    target_a.mkdir()
    target_b.mkdir()
    cache.store(target_a, ["q"], _findings("A"), ttl_hours=72)
    cache.store(target_b, ["q"], _findings("B"), ttl_hours=72)

    removed = cache.clear(target_a)
    assert removed == 1
    assert cache.lookup(target_a, ["q"], ttl_hours=72) is None
    assert cache.lookup(target_b, ["q"], ttl_hours=72) is not None


def test_clear_all_drops_everything(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q1"], _findings(), ttl_hours=72)
    cache.store(tmp_path / "other", ["q1"], _findings(), ttl_hours=72)
    removed = cache.clear()
    assert removed == 2
    assert cache.stats() == {"total": 0, "fresh": 0, "expired": 0}


def test_stats_counts_fresh_and_expired(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q1"], _findings(), ttl_hours=72)
    cache.store(tmp_path, ["q2"], _findings(), ttl_hours=1)

    stale = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
    with cache._connect() as conn:
        conn.execute(
            "UPDATE research_cache SET fetched_at = ? WHERE ttl_hours = 1",
            (stale,),
        )

    stats = cache.stats()
    assert stats == {"total": 2, "fresh": 1, "expired": 1}


def test_corrupt_findings_json_treated_as_miss(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    cache.store(tmp_path, ["q"], _findings(), ttl_hours=72)
    with cache._connect() as conn:
        conn.execute("UPDATE research_cache SET findings_json = '{not json'")
    assert cache.lookup(tmp_path, ["q"], ttl_hours=72) is None
