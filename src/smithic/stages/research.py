"""Research stage — pull market signal, return ``ResearchFindings``.

The research stage runs only when ``--feature`` is omitted. It:

1. Asks a small Claude subagent to derive 3–5 search queries from the mission
   + introspection report.
2. Fans those queries across MCP-backed sources (Tavily / Reddit / fetch).
3. Asks another Claude subagent to synthesize the raw evidence into 3–8
   ``FeatureCandidate`` entries with deduplicated supporting evidence.
4. Writes ``.smithic/research.md`` (human-readable) and
   ``.smithic/research.json`` (Pydantic-encoded) to the worktree.

If zero sources are reachable the stage raises ``AbortRun`` so the orchestrator
can mark the run aborted cleanly rather than producing a fictional spec.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
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
from smithic.config import ResearchConfig
from smithic.mcp.registry import build_mcp_servers, web_source_label
from smithic.memory.cache import ResearchCache
from smithic.stages.introspect import IntrospectionReport
from smithic.telemetry.logger import event
from smithic.types.research import ResearchFindings

_QUERY_SYSTEM_PROMPT = """You generate search queries that surface real user pain about a software project.

You will be given:
- The project's mission statement
- A short briefing on the repo's stack and conventions

Return a JSON object with the schema:
  {"queries": ["query1", "query2", ...]}

Rules:
- 3 to 5 queries.
- Each query should be a real phrase a frustrated user might type into Google or Reddit.
- Mix specific complaints ("FastAPI rate limiting nightmare") with category searches.
- Do not include the project's own name unless the user would actually search for it.
- Do not output anything besides the JSON object."""

_SYNTH_SYSTEM_PROMPT = """You distill raw market-signal evidence into specific feature candidates.

You will be given the research evidence and the project mission. Your job:

1. Group related evidence into 3–8 distinct feature candidates.
2. For each candidate, pick the 3–8 strongest evidence items already present in the input.
3. Write a 1–3 sentence description and a one-sentence "inferred user pain" line.
4. Title each candidate as an imperative phrase, ≤80 characters.

