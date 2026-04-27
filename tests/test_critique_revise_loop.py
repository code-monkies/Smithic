"""Orchestrator-level test: critic verdict drives revise/abort/draft."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.config import (
    AuthConfig,
    BudgetConfig,
    CritiqueConfig,
    PRConfig,
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
from smithic.types.critique import CriticVerdict
from smithic.worktree.manager import Worktree


def _config(target: Path) -> SmithicConfig:
    return SmithicConfig(
        target=TargetConfig(path=target, mission_text="Test mission."),
        swarm=SwarmConfig(),
        budget=BudgetConfig(),
        auth=AuthConfig(mode="api"),
        research=ResearchConfig(),
        rubric=RubricConfig(),
        critique=CritiqueConfig(enable=True, max_revise_loops=1),
        pr=PRConfig(),
    )


def _verdict(kind: str, summary: str = "") -> CriticVerdict:
    return CriticVerdict(
        verdict=kind,  # type: ignore[arg-type]
        issues=[],
        spec_adherence=0.9,
        convention_drift=0.9,
        summary=summary or f"verdict={kind}",
    )


def _impl_result(succeeded: bool = True) -> ImplementResult:
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
    """Patch out every external surface so the orchestrator runs in-memory."""
    from pathlib import Path as _Path

    target = _Path(str(tmp_path)) / "target"  # type: ignore[arg-type]
    target.mkdir()
    cfg = _config(target)

    # auth: pretend api mode is fine
    monkeypatch.setattr("smithic.orchestrator.preflight", lambda mode, cli_path=None: "api")
    monkeypatch.setattr("smithic.orchestrator.is_metered", lambda mode: True)
    monkeypatch.setattr("smithic.orchestrator.env_for_mode", lambda mode: {})

    # introspect: a stable report
    monkeypatch.setattr("smithic.orchestrator.introspect", lambda p: _intro(target))

    # worktree: don't actually call git
    class _StubManager:
        def __init__(self, *a, **kw) -> None:
            pass

        def create(self, run_id, feature, base_branch="main"):
            wt = _wt(target)
            return wt

        def remove(self, *a, **kw) -> None:
            pass

        def list(self):
            return []

    monkeypatch.setattr("smithic.orchestrator.WorktreeManager", _StubManager)

    return cfg, target


@pytest.mark.anyio("asyncio")
async def test_pass_opens_pr(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env

    async def fake_impl(*, revise_feedback=None, **kw):
        return _impl_result(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(verdict=_verdict("pass"), cost_usd=0.02)

    captured: dict[str, object] = {}

    def fake_open_pr(*, draft, extra_labels, **kw):
        captured["draft"] = draft
        captured["extra_labels"] = extra_labels or []
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
    assert captured["draft"] is False
    assert captured["extra_labels"] == []


@pytest.mark.anyio("asyncio")
async def test_pass_with_concerns_opens_draft_pr(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env

    async def fake_impl(**kw):
        return _impl_result(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(verdict=_verdict("pass-with-concerns"), cost_usd=0.02)

    captured: dict[str, object] = {}

    def fake_open_pr(*, draft, extra_labels, **kw):
        captured["draft"] = draft
        captured["extra_labels"] = extra_labels or []
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
async def test_revise_then_pass_opens_pr(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env

    impl_calls: list[str | None] = []

    async def fake_impl(*, revise_feedback=None, **kw):
        impl_calls.append(revise_feedback)
        return _impl_result(succeeded=True)

    crit_calls = {"i": 0}
    verdicts = [_verdict("revise", "needs more tests"), _verdict("pass", "fixed")]

    async def fake_critique(**kw):
        v = verdicts[crit_calls["i"]]
        crit_calls["i"] += 1
        return CritiqueResult(verdict=v, cost_usd=0.02)

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
    assert outcome.status == "completed"
    # Implement called twice — second time with revise_feedback set.
    assert len(impl_calls) == 2
    assert impl_calls[0] is None
    assert impl_calls[1] is not None and "needs more tests" in impl_calls[1]


@pytest.mark.anyio("asyncio")
async def test_revise_loop_exhausted_aborts(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env
    # Default cfg has max_revise_loops=1; two consecutive revise verdicts → abort.

    async def fake_impl(**kw):
        return _impl_result(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(verdict=_verdict("revise", "still bad"), cost_usd=0.02)

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


@pytest.mark.anyio("asyncio")
async def test_critic_abort_skips_pr(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env

    async def fake_impl(**kw):
        return _impl_result(succeeded=True)

    async def fake_critique(**kw):
        return CritiqueResult(verdict=_verdict("abort", "fundamentally wrong"), cost_usd=0.02)

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
    assert "fundamentally wrong" in outcome.notes


@pytest.mark.anyio("asyncio")
async def test_no_critique_flag_skips_critic(
    fake_run_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target = fake_run_env
    cfg.critique = cfg.critique.model_copy(update={"enable": False})

    async def fake_impl(**kw):
        return _impl_result(succeeded=True)

    crit_called = {"hit": False}

    async def fake_critique(**kw):
        crit_called["hit"] = True
        return CritiqueResult(verdict=_verdict("pass"), cost_usd=0.02)

    monkeypatch.setattr("smithic.orchestrator.run_implementation", fake_impl)
    monkeypatch.setattr("smithic.orchestrator.run_critique", fake_critique)
    monkeypatch.setattr("smithic.orchestrator.open_pr", lambda **kw: _pr())

    outcome = await run_once(
        config=cfg,
        config_dir=target,
        feature_seed="add a /healthz endpoint",
        db_path=tmp_path / "smithic.db",
    )
    assert outcome.status == "completed"
    assert crit_called["hit"] is False
