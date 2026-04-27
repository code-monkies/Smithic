"""Diversity-nudge tests for the score stage prompt builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.budget.meter import BudgetCeiling, Meter
from smithic.memory.db import Memory
from smithic.rubric.loader import load_rubric
from smithic.stages.score import _build_prompt, _diversity_block, run_score
from smithic.types.research import (
    AxisScore,
    Evidence,
    FeatureCandidate,
    ResearchFindings,
    ScoredCandidate,
    ScoringResult,
)
from tests._fakes import assistant_msg, result_msg, scripted_query


def _meter(tmp_path: Path) -> Meter:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run("rid", "/repo", None)
    return Meter(memory, "rid", BudgetCeiling(max_usd=10.0, max_tokens=1_000_000), enforce_usd=False)


def _candidate(title: str) -> FeatureCandidate:
    return FeatureCandidate(
        title=title,
        description="d",
        inferred_user_pain="p",
        evidence=[
            Evidence(source="web", url=f"https://x/{i}", title=f"t{i}", snippet="s")
            for i in range(3)
        ],
    )


def _findings(*titles: str) -> ResearchFindings:
    return ResearchFindings(
        candidates=[_candidate(t) for t in titles],
        queries_run=["q"],
        sources_used=["web"],
    )


def test_diversity_block_empty_when_no_siblings() -> None:
    assert _diversity_block([]) == ""
    assert _diversity_block(["", "  "]) == ""


def test_diversity_block_includes_titles() -> None:
    block = _diversity_block(["Add /healthz", "Add tracing"])
    assert "Sibling diversity" in block
    assert "Add /healthz" in block
    assert "Add tracing" in block


def test_build_prompt_omits_diversity_when_empty() -> None:
    rubric = load_rubric(None)
    prompt = _build_prompt(
        findings=_findings("a"),
        rubric=rubric,
        introspection=None,
        previously_selected=[],
    )
    assert "Sibling diversity" not in prompt


def test_build_prompt_embeds_diversity_when_provided() -> None:
    rubric = load_rubric(None)
    prompt = _build_prompt(
        findings=_findings("a", "b"),
        rubric=rubric,
        introspection=None,
        previously_selected=["Add tracing"],
    )
    assert "Sibling diversity" in prompt
    assert "Add tracing" in prompt


def _all_axes_score(value: float) -> list[tuple[str, float]]:
    return [
        ("market_demand", value),
        ("competitive_gap", value),
        ("effort_fit", value),
        ("strategic_alignment", value),
        ("user_pain_intensity", value),
        ("reversibility", value),
    ]


def _scored(title: str, *axis_scores: tuple[str, float]) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=_candidate(title),
        axes=[AxisScore(axis=name, score=s, rationale="r") for name, s in axis_scores],
        total=0.0,
    )


@pytest.mark.anyio("asyncio")
async def test_run_score_passes_previously_selected_into_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {"prompt": ""}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        canned = ScoringResult(
            scored=[_scored("Add /healthz", *_all_axes_score(0.85))],
            selected=None,
        ).model_dump_json()
        for msg in [assistant_msg(canned), result_msg(total_cost_usd=0.01)]:
            yield msg

    monkeypatch.setattr("smithic.stages.score.query", fake_query)

    rubric = load_rubric(None)
    await run_score(
        findings=_findings("Add /healthz"),
        rubric=rubric,
        introspection=None,
        meter=_meter(tmp_path),
        previously_selected=["Add tracing", "Add metrics"],
    )
    assert "Sibling diversity" in captured["prompt"]
    assert "Add tracing" in captured["prompt"]


def _scripted_for_diversity(canned_json: str):
    return scripted_query(
        [[assistant_msg(canned_json), result_msg(total_cost_usd=0.01)]]
    )
