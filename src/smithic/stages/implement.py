"""The implement stage delegates the actual coding work to a Claude session.

This is the load-bearing module for v0.1 — everything else is plumbing around
this call. We:

1. Build a system prompt that tells Claude it's running inside a worktree and
   must implement the feature described in ``.smithic/spec.md``.
2. Spawn a ``query()`` against the SDK with ``cwd`` set to the worktree.
3. Bound the run by ``max_budget_usd`` (the SDK's per-call ceiling) and a
   conservative ``max_turns``.
4. Stream messages, log token/cost events through the Meter, and capture the
   final ``ResultMessage`` for the orchestrator.

The stage never touches the target repo's main working tree — it only writes
inside the worktree path it was given.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from smithic.budget.meter import Meter

_SYSTEM_PROMPT = """You are an implementation agent operating inside an isolated git worktree.

A spec for the feature you are implementing is at `.smithic/spec.md` — read it first.

Your job:

1. Read the spec carefully.
2. Read enough of the surrounding codebase to understand existing conventions
   (file layout, naming, error handling, test patterns).
3. Implement the feature with the smallest reasonable diff.
4. Add or update tests. If no test framework is configured for the project,
   add a minimal smoke verification rather than skipping verification entirely.
5. Run the project's tests. If they fail because of pre-existing issues
   unrelated to your change, note that in your final summary rather than
   trying to fix them.
6. Keep the change focused. Do not refactor adjacent code or rename things.

You MUST commit your changes via `git commit` before you finish. Use a single
clear commit message in conventional-commits style (`feat: ...`, `fix: ...`,
etc.).

Do NOT push the branch. Do NOT open a PR. Do NOT touch anything outside this
worktree directory. Smithic's orchestrator handles those steps after you exit.

When you are done, output a brief summary of what you changed and any caveats
the human reviewer should know about."""


@dataclass
class ImplementResult:
    succeeded: bool
    summary: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    session_id: str | None
    num_turns: int


async def run_implementation(
    *,
    worktree_path: Path,
    feature: str,
    meter: Meter,
    model: str | None = None,
    max_turns: int = 40,
) -> ImplementResult:
    """Spawn the Claude implementation session inside ``worktree_path``."""
    remaining_usd = meter.remaining_usd()
    if remaining_usd <= 0:
        return ImplementResult(
            succeeded=False,
            summary="budget exhausted before implement stage started",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            session_id=None,
            num_turns=0,
        )

    options = ClaudeAgentOptions(
        cwd=str(worktree_path),
        system_prompt=_SYSTEM_PROMPT,
        max_turns=max_turns,
        max_budget_usd=remaining_usd,
        permission_mode="acceptEdits",
        model=model,
    )

    prompt = (
        f"The feature to implement: {feature.strip()}\n\n"
        "Begin by reading `.smithic/spec.md` and surveying the repo structure."
    )

    summary_chunks: list[str] = []
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    session_id: str | None = None
    num_turns = 0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            num_turns += 1
            for block in message.content:
                if isinstance(block, TextBlock):
                    summary_chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
            session_id = getattr(message, "session_id", None)
            usage = getattr(message, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    meter.record(
        "implement",
        cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
    )

    summary = "\n\n".join(chunk.strip() for chunk in summary_chunks if chunk.strip())
    return ImplementResult(
        succeeded=bool(num_turns) and bool(summary),
        summary=summary or "(no summary returned)",
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
        num_turns=num_turns,
    )
