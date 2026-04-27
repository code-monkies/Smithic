"""Research stage tests — Claude SDK is mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.budget.exceptions import AbortRun
from smithic.budget.meter import BudgetCeiling, Meter
from smithic.config import ResearchConfig
from smithic.memory.db import Memory
from smithic.stages.introspect import IntrospectionReport
from smithic.stages.research import run_research, write_research_artifacts
from smithic.types.research import Evidence, FeatureCandidate, ResearchFindings
from tests._fakes import assistant_msg, result_msg, scripted_query


def _meter(tmp_path: Path) -> Meter:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run("rid", "/repo", None)
    # Unmetered so remaining_usd is inf — we don't want to test budget pre-checks here.
    return Meter(memory, "rid", BudgetCeiling(max_usd=10.0, max_tokens=1_000_000), enforce_usd=False)


def _intro() -> IntrospectionReport:
    return IntrospectionReport(repo_path=Path("/tmp/repo"))


def _findings_json() -> str:
    return ResearchFindings(
        candidates=[
            FeatureCandidate(
                title="Add /healthz endpoint",
                description="A simple liveness probe endpoint for ops.",
                inferred_user_pain="No way to monitor liveness without a custom endpoint.",
                evidence=[
                    Evidence(
                        source="reddit",
                        url="https://reddit.com/r/Python/post1",
                        title="FastAPI healthz best practices?",
                        snippet="Trying to set up k8s probes...",
                    ),
                    Evidence(
                        source="web",
                        url="https://example.com/post",
                        title="Why every service needs /healthz",
                        snippet="Ops teams expect this.",
                    ),
                    Evidence(
                        source="reddit",
                        url="https://reddit.com/r/devops/post2",
                        title="K8s liveness probe missing endpoint",
                        snippet="Pod restarts are killing us.",
                    ),
                ],
            )
        ],
        queries_run=["FastAPI healthz", "k8s liveness probe"],
        sources_used=["reddit", "fetch"],
    ).model_dump_json()


@pytest.mark.anyio("asyncio")
async def test_run_research_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = tmp_path / ".smithic"

    queries_payload = '{"queries": ["FastAPI healthz", "k8s liveness probe"]}'
    scripts: list[list[object]] = [
        [assistant_msg(queries_payload), result_msg(total_cost_usd=0.01)],
        [assistant_msg(_findings_json()), result_msg(total_cost_usd=0.04)],
    ]
    fake = scripted_query(scripts)
    monkeypatch.setattr("smithic.stages.research.query", fake)
    monkeypatch.setattr(
        "smithic.stages.research.build_mcp_servers",
        lambda sources: {"reddit": {"type": "stdio", "command": "x", "args": []}},
    )

    cfg = ResearchConfig(sources=["reddit"], max_candidates=3)
    meter = _meter(tmp_path)
    result = await run_research(
        mission="Build a FastAPI service.",
        introspection=_intro(),
        research_cfg=cfg,
        out_dir=out_dir,
        run_id="rid",
        meter=meter,
    )

    assert result.findings.candidates[0].title.startswith("Add /healthz")
    assert (out_dir / "research.md").exists()
    assert (out_dir / "research.json").exists()
    assert "FastAPI healthz" in (out_dir / "research.md").read_text(encoding="utf-8")


@pytest.mark.anyio("asyncio")
async def test_run_research_no_sources_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("smithic.stages.research.build_mcp_servers", lambda sources: {})
    cfg = ResearchConfig(sources=[])
    meter = _meter(tmp_path)
    with pytest.raises(AbortRun, match="no research sources"):
        await run_research(
            mission="m",
            introspection=_intro(),
            research_cfg=cfg,
            out_dir=tmp_path / ".smithic",
            run_id="rid",
            meter=meter,
        )


@pytest.mark.anyio("asyncio")
async def test_run_research_empty_queries_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[list[object]] = [
        [assistant_msg("not json"), result_msg()],
    ]
    monkeypatch.setattr("smithic.stages.research.query", scripted_query(scripts))
    monkeypatch.setattr(
        "smithic.stages.research.build_mcp_servers",
        lambda sources: {"reddit": {"type": "stdio", "command": "x", "args": []}},
    )
    cfg = ResearchConfig(sources=["reddit"])
    with pytest.raises(AbortRun, match="generate queries"):
        await run_research(
            mission="m",
            introspection=_intro(),
            research_cfg=cfg,
            out_dir=tmp_path / ".smithic",
            run_id="rid",
            meter=_meter(tmp_path),
        )


def test_write_research_artifacts_round_trips(tmp_path: Path) -> None:
    findings = ResearchFindings.model_validate_json(_findings_json())
    out = tmp_path / ".smithic"
    md_path = write_research_artifacts(findings=findings, out_dir=out, run_id="rid")
    assert md_path.exists()
    body = md_path.read_text(encoding="utf-8")
    assert "Smithic research brief" in body
    assert findings.candidates[0].title in body
