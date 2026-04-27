"""Critique stage tests — Claude SDK is mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.budget.exceptions import AbortRun
from smithic.budget.meter import BudgetCeiling, Meter
from smithic.memory.db import Memory
from smithic.stages.critique import CritiqueResult, run_critique
from smithic.types.critique import CriticVerdict
from tests._fakes import assistant_msg, result_msg, scripted_query


def _meter(tmp_path: Path) -> Meter:
    memory = Memory(tmp_path / ".smithic" / "smithic.db")
    memory.start_run("rid", "/repo", None)
    return Meter(memory, "rid", BudgetCeiling(max_usd=10.0, max_tokens=1_000_000), enforce_usd=False)


def _setup_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake worktree with a spec.md and a non-empty diff."""
    worktree = tmp_path / "wt"
    smithic_dir = worktree / ".smithic"
    smithic_dir.mkdir(parents=True)
    spec = smithic_dir / "spec.md"
    spec.write_text("# Spec\n\nAdd /healthz endpoint.\n", encoding="utf-8")
    return worktree, spec


def _verdict_json(verdict: str) -> str:
    return CriticVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        issues=[],
        spec_adherence=0.95 if verdict == "pass" else 0.6,
        convention_drift=0.9,
        summary=f"verdict was {verdict}",
    ).model_dump_json()


@pytest.mark.anyio("asyncio")
async def test_critique_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree, spec = _setup_worktree(tmp_path)
    monkeypatch.setattr(
        "smithic.stages.critique.read_diff",
        lambda wt, base: "diff --git a/x b/x\n+ added line\n",
    )
    scripts: list[list[object]] = [
        [assistant_msg(_verdict_json("pass")), result_msg(total_cost_usd=0.03)]
    ]
    monkeypatch.setattr("smithic.stages.critique.query", scripted_query(scripts))

    result = await run_critique(
        spec_path=spec,
        worktree_path=worktree,
        base_branch="main",
        introspection=None,
        meter=_meter(tmp_path),
    )
    assert isinstance(result, CritiqueResult)
    assert result.verdict.verdict == "pass"
    assert result.skipped is False


@pytest.mark.anyio("asyncio")
async def test_critique_revise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree, spec = _setup_worktree(tmp_path)
    monkeypatch.setattr("smithic.stages.critique.read_diff", lambda wt, base: "diff stuff")
    scripts = [
        [assistant_msg(_verdict_json("revise")), result_msg()]
    ]
    monkeypatch.setattr("smithic.stages.critique.query", scripted_query(scripts))

    result = await run_critique(
        spec_path=spec,
        worktree_path=worktree,
        base_branch="main",
        introspection=None,
        meter=_meter(tmp_path),
    )
    assert result.verdict.verdict == "revise"


@pytest.mark.anyio("asyncio")
async def test_critique_abort_on_empty_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree, spec = _setup_worktree(tmp_path)
    monkeypatch.setattr("smithic.stages.critique.read_diff", lambda wt, base: "")
    # Should NOT call query when diff is empty.
    monkeypatch.setattr(
        "smithic.stages.critique.query",
        lambda **k: (_ for _ in ()).throw(AssertionError("query should not be called")),
    )
    result = await run_critique(
        spec_path=spec,
        worktree_path=worktree,
        base_branch="main",
        introspection=None,
        meter=_meter(tmp_path),
    )
    assert result.verdict.verdict == "abort"
    assert result.skipped is True


@pytest.mark.anyio("asyncio")
async def test_critique_aborts_on_non_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree, spec = _setup_worktree(tmp_path)
    monkeypatch.setattr("smithic.stages.critique.read_diff", lambda wt, base: "diff stuff")
    scripts = [[assistant_msg("blah blah"), result_msg()]]
    monkeypatch.setattr("smithic.stages.critique.query", scripted_query(scripts))

    with pytest.raises(AbortRun, match="non-JSON"):
        await run_critique(
            spec_path=spec,
            worktree_path=worktree,
            base_branch="main",
            introspection=None,
            meter=_meter(tmp_path),
        )


def test_critic_verdict_renders_revise_feedback() -> None:
    from smithic.types.critique import CriticIssue

    v = CriticVerdict(
        verdict="revise",
        issues=[
            CriticIssue(severity="critical", message="missing tests for /healthz", file_hint="tests/test_app.py"),
            CriticIssue(severity="nit", message="line too long"),
        ],
        spec_adherence=0.5,
        convention_drift=0.8,
        summary="ship blocker: no tests",
    )
    feedback = v.as_revise_feedback()
    assert "ship blocker" in feedback
    assert "missing tests" in feedback
    assert "tests/test_app.py" in feedback
