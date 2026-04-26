"""Per-run cost and token accounting with hard-ceiling enforcement.

The Claude Agent SDK has its own ``max_budget_usd`` ceiling we plumb through, but
we keep an out-of-band ledger here for two reasons:

1. We want to know exactly what we spent per stage and per session.
2. The SDK's ceiling is per-call; we need a per-run ceiling that survives multiple
   subagent invocations.

In v0.1 the meter records everything but the only ``check`` callsite is between
stages — there is no mid-call interrupt. v0.2 will tighten this.
"""

from __future__ import annotations

from dataclasses import dataclass

from smithic.budget.exceptions import BudgetExceeded
from smithic.memory.db import Memory


@dataclass(frozen=True)
class BudgetCeiling:
    max_usd: float
    max_tokens: int


class Meter:
    def __init__(self, memory: Memory, run_id: str, ceiling: BudgetCeiling) -> None:
        self.memory = memory
        self.run_id = run_id
        self.ceiling = ceiling

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
        return max(0.0, self.ceiling.max_usd - self.spent())

    def check(self) -> None:
        """Raise ``BudgetExceeded`` if either ceiling is breached."""
        spent = self.spent()
        tokens = self.tokens_used()
        if spent > self.ceiling.max_usd or tokens > self.ceiling.max_tokens:
            raise BudgetExceeded(
                spent_usd=spent, ceiling_usd=self.ceiling.max_usd, tokens=tokens
            )
