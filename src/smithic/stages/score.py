"""Score stage — pick a single candidate from ``ResearchFindings`` using a rubric.

Behavior:

1. Spawn a Claude session with structured output via the
   ``ScoringResult`` Pydantic schema.
2. Re-compute totals server-side from the per-axis scores using the rubric's
   weights — don't trust the model's arithmetic.
3. Apply the rubric thresholds to disqualify candidates and pick the highest
   surviving total. If none clears thresholds, return ``selected=None`` so
   the orchestrator can abort cleanly.

One retry on parse failure with the validation error appended to the prompt.
Second failure → ``AbortRun``.
"""

from __future__ import annotations

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

from smithic.budget.exceptions import AbortRun
from smithic.budget.meter import Meter
from smithic.rubric.schema import Rubric
from smithic.stages.introspect import IntrospectionReport
from smithic.types.research import (
    FeatureCandidate,
    ResearchFindings,
    ScoredCandidate,
    ScoringResult,
)

_SCORE_SYSTEM_PROMPT = """You score candidate features against a rubric on behalf of an automated pipeline.

You will be given:
- A ResearchFindings JSON document with candidate features and their evidence
- A rubric with named axes, weights, and thresholds
- Repo introspection context (treat as implementation context, not market evidence)

For each candidate, score every axis in [0.0, 1.0] and write a 1-sentence rationale.
Be honest — disqualify candidates that fail any axis below the rubric's min_per_axis.

Return JSON matching the ScoringResult schema you've been given via output_format.
Do not output anything besides JSON."""


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
class _ScoreCallResult:
    text: str
    structured_output: object  # Populated when output_format=json_schema fires.
    cost_usd: float
    input_tokens: int
    output_tokens: int
    session_id: str | None


async def _call_scorer(
    prompt: str,
    options: ClaudeAgentOptions,
) -> _ScoreCallResult:
    chunks: list[str] = []
    structured_output: object = None
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    session_id: str | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
            session_id = getattr(message, "session_id", None)
            input_tokens, output_tokens = _extract_tokens(getattr(message, "usage", None))
            # See stages/critique.py — when output_format=json_schema is set,
            # the SDK puts the response on ResultMessage.structured_output and
            # may emit zero TextBlocks. Real OnlyVAT runs confirmed this.
            so = getattr(message, "structured_output", None)
            if so is not None:
                structured_output = so
    return _ScoreCallResult(
        text="\n".join(c for c in chunks if c),
        structured_output=structured_output,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
    )


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def _try_parse(text: str) -> tuple[ScoringResult | None, str]:
    raw = _strip_code_fences(text)
    if not raw:
        return None, "empty model response"
    try:
        return ScoringResult.model_validate_json(raw), ""
    except Exception as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return ScoringResult.model_validate_json(raw[start : end + 1]), ""
            except Exception as exc2:
                return None, str(exc2)
        return None, str(exc)


def _try_parse_with_structured(call: _ScoreCallResult) -> tuple[ScoringResult | None, str]:
    """Resolve the call's output to a ``ScoringResult``, preferring the SDK's
    ``structured_output`` field over text. See critique.py for the rationale —
    real OnlyVAT runs returned empty TextBlocks while the schema-shaped
    response sat on ResultMessage.structured_output.
    """
    if isinstance(call.structured_output, dict):
        try:
            return ScoringResult.model_validate(call.structured_output), ""
        except Exception as exc:
            return None, f"structured_output validation: {exc}"
    return _try_parse(call.text)


def _dump_score_debug(
    out_dir: Path | None,
    first: _ScoreCallResult,
    second: _ScoreCallResult,
    err1: str,
    err2: str,
) -> None:
    """Write both scoring attempts' raw output to disk on double-failure.

    Best-effort. Same artifact pattern as research's synth-debug and
    critique's critique-debug — the next iteration shouldn't have to pay
    another live run just to learn what shape the model returned.
    """
    if out_dir is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        body = (
            "=== attempt 1 ===\n"
            f"validation error: {err1}\n\n"
            f"structured_output:\n{first.structured_output!r}\n\n"
            f"text:\n{first.text or '(empty)'}\n\n"
            "=== attempt 2 (with retry prompt) ===\n"
            f"validation error: {err2}\n\n"
            f"structured_output:\n{second.structured_output!r}\n\n"
            f"text:\n{second.text or '(empty)'}\n"
        )
        (out_dir / "score-debug.txt").write_text(body, encoding="utf-8")
    except OSError:
        pass


