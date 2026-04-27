"""Research stage with the v0.3 cache wired in.

Verifies the second invocation skips the synthesis Claude call when the cache
key matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.budget.meter import BudgetCeiling, Meter
from smithic.config import ResearchConfig
from smithic.memory.cache import ResearchCache
from smithic.memory.db import Memory
from smithic.stages.introspect import IntrospectionReport
from smithic.stages.research import run_research
from smithic.types.research import Evidence, FeatureCandidate, ResearchFindings
from tests._fakes import assistant_msg, result_msg, scripted_query


def _meter(tmp_path: Path, run_id: str = "rid") -> Meter:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run(run_id, "/repo", None)
    return Meter(memory, run_id, BudgetCeiling(max_usd=10.0, max_tokens=1_000_000), enforce_usd=False)


def _intro() -> IntrospectionReport:
    return IntrospectionReport(repo_path=Path("/tmp/repo"))


def _findings_json() -> str:
    return ResearchFindings(
        candidates=[
            FeatureCandidate(
                title="Add /healthz",
                description="Liveness endpoint.",
                inferred_user_pain="No probe target.",
                evidence=[
                    Evidence(source="reddit", url="https://r/1", title="t1", snippet="s")
                    for _ in range(3)
                ],
            )
        ],
        queries_run=["q1", "q2"],
        sources_used=["reddit"],
    ).model_dump_json()


@pytest.mark.anyio("asyncio")
async def test_second_run_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ResearchCache(tmp_path / "cache.db")
    target = tmp_path / "target"
    target.mkdir()
    out_dir = tmp_path / "wt" / ".smithic"

    queries_payload = '{"queries": ["FastAPI healthz", "k8s liveness probe"]}'

    # First run: queries call + synth call.
    scripts_first: list[list[object]] = [
        [assistant_msg(queries_payload), result_msg(total_cost_usd=0.01)],
        [assistant_msg(_findings_json()), result_msg(total_cost_usd=0.04)],
    ]
    monkeypatch.setattr("smithic.stages.research.query", scripted_query(scripts_first))
    monkeypatch.setattr(
        "smithic.stages.research.build_mcp_servers",
        lambda sources: {"reddit": {"type": "stdio", "command": "x", "args": []}},
    )

    cfg = ResearchConfig(sources=["reddit"], max_candidates=3)
    first = await run_research(
        mission="Build a FastAPI service.",
        introspection=_intro(),
        research_cfg=cfg,
        out_dir=out_dir,
        run_id="r1",
        meter=_meter(tmp_path, run_id="r1"),
        cache=cache,
        target_path=target,
    )
    assert first.cache_hit is False

    # Second run: the queries call still happens (each child generates its own
    # query plan), but if the planner produces the same query set, synthesis
    # is skipped. We simulate by giving the second run the same queries.
    out_dir2 = tmp_path / "wt2" / ".smithic"
    scripts_second: list[list[object]] = [
        [assistant_msg(queries_payload), result_msg(total_cost_usd=0.01)],
        # No synth script — if it's called, scripted_query saturates and we'd
        # get an empty text → AbortRun. The test fails loudly if synth runs.
    ]
    monkeypatch.setattr("smithic.stages.research.query", scripted_query(scripts_second))

    second = await run_research(
        mission="Build a FastAPI service.",
        introspection=_intro(),
        research_cfg=cfg,
        out_dir=out_dir2,
        run_id="r2",
        meter=_meter(tmp_path, run_id="r2"),
        cache=cache,
        target_path=target,
    )
    assert second.cache_hit is True
    assert second.findings.candidates[0].title == "Add /healthz"
    # Brief artifact still gets written into the second run's worktree.
    assert (out_dir2 / "research.md").exists()


@pytest.mark.anyio("asyncio")
async def test_cache_disabled_when_no_cache_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-existing v0.2 behavior: without a cache, every run does synth."""
    queries_payload = '{"queries": ["q1", "q2"]}'
    calls = {"i": 0}

    async def fake_query(*, prompt, options):
        scripts = [
            [assistant_msg(queries_payload), result_msg()],
            [assistant_msg(_findings_json()), result_msg()],
        ]
        idx = min(calls["i"], len(scripts) - 1)
        calls["i"] += 1
        for msg in scripts[idx]:
            yield msg

    monkeypatch.setattr("smithic.stages.research.query", fake_query)
    monkeypatch.setattr(
        "smithic.stages.research.build_mcp_servers",
        lambda sources: {"reddit": {"type": "stdio", "command": "x", "args": []}},
    )

    result = await run_research(
        mission="m",
        introspection=_intro(),
        research_cfg=ResearchConfig(sources=["reddit"]),
        out_dir=tmp_path / ".smithic",
        run_id="r",
        meter=_meter(tmp_path),
        cache=None,
        target_path=None,
    )
    assert result.cache_hit is False
    assert calls["i"] == 2  # queries + synth
