"""Critique stage — independent review of the implement diff against the spec.

Critical: this stage spawns a **fresh** Claude session that has never seen the
implement context. Independent review is the whole point — sharing context
defeats it. We pass only the spec text and the diff.

The verdict is one of:

- ``pass`` → open PR normally.
- ``pass-with-concerns`` → open PR as draft + ``smithic-needs-review`` label.
- ``revise`` → orchestrator hands feedback back to implement and re-runs.
- ``abort`` → orchestrator marks the run aborted, no PR.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from smithic.budget.exceptions import AbortRun
from smithic.budget.meter import Meter
from smithic.stages.introspect import IntrospectionReport
from smithic.types.critique import CriticVerdict

_CRITIC_SYSTEM_PROMPT = """You are an independent code reviewer.

You have NEVER seen the implementation work — only the spec and the resulting diff.
Your verdict is one of: pass, pass-with-concerns, revise, abort.

- pass: implementation matches the spec; no concerns.
- pass-with-concerns: ships, but with caveats a human reviewer should know.
- revise: clear gaps or bugs. The agent should be given another shot.
- abort: fundamentally wrong direction; do not ship.

Score `spec_adherence` and `convention_drift` in [0, 1] — convention_drift = 1.0 means
the diff respects the repo's existing patterns; 0.0 means it ignored them.

Map issues to spec sections or files where possible. Use the file_hint field for that.

