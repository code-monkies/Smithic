"""Pydantic schema for rubric YAML files.

A rubric defines:

- A set of named axes, each with a weight (0..1) and a human-readable description.
- Thresholds: the minimum total score for a candidate to be selected, and the
  per-axis floor below which a candidate is automatically disqualified.

The default rubric ships at ``src/smithic/rubric/default.yaml`` and the
loader merges any user override on top of it (axis-level merge, not replace).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Axis(BaseModel):
    """One scoring axis (e.g. ``market_demand``)."""

    model_config = ConfigDict(extra="forbid")

    weight: float = Field(ge=0.0, le=1.0)
    description: str


class Thresholds(BaseModel):
    """Selection / disqualification thresholds applied after scoring."""

    model_config = ConfigDict(extra="forbid")

    min_total: float = Field(default=0.55, ge=0.0, le=1.0)
    min_per_axis: float = Field(default=0.20, ge=0.0, le=1.0)


class Rubric(BaseModel):
    """Top-level rubric model — what ``rubric/loader.py`` produces."""

    model_config = ConfigDict(extra="forbid")

    axes: dict[str, Axis] = Field(min_length=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Rubric:
        total = sum(axis.weight for axis in self.axes.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(
                f"rubric axis weights must sum to 1.0 (got {total:.4f}). "
                "Adjust weights so the total score stays in [0, 1]."
            )
        return self

    def axis_names(self) -> list[str]:
        return list(self.axes.keys())

    def as_prompt_block(self) -> str:
        """Render the rubric as a markdown block for the scoring subagent."""
        lines = ["## Scoring rubric", ""]
        for name, axis in self.axes.items():
            lines.append(f"- **{name}** (weight {axis.weight:.2f}): {axis.description}")
        lines.append("")
        lines.append(
            f"Thresholds: candidates with total < {self.thresholds.min_total} or "
            f"any axis < {self.thresholds.min_per_axis} are disqualified."
        )
        return "\n".join(lines)
