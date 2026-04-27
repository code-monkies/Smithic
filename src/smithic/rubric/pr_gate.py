"""Quantitative PR gate applied to the critic's verdict before opening a PR.

The critic subagent in ``stages/critique.py`` returns a verdict literal
(``pass`` / ``pass-with-concerns`` / ``revise`` / ``abort``) plus float scores
for ``spec_adherence`` and ``convention_drift`` in ``[0, 1]`` and a list of
issues with severities. Without a gate, a model that hallucinates a "pass"
verdict alongside a 0.1 spec_adherence score still ships a PR — exactly the
17M-AI-PR slop pattern the spec is meant to defend against.

The gate is intentionally simple: it never *promotes* a verdict (a critic
``revise``/``abort`` always wins), it only *demotes* — turning a marginal pass
into ``pass-with-concerns`` or aborting the run if the scores fall below a
hard floor. Critical issues alone are enough to block too — even a high
score with one ``severity=critical`` issue should not open a clean PR.

Disable with ``[pr_gate] enable = false`` to fall back to v0.2 behavior
(critic literal verdict drives everything, no quantitative floor).
"""

from __future__ import annotations

from dataclasses import dataclass

from smithic.types.critique import CriticVerdict, CriticVerdictLiteral

# Config is the source of truth for thresholds, but the gate must be importable
# without dragging in the SmithicConfig graph. Keep this module config-free —
# it takes a plain ``PRGateThresholds`` dataclass.


@dataclass(frozen=True)
class PRGateThresholds:
    """Pure-data view of the gate thresholds.

    ``concerns_*`` must be >= ``min_*`` (validated by ``PRGateConfig`` when
    loaded from ``smithic.toml``; the dataclass form is what the gate
    operates on so the function is testable without the pydantic graph).
    """

    enable: bool = True
    min_spec_adherence: float = 0.50
    min_convention_drift: float = 0.40
    concerns_spec_adherence: float = 0.75
    concerns_convention_drift: float = 0.60


@dataclass(frozen=True)
class PRGateOutcome:
    """Result of applying the gate to a critic verdict.

    - ``verdict`` is the (possibly demoted) verdict literal the orchestrator
      should act on.
    - ``original_verdict`` is what the critic LLM returned, preserved for
      telemetry / audit.
    - ``reason`` is a one-sentence explanation when the gate changed the
      verdict (empty string when ``verdict == original_verdict``).
    - ``triggered`` is ``True`` iff the gate actually mutated the verdict.
    """

    verdict: CriticVerdictLiteral
    original_verdict: CriticVerdictLiteral
    reason: str
    triggered: bool


def _critical_issue_count(verdict: CriticVerdict) -> int:
    return sum(1 for issue in verdict.issues if issue.severity == "critical")


def apply_pr_gate(
    verdict: CriticVerdict, thresholds: PRGateThresholds
) -> PRGateOutcome:
    """Apply the quantitative gate to a critic verdict.

    Behavior matrix:

    - Gate disabled or critic already says ``revise`` / ``abort``:
      pass through unchanged.
    - ``pass`` / ``pass-with-concerns`` with any axis below ``min_*``:
      demote to ``abort``.
    - ``pass`` / ``pass-with-concerns`` with at least one
      ``severity=critical`` issue: demote to ``abort``.
    - ``pass`` with any axis below ``concerns_*``:
      demote to ``pass-with-concerns``.
    - Otherwise: pass through unchanged.

    The gate never promotes — it can only make the verdict stricter.
    """
    original = verdict.verdict
    if not thresholds.enable:
        return PRGateOutcome(
            verdict=original, original_verdict=original, reason="", triggered=False
        )
    if original in ("revise", "abort"):
        return PRGateOutcome(
            verdict=original, original_verdict=original, reason="", triggered=False
        )

    spec = verdict.spec_adherence
    drift = verdict.convention_drift

    # Hard floor: any axis below ``min_*`` aborts the run regardless of
    # which "pass" flavor the critic returned. Same for any critical issue
    # — a single ``severity=critical`` finding is, by definition, a blocker.
    if spec < thresholds.min_spec_adherence:
        return PRGateOutcome(
            verdict="abort",
            original_verdict=original,
            reason=(
                f"PR gate: spec_adherence {spec:.2f} below floor "
                f"{thresholds.min_spec_adherence:.2f}"
            ),
            triggered=True,
        )
    if drift < thresholds.min_convention_drift:
        return PRGateOutcome(
            verdict="abort",
            original_verdict=original,
            reason=(
                f"PR gate: convention_drift {drift:.2f} below floor "
                f"{thresholds.min_convention_drift:.2f}"
            ),
            triggered=True,
        )
    crit_n = _critical_issue_count(verdict)
    if crit_n > 0:
        return PRGateOutcome(
            verdict="abort",
            original_verdict=original,
            reason=(
                f"PR gate: critic reported {crit_n} critical "
                f"issue{'s' if crit_n != 1 else ''} despite verdict={original!r}"
            ),
            triggered=True,
        )

    # Soft demotion: marginal passes get the draft+needs-review treatment.
    # Only demote ``pass`` — ``pass-with-concerns`` is already the demoted
    # state, so re-applying the same demotion is a no-op (and would
    # over-report ``triggered=True``).
    if original == "pass" and (
        spec < thresholds.concerns_spec_adherence
        or drift < thresholds.concerns_convention_drift
    ):
        return PRGateOutcome(
            verdict="pass-with-concerns",
            original_verdict=original,
            reason=(
                f"PR gate: marginal pass (spec_adherence={spec:.2f}, "
                f"convention_drift={drift:.2f}); demoted to pass-with-concerns"
            ),
            triggered=True,
        )

    return PRGateOutcome(
        verdict=original, original_verdict=original, reason="", triggered=False
    )


__all__ = ["PRGateOutcome", "PRGateThresholds", "apply_pr_gate"]
