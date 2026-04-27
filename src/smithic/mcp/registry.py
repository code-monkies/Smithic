"""Resolve MCP server configs at runtime based on ``[research].sources``.

Returns a dict suitable for ``ClaudeAgentOptions(mcp_servers=...)``. Only the
research stage uses these — other stages don't need network tools.

Resolution rules:

- ``"web"`` → Tavily if ``TAVILY_API_KEY`` is set, otherwise ``mcp-server-fetch``.
- ``"reddit"`` → bundled ``smithic.mcp.custom.reddit_server`` over stdio.
- ``"hn"`` → bundled ``smithic.mcp.custom.hn_server`` over stdio.
- ``"producthunt"`` → bundled ``smithic.mcp.custom.ph_server`` if
  ``PRODUCTHUNT_TOKEN`` is set; otherwise skipped with a log line.
- Unknown sources are silently dropped (forward-compat).
"""

from __future__ import annotations

import os
import sys
from typing import Any


def has_tavily_key(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else os.environ
    return bool(e.get("TAVILY_API_KEY"))


def has_producthunt_token(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else os.environ
    return bool((e.get("PRODUCTHUNT_TOKEN") or "").strip())


def build_mcp_servers(
    sources: list[str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return MCP server configs for each requested research source.

    The shape matches ``McpStdioServerConfig`` so callers can pass it straight
    to ``ClaudeAgentOptions(mcp_servers=...)``.
    """
    servers: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for source in sources:
        if source in seen:
            continue
        seen.add(source)

        if source == "web":
            if has_tavily_key(env):
                servers["tavily"] = {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@tavily/mcp-server"],
                }
            else:
                servers["fetch"] = {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                }
        elif source == "reddit":
            servers["reddit"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "smithic.mcp.custom.reddit_server"],
            }
        elif source == "hn":
            servers["hn"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "smithic.mcp.custom.hn_server"],
            }
        elif source == "producthunt":
            if has_producthunt_token(env):
                servers["producthunt"] = {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "smithic.mcp.custom.ph_server"],
                }
            else:
                # Product Hunt's API requires a token. Skip with one log line
                # rather than hard-failing — unattended runs shouldn't break
                # because the user hasn't registered a PH developer app.
                from smithic.telemetry.logger import event

                event(
                    "research.source_skipped",
                    source="producthunt",
                    reason="PRODUCTHUNT_TOKEN not set",
                )
        # Unknown sources fall through silently — forward-compat with configs
        # written against a future release.

    return servers


def web_source_label(env: dict[str, str] | None = None) -> str:
    """Which web tool will be used: ``"tavily"`` or ``"fetch"``."""
    return "tavily" if has_tavily_key(env) else "fetch"