Return JSON matching the CriticVerdict schema you've been given via output_format.
Do not output anything besides JSON."""


# Truncation limits — keep the diff/spec from blowing the context window.
MAX_DIFF_CHARS = 60_000
MAX_SPEC_CHARS = 12_000


def read_diff(worktree_path: Path, base_branch: str) -> str:
    """Run ``git diff <base>...HEAD`` against the worktree.

    Falls back to a plain ``git diff HEAD`` if the three-dot form fails (e.g.
    when the base branch isn't fetched in the worktree's local refs).
    """
    # Windows subprocess defaults to cp1252 for text=True, which crashes on any
    # non-mappable byte (emoji, smart quotes, anything UTF-8) — and the diff
    # of a real repo will have those. Force utf-8 with replace fallback so a
    # weird byte never blows up the pipeline.
    #
    # IMPORTANT: diff from ``origin/<base>``, not just ``<base>``. The worktree
    # was created off ``origin/<base>`` (see worktree/manager.py); the *local*
    # branch named ``<base>`` may be far behind, in which case ``base...HEAD``
    # surfaces every commit between local-base and origin-base in addition to
    # the implement's actual work. The critic's job is to review the implement,
    # not the whole intervening history — and that history can be hundreds of
    # files / hundreds of KB, which then truncates mid-UTF8 and crashes the
    # downstream Claude SDK subprocess that ingests it.
    candidates = [
        ["git", "diff", f"origin/{base_branch}...HEAD"],
        ["git", "diff", f"{base_branch}...HEAD"],
        ["git", "diff", "HEAD~1", "HEAD"],
    ]
    for cmd in candidates:
        result = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if result.returncode == 0 and (result.stdout or "").strip():
            return result.stdout
    return ""


def _truncate(text: str, max_chars: int, *, label: str) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 200]
    return f"{head}\n\n[... {label} truncated to fit context ...]"


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def _try_parse(text: str) -> CriticVerdict | None:
    raw = _strip_code_fences(text)
    if not raw:
        return None
    try:
        return CriticVerdict.model_validate_json(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return CriticVerdict.model_validate_json(raw[start : end + 1])
            except Exception:
                return None
        return None


def _extract_tokens(usage: object) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


@dataclass
class CritiqueResult:
    verdict: CriticVerdict
    cost_usd: float
    skipped: bool = False


def _build_options(
    *,
    meter: Meter,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
    stderr_buf: list[str],
) -> ClaudeAgentOptions:
    kwargs: dict[str, object] = {
        "system_prompt": _CRITIC_SYSTEM_PROMPT,
        # Critic reads the spec + diff (passed in the prompt) and produces JSON.
        # Matches research.py — see the rationale comment there.
        "permission_mode": "bypassPermissions",
        "max_turns": 4,
        "model": model,
        "output_format": {
            "type": "json_schema",
            "schema": CriticVerdict.model_json_schema(),
        },
        # Without a stderr callback the SDK's "Check stderr output for details"
        # is literal — there's no captured stderr to check. Pipe it.
        "stderr": stderr_buf.append,
    }
    remaining = meter.remaining_usd()
    if math.isfinite(remaining):
        kwargs["max_budget_usd"] = remaining
    if auth_env:
        kwargs["env"] = auth_env
    if cli_path:
        kwargs["cli_path"] = cli_path
    return ClaudeAgentOptions(**kwargs)


def _build_prompt(
    *,
    spec_text: str,
    diff_text: str,
    introspection: IntrospectionReport | None,
) -> str:
    parts = [
        "## Spec",
        "",
        _truncate(spec_text, MAX_SPEC_CHARS, label="spec"),
        "",
        "## Diff (git diff base...HEAD)",
        "",
        "```diff",
        _truncate(diff_text, MAX_DIFF_CHARS, label="diff"),
        "```",
        "",
    ]
    if introspection is not None:
        parts.extend(
            [
                "## Repo conventions (for convention_drift)",
                "",
                introspection.as_briefing(),
                "",
            ]
        )
    parts.append("Return your verdict as CriticVerdict JSON now.")
    return "\n".join(parts)


async def run_critique(
    *,
    spec_path: Path,
    worktree_path: Path,
    base_branch: str,
    introspection: IntrospectionReport | None,
    meter: Meter,
    auth_env: dict[str, str] | None = None,
    cli_path: str | None = None,
    model: str | None = None,
) -> CritiqueResult:
    """Spawn the critic, parse its verdict, return a ``CritiqueResult``."""
    spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    diff_text = read_diff(worktree_path, base_branch)
    if not diff_text.strip():
        # No diff means implement produced nothing useful — treat as abort,
        # don't bother spending money on a critic call.
        return CritiqueResult(
            verdict=CriticVerdict(
                verdict="abort",
                issues=[],
                spec_adherence=0.0,
                convention_drift=0.0,
                summary="implement stage produced an empty diff",
            ),
            cost_usd=0.0,
            skipped=True,
        )

    stderr_buf: list[str] = []
    options = _build_options(
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
        stderr_buf=stderr_buf,
    )
    prompt = _build_prompt(
        spec_text=spec_text, diff_text=diff_text, introspection=introspection
    )

    chunks: list[str] = []
    last_message_chunks: list[str] = []
    structured_output: object = None
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    session_id: str | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                msg_chunks: list[str] = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        msg_chunks.append(block.text)
                if msg_chunks:
                    chunks.extend(msg_chunks)
                    last_message_chunks = msg_chunks
            elif isinstance(message, ResultMessage):
                cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                session_id = getattr(message, "session_id", None)
                input_tokens, output_tokens = _extract_tokens(getattr(message, "usage", None))
                # When ``output_format={"type": "json_schema", ...}`` is set,
                # the SDK returns the schema-shaped response on ResultMessage's
                # ``structured_output`` field — NOT as a TextBlock. Real runs
                # against OnlyVAT confirmed the assistant message can be empty
                # while the structured payload is right here.
                so = getattr(message, "structured_output", None)
                if so is not None:
                    structured_output = so
    except Exception as exc:
        # Persist captured stderr so the next run has something actionable.
        debug_path = worktree_path / ".smithic" / "critique-stderr.txt"
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                "\n".join(stderr_buf) or "(no stderr captured)", encoding="utf-8"
            )
        except OSError:
            pass
        tail = "\n".join(stderr_buf[-40:]) if stderr_buf else "(no stderr captured)"
        raise RuntimeError(
            f"critique stage CLI subprocess failed: {exc}\n"
            f"--- captured stderr (last 40 lines) ---\n{tail}\n"
            f"--- full stderr in {debug_path} ---"
        ) from exc

    meter.record(
        "critique",
        cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
    )

    # Resolution order:
    # 1. Structured output from ResultMessage (when output_format=json_schema fires)
    # 2. Last assistant message text (typical for free-form responses)
    # 3. Full concatenation across messages (fallback)
    parsed: CriticVerdict | None = None
    if isinstance(structured_output, dict):
        try:
            parsed = CriticVerdict.model_validate(structured_output)
        except Exception:
            parsed = None

    if parsed is None:
        last_text = "\n".join(c for c in last_message_chunks if c)
        full_text = "\n".join(c for c in chunks if c)
        parsed = _try_parse(last_text) or _try_parse(full_text)
    else:
        last_text = ""
        full_text = ""

    if parsed is None:
        try:
            debug = worktree_path / ".smithic" / "critique-debug.txt"
            debug.parent.mkdir(parents=True, exist_ok=True)
            debug.write_text(
                "=== structured_output ===\n\n"
                f"{structured_output!r}\n\n"
                "=== last_message_text ===\n\n"
                f"{last_text or '(empty)'}\n\n"
                "=== full concatenated text ===\n\n"
                f"{full_text or '(empty)'}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        raise AbortRun(
            "critique stage returned non-JSON output — see critique-debug.txt"
        )
    return CritiqueResult(verdict=parsed, cost_usd=cost_usd)
