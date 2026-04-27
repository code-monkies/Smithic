"""Critic verdict types — emitted by ``stages/critique.py``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CriticVerdictLiteral = Literal["pass", "pass-with-concerns", "revise", "abort"]


class CriticIssue(BaseModel):
    """One issue the critic found in the diff."""

    # Lenient input parsing — same rationale as types/research.py.
    model_config = ConfigDict(extra="ignore")

    severity: Literal["critical", "concern", "nit"] = "concern"
    message: str
    file_hint: str | None = None


class CriticVerdict(BaseModel):
    """Full structured verdict returned by the critic subagent."""

    model_config = ConfigDict(extra="ignore")

    verdict: CriticVerdictLiteral
    issues: list[CriticIssue] = Field(default_factory=list)
    spec_adherence: float = Field(default=0.5, ge=0.0, le=1.0)
    convention_drift: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""

    def as_revise_feedback(self) -> str:
        """Render the verdict as feedback prepended to the implement stage's prompt."""
        lines = [
            "# Critic feedback (please address before re-attempting)",
            "",
            self.summary.strip(),
            "",
        ]
        if self.issues:
            lines.append("## Issues")
            for issue in self.issues:
                tag = f"**[{issue.severity}]**"
                hint = f" _(see `{issue.file_hint}`)_" if issue.file_hint else ""
                lines.append(f"- {tag} {issue.message}{hint}")
            lines.append("")
        return "\n".join(lines).strip()
