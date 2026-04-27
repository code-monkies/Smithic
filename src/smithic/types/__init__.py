"""Cross-stage Pydantic value types for the Smithic pipeline.

The orchestrator hands these models from stage to stage so contracts are
explicit. Subagents that emit JSON go through ``model_validate`` here, not
ad-hoc parsing.
"""

from smithic.types.critique import CriticIssue, CriticVerdict
from smithic.types.research import (
    AxisScore,
    Evidence,
    FeatureCandidate,
    ResearchFindings,
    ScoredCandidate,
    ScoringResult,
)

__all__ = [
    "AxisScore",
    "CriticIssue",
    "CriticVerdict",
    "Evidence",
    "FeatureCandidate",
    "ResearchFindings",
    "ScoredCandidate",
    "ScoringResult",
]