Return a JSON object matching the ResearchFindings schema you've been given via output_format.
Do not invent evidence. Do not output anything besides JSON."""


def _extract_tokens(usage: object) -> tuple[int, int]:
    """Read input/output token counts whether ``usage`` is a dict or an object."""
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


@dataclass(frozen=True)
class _ClaudeCallResult:
    text: str  # Concatenation of every TextBlock across every AssistantMessage.
    last_message_text: str  # Just the final AssistantMessage's text — where structured-output JSON lives.
    structured_output: object  # Populated when output_format=json_schema fires (see critique.py).
    cost_usd: float
    input_tokens: int
    output_tokens: int
    session_id: str | None


async def _call_claude(
    *,
    prompt: str,
    options: ClaudeAgentOptions,
) -> _ClaudeCallResult:
    """One-shot Claude call: collect the text output and cost summary.

    With MCP tool use, Claude emits prose interleaved with tool calls — every
    "Let me search Reddit..." between tool invocations becomes another
    ``TextBlock`` in another ``AssistantMessage``. The final synthesized JSON
    is in the *last* assistant message, not the concatenation of all of them,
    so we track ``last_message_text`` separately. The parser tries that first
    and falls back to the full concatenation if needed.
    """
    chunks: list[str] = []
    last_message_chunks: list[str] = []
    structured_output: object = None
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    session_id: str | None = None
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
            so = getattr(message, "structured_output", None)
            if so is not None:
                structured_output = so
    return _ClaudeCallResult(
        text="\n".join(c for c in chunks if c),
        last_message_text="\n".join(c for c in last_message_chunks if c),
        structured_output=structured_output,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        session_id=session_id,
    )


def _options_kwargs(
    *,
    meter: Meter,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        # Smithic runs unattended; the SDK call is sandboxed inside a worktree
        # (or, for research, outside any worktree at all and read-only). With
        # acceptEdits the model gets permission-denied on every MCP tool call
        # — see synth-debug-* artifacts when this fails. bypassPermissions is
        # the right setting for "I trust this SDK call to do what its prompt
        # says and nothing else."
        "permission_mode": "bypassPermissions",
        "max_turns": 8,
        "model": model,
    }
    remaining = meter.remaining_usd()
    if math.isfinite(remaining):
        kwargs["max_budget_usd"] = remaining
    if auth_env:
        kwargs["env"] = auth_env
    if cli_path:
        kwargs["cli_path"] = cli_path
    if extra:
        kwargs.update(extra)
    return kwargs


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # Drop opening fence (with optional language).
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def _parse_queries(text: str) -> list[str]:
    raw = _strip_code_fences(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list):
        return []
    return [str(q).strip() for q in queries if isinstance(q, str | int) and str(q).strip()]


async def _generate_queries(
    *,
    mission: str,
    introspection: IntrospectionReport,
    research_cfg: ResearchConfig,
    meter: Meter,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
) -> tuple[list[str], _ClaudeCallResult]:
    if meter.would_exceed(research_cfg.query_budget_usd):
        return [], _ClaudeCallResult(text="", cost_usd=0.0, input_tokens=0, output_tokens=0, session_id=None)

    options = ClaudeAgentOptions(
        **_options_kwargs(
            meter=meter,
            auth_env=auth_env,
            cli_path=cli_path,
            model=model,
            extra={"system_prompt": _QUERY_SYSTEM_PROMPT},
        )
    )
    prompt = (
        "Mission:\n\n"
        f"{mission.strip()}\n\n"
        "Repo briefing:\n\n"
        f"{introspection.as_briefing()}\n\n"
        "Output JSON now."
    )
    result = await _call_claude(prompt=prompt, options=options)
    queries = _parse_queries(result.last_message_text) or _parse_queries(result.text)
    return queries, result


def _options_for_synth(
    *,
    meter: Meter,
    research_cfg: ResearchConfig,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
    mcp_servers: dict[str, dict[str, object]],
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        **_options_kwargs(
            meter=meter,
            auth_env=auth_env,
            cli_path=cli_path,
            model=model,
            extra={
                "system_prompt": _SYNTH_SYSTEM_PROMPT,
                "mcp_servers": mcp_servers,
                "max_turns": 30,
                "output_format": {
                    "type": "json_schema",
                    "schema": ResearchFindings.model_json_schema(),
                },
            },
        )
    )


async def _synthesize_findings(
    *,
    queries: list[str],
    mission: str,
    research_cfg: ResearchConfig,
    meter: Meter,
    auth_env: dict[str, str] | None,
    cli_path: str | None,
    model: str | None,
    mcp_servers: dict[str, dict[str, object]],
) -> tuple[ResearchFindings | None, _ClaudeCallResult]:
    options = _options_for_synth(
        meter=meter,
        research_cfg=research_cfg,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
        mcp_servers=mcp_servers,
    )
    prompt = (
        "Use the available MCP search tools to gather evidence for each of the queries below.\n"
        "Then synthesize the evidence into ResearchFindings JSON.\n\n"
        "Mission:\n\n"
        f"{mission.strip()}\n\n"
        "Queries:\n"
        + "\n".join(f"- {q}" for q in queries)
        + "\n\n"
        f"Aim for {research_cfg.max_candidates} candidates with at least 3 evidence items each."
    )
    result = await _call_claude(prompt=prompt, options=options)
    # Resolution order: structured_output (when output_format fires), last
    # assistant message text, then full concatenation. See critique.py for
    # why structured_output takes priority.
    parsed: ResearchFindings | None = None
    if isinstance(result.structured_output, dict):
        try:
            parsed = ResearchFindings.model_validate(result.structured_output)
        except Exception:
            parsed = None
    if parsed is None:
        parsed = _try_parse_findings(result.last_message_text) or _try_parse_findings(result.text)
    return parsed, result


def _try_parse_findings(text: str) -> ResearchFindings | None:
    raw = _strip_code_fences(text)
    if not raw:
        return None
    try:
        return ResearchFindings.model_validate_json(raw)
    except Exception:
        # Tolerate models that wrap the JSON in a sentence — try to find the
        # outermost {...} block and re-parse.
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return ResearchFindings.model_validate_json(raw[start : end + 1])
        except Exception:
            return None


def _dump_synth_debug(
    out_dir: Path, run_id: str, full_text: str, last_text: str
) -> None:
    """Write the synthesis call's raw output to disk so failures are inspectable.

    Best-effort — caller is already on an abort path, never raise from here.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        body = (
            "=== last_message_text (where JSON should live) ===\n\n"
            f"{last_text or '(empty)'}\n\n"
            "=== full concatenated text (every assistant message) ===\n\n"
            f"{full_text or '(empty)'}\n"
        )
        (out_dir / f"synth-debug-{run_id}.txt").write_text(body, encoding="utf-8")
    except Exception:
        pass


def _render_findings_md(findings: ResearchFindings, *, run_id: str) -> str:
    lines = [
        "# Smithic research brief",
        "",
        f"- **Run ID**: `{run_id}`",
        f"- **Generated**: {datetime.now(UTC).isoformat()}",
        f"- **Sources used**: {', '.join(findings.sources_used) or '(none)'}",
        f"- **Queries run**: {len(findings.queries_run)}",
        "",
    ]
    for i, q in enumerate(findings.queries_run, start=1):
        lines.append(f"  {i}. {q}")
    lines.extend(["", "## Candidates", ""])
    for cand in findings.candidates:
        lines.append(f"### {cand.title}")
        lines.append("")
        lines.append(cand.description.strip())
        lines.append("")
        lines.append(f"**Inferred user pain:** {cand.inferred_user_pain.strip()}")
        lines.append("")
        lines.append("**Evidence:**")
        for ev in cand.evidence:
            posted = f" ({ev.posted_at.date().isoformat()})" if ev.posted_at else ""
            lines.append(f"- [{ev.title}]({ev.url}) — {ev.source}{posted}")
            if ev.snippet:
                lines.append(f"  > {ev.snippet[:300]}")
        lines.append("")
    return "\n".join(lines)


