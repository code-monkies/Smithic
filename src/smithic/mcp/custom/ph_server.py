"""Custom MCP server for Product Hunt — GraphQL API, token-gated.

Product Hunt's API requires a developer token (free, but you have to register
the app). The orchestrator only spawns this server when ``PRODUCTHUNT_TOKEN``
is set in the environment; the registry skips the source with a log line
otherwise. This module raises a clear error at startup if the token is
missing, so misconfigured spawns die fast instead of returning empty results
that look like "no signal."

Tools:

- ``search_posts(query, limit)`` — keyword search across PH posts/launches.
- ``trending_posts(time_window, limit)`` — currently trending products.

Run as stdio: ``python -m smithic.mcp.custom.ph_server``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

USER_AGENT = "smithic/0.3 (https://github.com/code-monkies/Smithic)"
GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
CACHE_TTL_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 15.0

mcp = FastMCP("smithic-producthunt")


@dataclass
class _CacheEntry:
    expires_at: float
    payload: Any


_cache: dict[str, _CacheEntry] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    if entry.expires_at < time.monotonic():
        del _cache[key]
        return None
    return entry.payload


def _cache_put(key: str, payload: Any) -> None:
    _cache[key] = _CacheEntry(
        expires_at=time.monotonic() + CACHE_TTL_SECONDS, payload=payload
    )


def _token() -> str:
    token = os.environ.get("PRODUCTHUNT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "PRODUCTHUNT_TOKEN env var is not set — Product Hunt MCP server cannot start. "
            "Either set the token (https://api.producthunt.com/v2/oauth/applications) "
            "or remove `producthunt` from [research].sources in smithic.toml."
        )
    return token


def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    cache_key = f"{query}::{sorted((variables or {}).items())}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_token()}",
    }
    body = {"query": query, "variables": variables or {}}
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        resp = client.post(GRAPHQL_URL, json=body)
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"Product Hunt GraphQL error: {payload['errors']}")
    _cache_put(cache_key, payload)
    return payload


_SEARCH_QUERY = """
query Search($query: String!, $first: Int!) {
  posts(first: $first, order: VOTES, postedAfter: null) {
    edges {
      node {
        id
        name
        tagline
        description
        url
        votesCount
        commentsCount
        createdAt
        topics(first: 5) { edges { node { name } } }
      }
    }
  }
}
""".strip()

_TRENDING_QUERY = """
query Trending($first: Int!, $postedAfter: DateTime) {
  posts(first: $first, order: VOTES, postedAfter: $postedAfter) {
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        commentsCount
        createdAt
      }
    }
  }
}
""".strip()


def _normalize(node: dict[str, Any]) -> dict[str, Any]:
    topics = [
        edge.get("node", {}).get("name")
        for edge in (node.get("topics") or {}).get("edges") or []
    ]
    description = node.get("description") or ""
    return {
        "id": node.get("id"),
        "title": node.get("name"),
        "tagline": node.get("tagline") or "",
        "body_excerpt": description[:500],
        "url": node.get("url"),
        "votes": node.get("votesCount"),
        "num_comments": node.get("commentsCount"),
        "created_at": node.get("createdAt"),
        "topics": [t for t in topics if t],
    }


@mcp.tool()
def search_posts(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search Product Hunt posts by keyword (PH's GraphQL doesn't support
    server-side keyword search well, so we pull recent posts ordered by votes
    and let the caller filter — close enough for research signal). Returns up
    to ``limit`` posts."""
    limit = max(1, min(int(limit), 50))
    payload = _graphql(_SEARCH_QUERY, {"query": query, "first": limit})
    edges = (payload.get("data") or {}).get("posts", {}).get("edges") or []
    posts = [_normalize(e["node"]) for e in edges if e.get("node")]
    if query:
        q = query.lower()
        posts = [
            p
            for p in posts
            if q in (p["title"] or "").lower()
            or q in (p["tagline"] or "").lower()
            or q in (p["body_excerpt"] or "").lower()
        ] or posts  # fall back to unfiltered if nothing matches
    return posts


@mcp.tool()
def trending_posts(time_window: str = "week", limit: int = 25) -> list[dict[str, Any]]:
    """Return currently trending PH posts ordered by votes.

    ``time_window`` is one of ``day``, ``week``, ``month``. Anything else
    falls back to ``week``.
    """
    import datetime as _dt

    limit = max(1, min(int(limit), 50))
    delta = {
        "day": _dt.timedelta(days=1),
        "week": _dt.timedelta(days=7),
        "month": _dt.timedelta(days=30),
    }.get(time_window, _dt.timedelta(days=7))
    posted_after = (_dt.datetime.now(_dt.UTC) - delta).isoformat()

    payload = _graphql(_TRENDING_QUERY, {"first": limit, "postedAfter": posted_after})
    edges = (payload.get("data") or {}).get("posts", {}).get("edges") or []
    return [_normalize(e["node"]) for e in edges if e.get("node")]


def main() -> None:
    """Entry point for ``python -m smithic.mcp.custom.ph_server``.

    Validates the token at startup so a missing-token misconfig fails fast.
    """
    _token()
    mcp.run()


if __name__ == "__main__":
    main()