def _build_options(
    *,
    meter: Meter,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
) -> ClaudeAgentOptions:
    kwargs: dict[str, object] = {
        "system_prompt": _SCORE_SYSTEM_PROMPT,
        # Score subagent doesn't need tools but matches the rest of the suite
        # — see research.py for the rationale.
        "permission_mode": "bypassPermissions",
        "max_turns": 4,
        "model": model,
        "output_format": {
            "type": "json_schema",
            "schema": ScoringResult.model_json_schema(),
        },
    }
    remaining = meter.remaining_usd()
    if math.isfinite(remaining):
        kwargs["max_budget_usd"] = remaining
    if auth_env:
        kwargs["env"] = auth_env
    if cli_path:
        kwargs["cli_path"] = cli_path
    return ClaudeAgentOptions(**kwargs)


def _diversity_block(previously_selected: list[str]) -> str:
    """Build the soft-nudge block that points the scorer away from sibling picks.

    Empty when no siblings have selected anything yet — keeps the prompt clean
    in the single-run case.
    """
    titles = [t.strip() for t in previously_selected if t and t.strip()]
    if not titles:
        return ""
    bulleted = "\n".join(f"  - {t}" for t in titles)
    return (
        "## Sibling diversity (soft preference)\n"
        "\n"
        "Other parallel runs in this swarm have already selected:\n"
        f"{bulleted}\n"
        "\n"
        "If two candidates' totals are within 0.05 of each other, prefer the one most\n"
        "different from the already-selected list. Never pick a disqualified candidate.\n"
        "Rubric thresholds always win — better no PR than a duplicate."
    )


def _build_prompt(
    *,
    findings: ResearchFindings,
    rubric: Rubric,
    introspection: IntrospectionReport | None,
    extra: str | None = None,
    previously_selected: list[str] | None = None,
) -> str:
    parts = [
        "ResearchFindings:",
        "```json",
        findings.model_dump_json(indent=2),
        "```",
        "",
        rubric.as_prompt_block(),
        "",
    ]
    if introspection is not None:
        parts.append("## Implementation context (NOT market evidence)")
        parts.append("")
        parts.append(introspection.as_briefing())
        parts.append("")
    diversity = _diversity_block(previously_selected or [])
    if diversity:
        parts.append(diversity)
        parts.append("")
    parts.append(
        "Score every candidate. Pick the highest-total non-disqualified candidate as `selected`."
    )
    if extra:
        parts.append("")
        parts.append(extra)
    return "\n".join(parts)


def _recompute(scored: ScoredCandidate, rubric: Rubric) -> ScoredCandidate:
    """Recompute total + disqualification flags using the trusted rubric weights."""
    by_name = {a.axis: a for a in scored.axes}
    total = 0.0
    disqualified = scored.disqualified
    reason: str | None = scored.disqualification_reason
    for name, axis in rubric.axes.items():
        a = by_name.get(name)
        if a is None:
            disqualified = True
            reason = reason or f"missing score for axis {name!r}"
            continue
        total += a.score * axis.weight
        if a.score < rubric.thresholds.min_per_axis:
            disqualified = True
            reason = reason or f"axis {name!r} scored {a.score:.2f} < {rubric.thresholds.min_per_axis}"
    if total < rubric.thresholds.min_total:
        disqualified = True
        reason = reason or f"total {total:.3f} < min_total {rubric.thresholds.min_total}"
    total = max(0.0, min(1.0, total))
    return scored.model_copy(
        update={"total": total, "disqualified": disqualified, "disqualification_reason": reason}
    )


