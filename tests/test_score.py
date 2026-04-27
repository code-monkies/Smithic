"""Score stage tests — Claude SDK is mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.budget.exceptions import AbortRun
from smithic.budget.meter import BudgetCeiling, Meter
from smithic.memory.db import Memory
from smithic.rubric.loader import load_rubric
from smithic.stages.score import apply_rubric, feature_from_selection, run_score
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
        description="A description.",
        inferred_user_pain="It hurts.",
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


def _scored(title: str, *axis_scores: tuple[str, float]) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=_candidate(title),
        axes=[AxisScore(axis=name, score=s, rationale="r") for name, s in axis_scores],
        total=0.0,
    )


def _all_axes_score(value: float) -> list[tuple[str, float]]:
    return [
        ("market_demand", value),
        ("competitive_gap", value),
        ("effort_fit", value),
        ("strategic_alignment", value),
        ("user_pain_intensity", value),
        ("reversibility", value),
    ]


def test_apply_rubric_picks_winner() -> None:
    rubric = load_rubric(None)
    scoring = ScoringResult(
        scored=[
            _scored("strong feature", *_all_axes_score(0.9)),
            _scored("weak feature", *_all_axes_score(0.10)),
        ],
        selected=None,
    )
    fixed = apply_rubric(scoring, rubric)
    assert fixed.selected is not None
    assert fixed.selected.candidate.title == "strong feature"
    assert fixed.scored[1].disqualified
    assert fixed.scored[0].total > 0.85


def test_apply_rubric_aborts_when_all_below_threshold() -> None:
    rubric = load_rubric(None)
    scoring = ScoringResult(
        scored=[
            _scored("a", *_all_axes_score(0.30)),
            _scored("b", *_all_axes_score(0.40)),
        ],
        selected=None,
    )
    fixed = apply_rubric(scoring, rubric)
    assert fixed.selected is None
    assert fixed.abort_reason is not None
    assert "thresholds" in fixed.abort_reason


def test_apply_rubric_disqualifies_on_per_axis_floor() -> None:
    rubric = load_rubric(None)
    axes = _all_axes_score(0.9)
    # One axis below the per-axis floor, even though the weighted total is high.
    axes[0] = ("market_demand", 0.10)
    scoring = ScoringResult(scored=[_scored("a", *axes)], selected=None)
    fixed = apply_rubric(scoring, rubric)
    assert fixed.scored[0].disqualified
    assert fixed.selected is None


@pytest.mark.anyio("asyncio")
async def test_run_score_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rubric = load_rubric(None)
    canned = ScoringResult(
        scored=[
            _scored("Add /healthz", *_all_axes_score(0.85)),
            _scored("Add tracing", *_all_axes_score(0.50)),
        ],
        selected=None,
    ).model_dump_json()
    scripts: list[list[object]] = [
        [assistant_msg(canned), result_msg(total_cost_usd=0.05)],
    ]
    monkeypatch.setattr("smithic.stages.score.query", scripted_query(scripts))

    result = await run_score(
        findings=_findings("Add /healthz", "Add tracing"),
        rubric=rubric,
        introspection=None,
        meter=_meter(tmp_path),
    )
    assert result.scoring.selected is not None
    assert result.scoring.selected.candidate.title == "Add /healthz"


@pytest.mark.anyio("asyncio")
async def test_run_score_retries_on_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rubric = load_rubric(None)
    good = ScoringResult(
        scored=[_scored("Add /healthz", *_all_axes_score(0.85))],
        selected=None,
    ).model_dump_json()
    scripts: list[list[object]] = [
        [assistant_msg("not json at all"), result_msg(total_cost_usd=0.01)],
        [assistant_msg(good), result_msg(total_cost_usd=0.02)],
    ]
    monkeypatch.setattr("smithic.stages.score.query", scripted_query(scripts))

    result = await run_score(
        findings=_findings("Add /healthz"),
        rubric=rubric,
        introspection=None,
        meter=_meter(tmp_path),
    )
    assert result.scoring.selected is not None


@pytest.mark.anyio("asyncio")
async def test_run_score_aborts_after_two_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rubric = load_rubric(None)
    scripts: list[list[object]] = [
        [assistant_msg("nope"), result_msg()],
        [assistant_msg("still nope"), result_msg()],
    ]
    monkeypatch.setattr("smithic.stages.score.query", scripted_query(scripts))
    with pytest.raises(AbortRun, match="valid JSON"):
        await run_score(
            findings=_findings("Add /healthz"),
            rubric=rubric,
            introspection=None,
            meter=_meter(tmp_path),
        )


def test_feature_from_selection_returns_seed_and_rationale() -> None:
    rubric = load_rubric(None)
    scoring = apply_rubric(
        ScoringResult(
            scored=[_scored("Add /healthz", *_all_axes_score(0.85))],
            selected=None,
        ),
        rubric,
    )
    assert scoring.selected is not None
    seed, rationale = feature_from_selection(scoring.selected)
    assert seed == "Add /healthz"
    assert "Per-axis breakdown" in rationale
    assert "market_demand" in rationale
