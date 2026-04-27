"""Per-run cost and token accounting with hard-ceiling enforcement.

The Claude Agent SDK has its own ``max_budget_usd`` ceiling we plumb through, but
we keep an out-of-band ledger here for two reasons:

1. We want to know exactly what we spent per stage and per session.
2. The SDK's ceiling is per-call; we need a per-run ceiling that survives multiple
   subagent invocations.

The USD ceiling is treated as a hard limit only when ``enforce_usd`` is set
(which the orchestrator sets to ``True`` only for direct API auth — see
``smithic.auth.is_metered``). For subscription / Bedrock / Vertex / Foundry the
SDK does not aggregate per-call USD, so the figure would always be 0 and
enforcement would be a no-op or, worse, misleading. The token ceiling is hard
in every mode.

v0.2 tightens enforcement: ``would_exceed`` lets stages do a pre-call estimate
based on prior stages' average cost so we catch breaches at the next checkpoint
rather than mid-call. ``snapshot`` returns a frozen view the orchestrator can
log between substages.
"""

from __future__ import annotations

from dataclasses import dataclass

from smithic.budget.exceptions import BudgetExceeded
from smithic.memory.db import Memory


@dataclass(frozen=True)
class BudgetCeiling:
    max_usd: float
    max_tokens: int


@dataclass(frozen=True)
class MeterSnapshot:
    """Frozen view of meter state — safe to log."""

    spent_usd: float
    tokens_used: int
    remaining_usd: float
    enforce_usd: bool


class Meter:
    def __init__(
        self,
        memory: Memory,
        run_id: str,
        ceiling: BudgetCeiling,
        *,
        enforce_usd: bool = True,
    ) -> None:
        self.memory = memory
        self.run_id = run_id
        self.ceiling = ceiling
        self.enforce_usd = enforce_usd

    def record(
        self,
        stage: str,
        cost_usd: float,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        session_id: str | None = None,
    ) -> None:
        self.memory.record_cost(
            self.run_id,
            stage,
            cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            session_id=session_id,
        )

    def spent(self) -> float:
        return self.memory.total_cost(self.run_id)

    def tokens_used(self) -> int:
        return self.memory.total_tokens(self.run_id)

    def remaining_usd(self) -> float:
        """Headroom under the USD ceiling. Always non-negative.

        Returns ``inf`` for unmetered modes so callers that pass the value to
        the SDK as ``max_budget_usd`` don't accidentally cap an unmetered call.
        """
        if not self.enforce_usd:
            return float("inf")
        return max(0.0, self.ceiling.max_usd - self.spent())

    def would_exceed(self, estimated_usd: float) -> bool:
        """Pre-call check: would adding ``estimated_usd`` blow the ceiling?

        Always ``False`` in unmetered modes. Use this to skip a substage cleanly
        before incurring the cost, instead of catching a breach after the fact.
        """
        if not self.enforce_usd:
            return False
        return (self.spent() + max(0.0, estimated_usd)) > self.ceiling.max_usd

    def snapshot(self) -> MeterSnapshot:
        """Frozen view for logging between stages."""
        return MeterSnapshot(
            spent_usd=self.spent(),
            tokens_used=self.tokens_used(),
            remaining_usd=self.remaining_usd(),
            enforce_usd=self.enforce_usd,
        )

    def check(self) -> None:
        """Raise ``BudgetExceeded`` if a hard ceiling is breached.

        The token ceiling is always enforced. The USD ceiling is enforced only
        when ``enforce_usd`` was set at construction time.
        """
        tokens = self.tokens_used()
        if tokens > self.ceiling.max_tokens:
            raise BudgetExceeded(
                spent_usd=self.spent(),
                ceiling_usd=self.ceiling.max_usd,
                tokens=tokens,
            )
        if self.enforce_usd:
            spent = self.spent()
            if spent > self.ceiling.max_usd:
                raise BudgetExceeded(
                    spent_usd=spent,
                    ceiling_usd=self.ceiling.max_usd,
                    tokens=tokens,
                )