def _select_winner(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    survivors = [s for s in scored if not s.disqualified]
    if not survivors:
        return None
    return max(survivors, key=lambda s: s.total)


def apply_rubric(scoring: ScoringResult, rubric: Rubric) -> ScoringResult:
    """Re-score every candidate server-side using the trusted rubric."""
    fixed = [_recompute(s, rubric) for s in scoring.scored]
    winner = _select_winner(fixed)
    abort_reason: str | None = None
    if winner is None:
        abort_reason = (
            "no candidate cleared the rubric thresholds "
            f"(min_total={rubric.thresholds.min_total}, "
            f"min_per_axis={rubric.thresholds.min_per_axis})"
        )
    return ScoringResult(scored=fixed, selected=winner, abort_reason=abort_reason)


@dataclass
class ScoreResult:
    scoring: ScoringResult
    cost_usd: float


async def run_score(
    *,
    findings: ResearchFindings,
    rubric: Rubric,
    introspection: IntrospectionReport | None,
    meter: Meter,
    auth_env: dict[str, str] | None = None,
    cli_path: str | None = None,
    model: str | None = None,
    previously_selected: list[str] | None = None,
    out_dir: Path | None = None,
) -> ScoreResult:
    """Score the candidates and pick a winner. Raises ``AbortRun`` on parse failure.

    ``previously_selected`` is a list of titles already chosen by sibling runs
    in the same swarm. When non-empty, the scoring prompt soft-nudges the
    model away from those picks. Disqualification thresholds still apply.

    ``out_dir`` is where ``score-debug.txt`` is written when both attempts
    fail to parse — same artifact pattern as research and critique.
    """
    options = _build_options(meter=meter, auth_env=auth_env, cli_path=cli_path, model=model)
    prompt = _build_prompt(
        findings=findings,
        rubric=rubric,
        introspection=introspection,
        previously_selected=previously_selected,
    )

    first = await _call_scorer(prompt, options)
    parsed, err = _try_parse_with_structured(first)
    cost = first.cost_usd
    in_tokens = first.input_tokens
    out_tokens = first.output_tokens

    if parsed is None:
        retry_prompt = (
            f"{prompt}\n\nYour previous response failed validation: {err}\n"
            "Return strictly valid JSON conforming to the ScoringResult schema."
        )
        second = await _call_scorer(retry_prompt, options)
        parsed, err2 = _try_parse_with_structured(second)
        cost += second.cost_usd
        in_tokens += second.input_tokens
        out_tokens += second.output_tokens
        if parsed is None:
            meter.record(
                "score",
                cost,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                session_id=second.session_id,
            )
            _dump_score_debug(out_dir, first, second, err, err2)
            raise AbortRun(
                f"score stage failed to produce valid JSON twice: {err2} "
                "— see score-debug.txt for raw model output"
            )

    meter.record(
        "score",
        cost,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        session_id=first.session_id,
    )
    return ScoreResult(scoring=apply_rubric(parsed, rubric), cost_usd=cost)


def feature_from_selection(selected: ScoredCandidate) -> tuple[str, str]:
    """Return ``(feature_seed, rationale)`` for the spec stage."""
    feature_seed = selected.candidate.title.strip()
    rationale_lines = [
        f"**Title:** {feature_seed}",
        f"**Description:** {selected.candidate.description.strip()}",
        f"**Inferred user pain:** {selected.candidate.inferred_user_pain.strip()}",
        f"**Total score:** {selected.total:.3f}",
        "",
        "Per-axis breakdown:",
    ]
    for a in selected.axes:
        rationale_lines.append(f"- _{a.axis}_ ({a.score:.2f}): {a.rationale}")
    rationale_lines.append("")
    rationale_lines.append("Top supporting evidence:")
    for ev in selected.candidate.evidence[:5]:
        rationale_lines.append(f"- [{ev.title}]({ev.url}) — {ev.source}")
    return feature_seed, "\n".join(rationale_lines)


def write_score_artifact(scoring: ScoringResult, out_dir: Path) -> Path:
    """Drop a ``score.json`` next to ``research.json`` so reviewers can audit."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "score.json"
    path.write_text(scoring.model_dump_json(indent=2), encoding="utf-8")
    return path


# Re-export for tests / orchestrator convenience.
__all__ = [
    "FeatureCandidate",
    "ScoreResult",
    "apply_rubric",
    "feature_from_selection",
    "run_score",
    "write_score_artifact",
]
