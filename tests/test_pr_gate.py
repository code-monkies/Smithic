"""Unit tests for the quantitative PR gate.

The gate sits between ``stages/critique`` and the orchestrator's act-on-verdict
logic: a critic ``pass`` with low ``spec_adherence`` / ``convention_drift``
floats — or even a single ``severity=critical`` issue — should not ship a PR.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from smithic.config import PRGateConfig, load_config
from smithic.rubric.pr_gate import PRGateThresholds, apply_pr_gate
from smithic.types.critique import CriticIssue, CriticVerdict


def _verdict(
    verdict: str,
    *,
    spec_adherence: float = 0.9,
    convention_drift: float = 0.9,
    issues: list[CriticIssue] | None = None,
    summary: str = "",
) -> CriticVerdict:
    return CriticVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        issues=issues or [],
        spec_adherence=spec_adherence,
        convention_drift=convention_drift,
        summary=summary,
    )


def _thresholds(**kw: object) -> PRGateThresholds:
    """Build a thresholds dataclass, defaulting to the same numbers as the config."""
    base = {
        "enable": True,
        "min_spec_adherence": 0.50,
        "min_convention_drift": 0.40,
        "concerns_spec_adherence": 0.75,
        "concerns_convention_drift": 0.60,
    }
    base.update(kw)
    return PRGateThresholds(**base)  # type: ignore[arg-type]


# -- gate disabled / non-pass verdicts pass through --------------------------


def test_disabled_gate_passes_through_unchanged() -> None:
    out = apply_pr_gate(
        _verdict("pass", spec_adherence=0.0, convention_drift=0.0),
        _thresholds(enable=False),
    )
    assert out.verdict == "pass"
    assert out.triggered is False
    assert out.reason == ""


def test_revise_verdict_is_not_overridden() -> None:
    # The gate never *promotes* — a critic ``revise`` stays ``revise`` even
    # if the floats happen to be high.
    out = apply_pr_gate(
        _verdict("revise", spec_adherence=0.99, convention_drift=0.99),
        _thresholds(),
    )
    assert out.verdict == "revise"
    assert out.triggered is False


def test_abort_verdict_is_not_overridden() -> None:
    out = apply_pr_gate(
        _verdict("abort", spec_adherence=0.99, convention_drift=0.99),
        _thresholds(),
    )
    assert out.verdict == "abort"
    assert out.triggered is False


# -- hard floor → abort ------------------------------------------------------


def test_low_spec_adherence_aborts() -> None:
    out = apply_pr_gate(
        _verdict("pass", spec_adherence=0.30, convention_drift=0.95),
        _thresholds(),
    )
    assert out.verdict == "abort"
    assert out.original_verdict == "pass"
    assert out.triggered is True
    assert "spec_adherence" in out.reason
    assert "0.30" in out.reason


def test_low_convention_drift_aborts() -> None:
    out = apply_pr_gate(
        _verdict("pass-with-concerns", spec_adherence=0.95, convention_drift=0.10),
        _thresholds(),
    )
    assert out.verdict == "abort"
    assert "convention_drift" in out.reason


def test_critical_issue_aborts_even_with_high_scores() -> None:
    out = apply_pr_gate(
        _verdict(
            "pass",
            spec_adherence=0.99,
            convention_drift=0.99,
            issues=[CriticIssue(severity="critical", message="SQL injection")],
        ),
        _thresholds(),
    )
    assert out.verdict == "abort"
    assert "critical" in out.reason


def test_concern_issue_does_not_abort() -> None:
    # Only ``severity=critical`` triggers the issue-based abort. ``concern``
    # issues are normal feedback that the critic literal verdict already
    # accounts for.
    out = apply_pr_gate(
        _verdict(
            "pass",
            spec_adherence=0.99,
            convention_drift=0.99,
            issues=[CriticIssue(severity="concern", message="nit")],
        ),
        _thresholds(),
    )
    assert out.verdict == "pass"
    assert out.triggered is False


# -- soft demotion → pass-with-concerns --------------------------------------


def test_marginal_pass_demoted_to_pass_with_concerns() -> None:
    # spec_adherence above min but below concerns floor → demote.
    out = apply_pr_gate(
        _verdict("pass", spec_adherence=0.65, convention_drift=0.95),
        _thresholds(),
    )
    assert out.verdict == "pass-with-concerns"
    assert out.original_verdict == "pass"
    assert out.triggered is True
    assert "marginal" in out.reason


def test_marginal_pass_with_concerns_is_not_re_demoted() -> None:
    # Already in the demoted state — the gate should not re-flag triggered.
    out = apply_pr_gate(
        _verdict("pass-with-concerns", spec_adherence=0.65, convention_drift=0.95),
        _thresholds(),
    )
    assert out.verdict == "pass-with-concerns"
    assert out.triggered is False


def test_clean_pass_unchanged() -> None:
    out = apply_pr_gate(
        _verdict("pass", spec_adherence=0.95, convention_drift=0.92),
        _thresholds(),
    )
    assert out.verdict == "pass"
    assert out.triggered is False
    assert out.reason == ""


# -- config plumbing ---------------------------------------------------------


def test_pr_gate_config_defaults() -> None:
    cfg = PRGateConfig()
    assert cfg.enable is True
    assert cfg.min_spec_adherence == 0.50
    assert cfg.min_convention_drift == 0.40
    assert cfg.concerns_spec_adherence == 0.75
    assert cfg.concerns_convention_drift == 0.60


def test_pr_gate_config_concerns_below_min_rejected() -> None:
    with pytest.raises(ValidationError, match="concerns_spec_adherence"):
        PRGateConfig(min_spec_adherence=0.7, concerns_spec_adherence=0.5)


def test_pr_gate_config_loads_from_toml(tmp_path: Path) -> None:
    config = tmp_path / "smithic.toml"
    config.write_text(
        textwrap.dedent(
            """
            [target]
            path = "."
            mission_text = "x"

            [pr_gate]
            enable = true
            min_spec_adherence = 0.6
            min_convention_drift = 0.5
            concerns_spec_adherence = 0.85
            concerns_convention_drift = 0.7
            """
        ),
        encoding="utf-8",
    )
    cfg, _ = load_config(config)
    assert cfg.pr_gate.min_spec_adherence == 0.6
    assert cfg.pr_gate.concerns_convention_drift == 0.7


def test_pr_gate_config_disable_via_toml(tmp_path: Path) -> None:
    config = tmp_path / "smithic.toml"
    config.write_text(
        textwrap.dedent(
            """
            [target]
            path = "."
            mission_text = "x"

            [pr_gate]
            enable = false
            """
        ),
        encoding="utf-8",
    )
    cfg, _ = load_config(config)
    assert cfg.pr_gate.enable is False