def write_research_artifacts(
    *,
    findings: ResearchFindings,
    out_dir: Path,
    run_id: str,
) -> Path:
    """Write ``research.md`` + ``research.json`` to ``out_dir`` and return the markdown path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "research.md"
    md_path.write_text(_render_findings_md(findings, run_id=run_id), encoding="utf-8")
    json_path = out_dir / "research.json"
    json_path.write_text(findings.model_dump_json(indent=2), encoding="utf-8")
    return md_path


@dataclass
class ResearchResult:
    findings: ResearchFindings
    brief_path: Path
    cost_usd: float
    cache_hit: bool = False


async def run_research(
    *,
    mission: str,
    introspection: IntrospectionReport,
    research_cfg: ResearchConfig,
    out_dir: Path,
    run_id: str,
    meter: Meter,
    auth_env: dict[str, str] | None = None,
    cli_path: str | None = None,
    model: str | None = None,
    cache: ResearchCache | None = None,
    target_path: Path | None = None,
) -> ResearchResult:
    """Execute the research stage. Raises ``AbortRun`` on hard failure.

    ``cache`` + ``target_path`` are used by v0.3 swarm runs to share
    synthesized findings across siblings. The cache key is
    ``(target_path, normalized_query_set)`` — when the second sibling's query
    generator produces a query set that matches the first sibling's, we skip
    the (expensive) synthesis call entirely.

    Single runs without ``cache`` set behave exactly like v0.2.
    """
    mcp_servers = build_mcp_servers(research_cfg.sources)
    if not mcp_servers:
        raise AbortRun(
            "no research sources reachable: configure [research].sources or set TAVILY_API_KEY"
        )

    queries, query_call = await _generate_queries(
        mission=mission,
        introspection=introspection,
        research_cfg=research_cfg,
        meter=meter,
        auth_env=auth_env,
        cli_path=cli_path,
        model=model,
    )
    meter.record(
        "research:queries",
        query_call.cost_usd,
        input_tokens=query_call.input_tokens,
        output_tokens=query_call.output_tokens,
        session_id=query_call.session_id,
    )
    if not queries:
        raise AbortRun("research stage failed to generate queries")

    cache_hit = False
    findings: ResearchFindings | None = None
    if cache is not None and target_path is not None:
        findings = cache.lookup(
            target_path, queries, ttl_hours=research_cfg.cache_ttl_hours
        )
        if findings is not None:
            cache_hit = True
            event(
                "research.cache_hit",
                run_id=run_id,
                target=str(target_path),
                queries=len(queries),
            )

    if findings is None:
        findings, synth_call = await _synthesize_findings(
            queries=queries,
            mission=mission,
            research_cfg=research_cfg,
            meter=meter,
            auth_env=auth_env,
            cli_path=cli_path,
            model=model,
            mcp_servers=mcp_servers,
        )
        meter.record(
            "research:synth",
            synth_call.cost_usd,
            input_tokens=synth_call.input_tokens,
            output_tokens=synth_call.output_tokens,
            session_id=synth_call.session_id,
        )
        if findings is None:
            _dump_synth_debug(out_dir, run_id, synth_call.text, synth_call.last_message_text)
            raise AbortRun(
                "research stage produced no parseable findings — see "
                f"synth-debug-{run_id}.txt for raw model output"
            )
        synth_cost = synth_call.cost_usd
    else:
        synth_cost = 0.0

    if not findings.queries_run:
        findings = findings.model_copy(update={"queries_run": queries})
    if not findings.sources_used:
        sources = []
        if "web" in research_cfg.sources:
            sources.append(web_source_label())
        if "reddit" in research_cfg.sources:
            sources.append("reddit")
        if "hn" in research_cfg.sources:
            sources.append("hn")
        if "producthunt" in research_cfg.sources:
            sources.append("producthunt")
        findings = findings.model_copy(update={"sources_used": sources})

    if not cache_hit and cache is not None and target_path is not None:
        cache.store(
            target_path, queries, findings, ttl_hours=research_cfg.cache_ttl_hours
        )
        event(
            "research.cache_store",
            run_id=run_id,
            target=str(target_path),
            queries=len(queries),
        )

    brief_path = write_research_artifacts(findings=findings, out_dir=out_dir, run_id=run_id)
    return ResearchResult(
        findings=findings,
        brief_path=brief_path,
        cost_usd=query_call.cost_usd + synth_cost,
        cache_hit=cache_hit,
    )
