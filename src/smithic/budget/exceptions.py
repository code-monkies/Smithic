"""Exceptions for budget enforcement and run abort signaling."""


class BudgetExceeded(Exception):
    """Raised when a run blows its USD or token ceiling."""

    def __init__(self, *, spent_usd: float, ceiling_usd: float, tokens: int) -> None:
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd
        self.tokens = tokens
        super().__init__(
            f"budget exceeded: ${spent_usd:.4f} > ${ceiling_usd:.4f} ceiling "
            f"(used {tokens} tokens)"
        )


class AbortRun(Exception):
    """Raised when a stage decides the run should not continue (e.g., critic abort)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
