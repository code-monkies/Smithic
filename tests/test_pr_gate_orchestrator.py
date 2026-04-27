"""Orchestrator-level test: PR gate translates a hallucinated ``pass`` into an abort.

The unit-level coverage of the gate logic itself lives in ``test_pr_gate.py``.
This file is the wiring check — does the orchestrator actually route the
critic's verdict through the gate before opening a PR?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.config import (
    AuthConfig,
    BudgetConfig,
    CritiqueConfig,
    PRConfig,
    PRGateConfig,
    ResearchConfig,
    RubricConfig,
    SmithicConfig,
    SwarmConfig,
    TargetConfig,
)
from smithic.orchestrator import run_once
from smithic.stages.critique import CritiqueResult
from smithic.stages.implement import ImplementResult
from smithic.stages.introspect import IntrospectionReport
from smithic.stages.pr import PRResult
from smithic.types.critique import CriticIssue, CriticVerdict
from smithic.worktree.manager import Worktree


def _config(target: Path, *, pr_gate: PRGateConfig | None = None) -> SmithicConfig:
    return SmithicConfig(
        target=TargetConfig(path=target, mission_text="Test mission."),
        swarm=SwarmConfig(),
        budget=BudgetConfig(),
        auth=AuthConfig(mode="api"),
        research=ResearchConfig(),
        rubric=RubricConfig(),
        critique=CritiqueConfig(enable=True, max_revise_loops=0),
        pr_gate=pr_gate or PRGateConfig(),
        pr=PRConfig(),
    )


def _impl(succeeded: bool = True) -> ImplementResult:
    return ImplementResult(
        succeeded=succeeded,
        summary="implementation done" if succeeded else "",
        cost_usd=0.05,
        input_tokens=100,
        output_tokens=50,
        session_id="sess-impl",
        num_turns=3,
    )


def _wt(tmp_path: Path) -> Worktree:
    p = tmp_path / "wt"
    (p / ".smithic").mkdir(parents=True, exist_ok=True)
    return Worktree(path=p, branch="smithic/test", base_branch="main")


def _intro(tmp_path: Path) -> IntrospectionReport:
    return IntrospectionReport(repo_path=tmp_path)


def _pr(url: str = "https://github.com/x/y/pull/1") -> PRResult:
    return PRResult(url=url, branch="smithic/test", is_draft=False)


@pytest.fixture
def fake_run_env(tmp_path: pytest.TempdirFactory, monkeypatch: pytest.MonkeyPatch):
    target = Path(str(tmp_path)) / "target"
    target.mkdir()

    monkeypatch.setattr("smithic.orchestrator.preflight", lambda mode, cli_path=None: "api")
    monkeypatch.setattr("smithic.orchestrator.is_metered", lambda mode: True)
    monkeypatch.setattr("smithic.orchestrator.env_for_mode", lambda mode: {})
    monkeypatch.setattr("smithic.orchestrator.introspect", lambda p: _intro(target))

    class _StubManager:
        def __init__(self, *a, **kw) -> None:
            pass

        def create(self, run_id, feature, base_branch="main"):
            return _wt(target)

        def remove(self, *a, **kw) -> None:
            pass

        def list(self):
            return []

    monkeypatch.setattr("smithic.orchestrator.WorktreeManager", _StubManager)
    return target


@pytest.mark.anyio("asyncio")
async def test_low_spec_adherence_forces_abort_despite_pass_verdict(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic LLM hallucinates a clean ``pass``, but the floats betray it.

    With the default gate (min_spec_adherence=0.50), a verdict of ``pass``
    paired with spec_adherence=0.10 must abort the run rather than open a PR
    — that's the entire point of the gate.
    """
    target = fake_run_env
    cfg = _config(target)

    async def fake_impl(**kw):
        return _impl(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(
            verdict=CriticVerdict(
                verdict="pass",
                issues=[],
                spec_adherence=0.10,  # Way below the 0.50 floor.
                convention_drift=0.95,
                summary="lgtm",
            ),
            cost_usd=0.02,
        )

    open_pr_called = {"hit": False}

    def fake_open_pr(**kw):
        open_pr_called["hit"] = True
        return _pr()

    monkeypatch.setattr("smithic.orchestrator.run_implementation", fake_impl)
    monkeypatch.setattr("smithic.orchestrator.run_critique", fake_critique)
    monkeypatch.setattr("smithic.orchestrator.open_pr", fake_open_pr)

    outcome = await run_once(
        config=cfg,
        config_dir=target,
        feature_seed="add a /healthz endpoint",
        db_path=tmp_path / "smithic.db",
    )
    assert outcome.status == "aborted"
    assert open_pr_called["hit"] is False
    assert "PR gate" in outcome.notes
    assert "spec_adherence" in outcome.notes


@pytest.mark.anyio("asyncio")
async def test_marginal_pass_demoted_to_draft_pr(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``pass`` with mid-band scores still ships, but as a draft + needs-review."""
    target = fake_run_env
    cfg = _config(target)

    async def fake_impl(**kw):
        return _impl(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(
            verdict=CriticVerdict(
                verdict="pass",
                issues=[],
                spec_adherence=0.65,  # above 0.50 floor, below 0.75 concerns
                convention_drift=0.95,
                summary="mostly fine",
            ),
            cost_usd=0.02,
        )

    captured: dict[str, object] = {}

    def fake_open_pr(*, draft, extra_labels, **kw):
        captured["draft"] = draft
        captured["extra_labels"] = list(extra_labels or [])
        return _pr()

    monkeypatch.setattr("smithic.orchestrator.run_implementation", fake_impl)
    monkeypatch.setattr("smithic.orchestrator.run_critique", fake_critique)
    monkeypatch.setattr("smithic.orchestrator.open_pr", fake_open_pr)

    outcome = await run_once(
        config=cfg,
        config_dir=target,
        feature_seed="add a /healthz endpoint",
        db_path=tmp_path / "smithic.db",
    )
    assert outcome.status == "completed"
    assert captured["draft"] is True
    assert "smithic-needs-review" in captured["extra_labels"]


@pytest.mark.anyio("asyncio")
async def test_critical_issue_aborts_pass(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ``severity=critical`` issue blocks even a numerically-clean pass."""
    target = fake_run_env
    cfg = _config(target)

    async def fake_impl(**kw):
        return _impl(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(
            verdict=CriticVerdict(
                verdict="pass",
                issues=[
                    CriticIssue(
                        severity="critical",
                        message="leaks API key into log line",
                        file_hint="src/app.py",
                    )
                ],
                spec_adherence=0.95,
                convention_drift=0.92,
                summary="ok",
            ),
            cost_usd=0.02,
        )

    open_pr_called = {"hit": False}

    def fake_open_pr(**kw):
        open_pr_called["hit"] = True
        return _pr()

    monkeypatch.setattr("smithic.orchestrator.run_implementation", fake_impl)
    monkeypatch.setattr("smithic.orchestrator.run_critique", fake_critique)
    monkeypatch.setattr("smithic.orchestrator.open_pr", fake_open_pr)

    outcome = await run_once(
        config=cfg,
        config_dir=target,
        feature_seed="add a /healthz endpoint",
        db_path=tmp_path / "smithic.db",
    )
    assert outcome.status == "aborted"
    assert open_pr_called["hit"] is False
    assert "critical" in outcome.notes


@pytest.mark.anyio("asyncio")
async def test_disabled_gate_lets_low_pass_ship(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[pr_gate] enable = false`` falls back to v0.2 verdict-literal-only behavior."""
    target = fake_run_env
    cfg = _config(target, pr_gate=PRGateConfig(enable=False))

    async def fake_impl(**kw):
        return _impl(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(
            verdict=CriticVerdict(
                verdict="pass",
                issues=[],
                spec_adherence=0.05,  # would normally abort
                convention_drift=0.05,
                summary="lgtm",
            ),
            cost_usd=0.02,
        )

    captured: dict[str, object] = {}

    def fake_open_pr(*, draft, extra_labels, **kw):
        captured["draft"] = draft
        return _pr()

    monkeypatch.setattr("smithic.orchestrator.run_implementation", fake_impl)
    monkeypatch.setattr("smithic.orchestrator.run_critique", fake_critique)
    monkeypatch.setattr("smithic.orchestrator.open_pr", fake_open_pr)

    outcome = await run_once(
        config=cfg,
        config_dir=target,
        feature_seed="add a /healthz endpoint",
        db_path=tmp_path / "smithic.db",
    )
    # Gate is off — pass means pass even with terrible scores.
    assert outcome.status == "completed"
    assert captured["draft"] is False
