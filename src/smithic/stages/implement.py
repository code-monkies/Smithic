"""The implement stage delegates the actual coding work to a Claude session.

This is the load-bearing module for v0.1 — everything else is plumbing around
this call. We:

1. Build a system prompt that tells Claude it's running inside a worktree and
   must implement the feature described in ``.smithic/spec.md``.
2. Spawn a ``query()`` against the SDK with ``cwd`` set to the worktree and
   the resolved auth env / CLI path injected.
3. Bound the run by ``max_budget_usd`` (only meaningful in API mode) and a
   conservative ``max_turns``.
4. Stream messages, log token/cost events through the Meter, and capture the
   final ``ResultMessage`` for the orchestrator.

The stage never touches the target repo's main working tree — it only writes
inside the worktree path it was given.
"""

from __future__ import annotations

import contextlib
import math
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

_SYSTEM_PROMPT_TEMPLATE = """You are an implementation agent operating inside an isolated git worktree.

**Worktree (your sandbox)**: `{worktree_path}`

This is your `cwd`. Every Read/Edit/Write/Bash tool call you make must stay
inside this directory. Use relative paths or paths that start with the
worktree path above. **Never use an absolute path to a parent repository,
even if a spec, briefing, or CLAUDE.md mentions one** — those references
exist for context, not for editing.

A spec for the feature you are implementing is at `.smithic/spec.md` (relative
to the worktree) — read it first.

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
etc.). Run `git` from inside the worktree only.

Do NOT push the branch. Do NOT open a PR. Do NOT touch anything outside this
worktree directory. Smithic's orchestrator handles those steps after you exit.

When you are done, output a brief summary of what you changed and any caveats
the human reviewer should know about."""


def _build_system_prompt(worktree_path: Path) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(worktree_path=worktree_path)


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
    auth_env: dict[str, str] | None = None,
    cli_path: str | None = None,
    revise_feedback: str | None = None,
) -> ImplementResult:
    """Spawn the Claude implementation session inside ``worktree_path``.

    On a critic-driven revise loop, ``revise_feedback`` is the critic's summary
    + issues; we prepend it to the user prompt so the agent picks up where it
    left off rather than restarting.
    """
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

    # `max_budget_usd` is only meaningful in API mode. For unmetered modes the
    # meter returns inf and we drop the kwarg so the SDK doesn't see a value
    # it would try to enforce against $0 cost reports.
    # Capture CLI stderr so a subprocess crash doesn't surface as "Check stderr
    # output for details" with no actual stderr in sight. The SDK's default
    # ``stderr`` is None — without this, a CLI exit-1 leaves us blind.
    stderr_buf: list[str] = []

    def _capture_stderr(line: str) -> None:
        stderr_buf.append(line)

    options_kwargs: dict[str, object] = dict(
        cwd=str(worktree_path),
        system_prompt=_build_system_prompt(worktree_path),
        max_turns=max_turns,
        # Implement is sandboxed inside the worktree (cwd above). It needs
        # Read/Write/Edit/Bash to actually do the work. acceptEdits gates
        # everything except file edits, which broke real runs against MCP-using
        # stages — see research.py for the full rationale.
        permission_mode="bypassPermissions",
        model=model,
        stderr=_capture_stderr,
    )
    if math.isfinite(remaining_usd):
        options_kwargs["max_budget_usd"] = remaining_usd
    if auth_env:
        options_kwargs["env"] = auth_env
    if cli_path:
        options_kwargs["cli_path"] = cli_path

    options = ClaudeAgentOptions(**options_kwargs)

    prompt_parts: list[str] = []
    if revise_feedback:
        prompt_parts.append(revise_feedback.strip())
        prompt_parts.append(
            "Address the issues above on top of the existing diff in this worktree. "
            "Do not redo work that already passes — focus on the points the critic raised. "
            "Commit the updated changes when done."
        )
    else:
        prompt_parts.append(f"The feature to implement: {feature.strip()}")
        prompt_parts.append(
            "Begin by reading `.smithic/spec.md` and surveying the repo structure."
        )
    prompt = "\n\n".join(prompt_parts)

    summary_chunks: list[str] = []
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    session_id: str | None = None
    num_turns = 0

    # Stream-log every assistant turn to disk *as it arrives*. When the SDK
    # CLI subprocess dies mid-iteration (silent exit-1 — see the run-1e59a7
    # incident in PR #2's description), in-memory ``summary_chunks`` go with
    # it. Persisting per-turn means the next debugger has the agent's actual
    # narrative even when stderr is empty.
    progress_path = worktree_path / ".smithic" / "implement-progress.txt"
    progress_fp = None
    try:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_fp = progress_path.open("w", encoding="utf-8")
        progress_fp.write(f"=== implement stage @ {worktree_path} ===\n\n")
        progress_fp.flush()
    except OSError:
        progress_fp = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                num_turns += 1
                turn_text_parts: list[str] = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        summary_chunks.append(block.text)
                        turn_text_parts.append(block.text)
                if progress_fp is not None and turn_text_parts:
                    try:
                        progress_fp.write(f"--- turn {num_turns} ---\n")
                        progress_fp.write("\n".join(turn_text_parts))
                        progress_fp.write("\n\n")
                        progress_fp.flush()
                    except OSError:
                        pass
            elif isinstance(message, ResultMessage):
                cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                session_id = getattr(message, "session_id", None)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    except Exception as exc:
        # Dump captured stderr to disk so the next invocation has something to
        # read besides "Check stderr output for details". Best-effort.
        debug_path = worktree_path / ".smithic" / "implement-stderr.txt"
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                "\n".join(stderr_buf) or "(no stderr captured)", encoding="utf-8"
            )
        except OSError:
            pass
        if progress_fp is not None:
            try:
                progress_fp.write(f"--- crash after turn {num_turns} ---\n{exc!r}\n")
                progress_fp.flush()
            except OSError:
                pass
        # Re-raise with the captured stderr appended so the orchestrator's
        # error path surfaces something actionable.
        tail = "\n".join(stderr_buf[-40:]) if stderr_buf else "(no stderr captured)"
        raise RuntimeError(
            f"implement stage CLI subprocess failed: {exc}\n"
            f"--- captured stderr (last 40 lines) ---\n{tail}\n"
            f"--- full stderr in {debug_path} ---\n"
            f"--- per-turn agent narrative in {progress_path} ---"
        ) from exc
    finally:
        if progress_fp is not None:
            with contextlib.suppress(OSError):
                progress_fp.close()

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
