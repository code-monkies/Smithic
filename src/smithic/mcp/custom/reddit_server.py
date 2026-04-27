"""Custom MCP server wrapping Reddit's public JSON API for read-only browsing.

Reddit exposes JSON for almost every endpoint (``/r/{sub}/.json``,
``/r/{sub}/search.json``, ``/comments/{id}.json``) without requiring OAuth as
long as we send a real ``User-Agent`` and don't hammer them. This server
exposes three tools the research stage can call:

- ``search_subreddit(subreddit, query, limit)``
- ``top_posts(subreddit, time, limit)``
- ``comments(post_id, limit)``

Run as a stdio server: ``python -m smithic.mcp.custom.reddit_server``.

This module is intentionally dependency-light — it only uses ``httpx`` (already
transitively available) and the official ``mcp`` SDK. We cache responses
in-process for 5 minutes so the research stage can fan out queries without
re-hitting Reddit for the same key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

USER_AGENT = "smithic/0.2 (https://github.com/code-monkies/Smithic)"
BASE_URL = "https://www.reddit.com"
CACHE_TTL_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 10.0

mcp = FastMCP("smithic-reddit")


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


def _fetch(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    cache_key = f"{url}?{sorted((params or {}).items())}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        resp = client.get(url, params=params)
    resp.raise_for_status()
    payload = resp.json()
    _cache_put(cache_key, payload)
    return payload


def _normalize_post(child: dict[str, Any]) -> dict[str, Any]:
    data = child.get("data") or {}
    body_excerpt = (data.get("selftext") or "")[:500]
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "body_excerpt": body_excerpt,
        "url": f"{BASE_URL}{data.get('permalink', '')}",
        "score": data.get("score"),
        "num_comments": data.get("num_comments"),
        "subreddit": data.get("subreddit"),
        "author": data.get("author"),
        "created_utc": data.get("created_utc"),
    }


def _flatten_comments(node: dict[str, Any], out: list[dict[str, Any]], limit: int) -> None:
    if len(out) >= limit:
        return
    data = node.get("data") or {}
    if node.get("kind") == "t1":
        out.append(
            {
                "id": data.get("id"),
                "author": data.get("author"),
                "body": data.get("body"),
                "score": data.get("score"),
                "created_utc": data.get("created_utc"),
            }
        )
        if len(out) >= limit:
            return
    replies = data.get("replies")
    if isinstance(replies, dict):
        for child in (replies.get("data") or {}).get("children") or []:
            _flatten_comments(child, out, limit)


@mcp.tool()
def search_subreddit(subreddit: str, query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search a subreddit for posts matching ``query``.

    Returns up to ``limit`` posts with title, body excerpt, url, score, and timestamp.
    """
    limit = max(1, min(int(limit), 100))
    payload = _fetch(
        f"{BASE_URL}/r/{subreddit}/search.json",
        params={"q": query, "restrict_sr": "on", "limit": limit, "sort": "relevance"},
    )
    children = (payload.get("data") or {}).get("children") or []
    return [_normalize_post(c) for c in children]


@mcp.tool()
def top_posts(subreddit: str, time: str = "week", limit: int = 25) -> list[dict[str, Any]]:
    """Return the top posts in a subreddit over the given time window.

    ``time`` accepts Reddit's standard values: ``hour``, ``day``, ``week``,
    ``month``, ``year``, ``all``.
    """
    limit = max(1, min(int(limit), 100))
    if time not in {"hour", "day", "week", "month", "year", "all"}:
        time = "week"
    payload = _fetch(
        f"{BASE_URL}/r/{subreddit}/top.json",
        params={"t": time, "limit": limit},
    )
    children = (payload.get("data") or {}).get("children") or []
    return [_normalize_post(c) for c in children]


@mcp.tool()
def comments(post_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return a flattened comment tree for the given post id."""
    limit = max(1, min(int(limit), 200))
    payload = _fetch(f"{BASE_URL}/comments/{post_id}.json", params={"limit": limit})
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    out: list[dict[str, Any]] = []
    children = (payload[1].get("data") or {}).get("children") or []
    for child in children:
        _flatten_comments(child, out, limit)
        if len(out) >= limit:
            break
    return out


def main() -> None:
    """Entry point for ``python -m smithic.mcp.custom.reddit_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
