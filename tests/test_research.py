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


def test_evidence_promotes_urly_source_to_url() -> None:
    """Real subscription runs collapse citation pointer + body into a single
    ``source`` field, omitting ``url`` entirely. The schema must promote the
    URL-ish source to ``url`` rather than rejecting the whole evidence item.
    """
    ev = Evidence.model_validate(
        {
            "source": "github.com/anthropics/claude-code/issues/26171",
            "summary": "Token-burn loops drain quota.",
        }
    )
    assert ev.url == "github.com/anthropics/claude-code/issues/26171"
    assert ev.snippet == "Token-burn loops drain quota."  # ``summary`` aliased
    assert ev.title  # filled from URL stem


def test_evidence_keeps_human_label_source_when_not_urly() -> None:
    """Sources with whitespace are human labels, not URLs — keep them as-is."""
    ev = Evidence.model_validate(
        {
            "source": "theregister.com 2026/03/31 — Anthropic admits quotas",
            "summary": "Max subscribers reported quota exhaustion in 19 minutes.",
        }
    )
    assert ev.url == ""  # not promoted — has whitespace
    assert ev.title.startswith("theregister.com")


def test_evidence_url_overrides_source_promotion() -> None:
    """If both fields are present, ``url`` wins; ``source`` stays as the label."""
    ev = Evidence.model_validate(
        {
            "source": "Hacker News (Show HN)",
            "url": "https://news.ycombinator.com/item?id=47096937",
            "summary": "...",
        }
    )
    assert ev.url == "https://news.ycombinator.com/item?id=47096937"
    assert ev.source == "Hacker News (Show HN)"


def test_research_findings_parse_with_no_urls_anywhere() -> None:
    """End-to-end: the smith-on-smith run-1e59a7 dump shape must parse."""
    payload = {
        "candidates": [
            {
                "title": "Detect agent token-burn states",
                "description": "Watchdog for thinking loops.",
                "inferred_user_pain": "Quota drains overnight.",
                "evidence": [
                    {"source": "github.com/anthropics/x/issues/1", "summary": "stuck loop"},
                    {"source": "qwe.edu.pl tutorial — autocompact", "summary": "retry storms"},
                ],
            }
        ]
    }
    findings = ResearchFindings.model_validate(payload)
    assert len(findings.candidates) == 1
    assert findings.candidates[0].evidence[0].url.startswith("github.com")
    assert findings.candidates[0].evidence[1].url == ""
