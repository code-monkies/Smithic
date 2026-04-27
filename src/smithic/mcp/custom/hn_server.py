"""Custom MCP server wrapping Hacker News via the Algolia search API.

HN's official Firebase API is read-only and great for raw IDs but bad for
keyword search. Algolia hosts the public search endpoint at
``https://hn.algolia.com/api/v1`` — no auth, no rate limit issues for
reasonable use, structured JSON.

Tools exposed:

- ``search_hn(query, tags, limit)`` — keyword search across stories/comments.
- ``front_page(limit)`` — current top stories.
- ``story_with_comments(story_id, limit)`` — a single story plus its comments.

Same in-process 5-minute response cache pattern as the Reddit server.

Run as stdio: ``python -m smithic.mcp.custom.hn_server``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

USER_AGENT = "smithic/0.3 (https://github.com/code-monkies/Smithic)"
SEARCH_URL = "https://hn.algolia.com/api/v1/search"
SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items"
HN_PERMALINK = "https://news.ycombinator.com/item?id={id}"

CACHE_TTL_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 10.0

mcp = FastMCP("smithic-hn")


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


def _normalize_hit(hit: dict[str, Any]) -> dict[str, Any]:
    obj_id = hit.get("objectID")
    title = hit.get("title") or hit.get("story_title") or ""
    body_excerpt = (hit.get("comment_text") or hit.get("story_text") or "")[:500]
    url = hit.get("url") or HN_PERMALINK.format(id=obj_id)
    return {
        "id": obj_id,
        "title": title,
        "body_excerpt": body_excerpt,
        "url": url,
        "points": hit.get("points"),
        "num_comments": hit.get("num_comments"),
        "author": hit.get("author"),
        "created_at": hit.get("created_at"),
        "tags": hit.get("_tags") or [],
    }


@mcp.tool()
def search_hn(
    query: str,
    tags: str = "story",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search Hacker News by keyword.

    ``tags`` accepts Algolia's HN tag syntax: ``"story"``, ``"comment"``,
    ``"front_page"``, ``"poll"``, ``"show_hn"``, ``"ask_hn"``, or comma-
    separated combinations like ``"story,front_page"``. Default is ``"story"``.

    Returns up to ``limit`` hits with title, body excerpt, url, points, and
    comment count.
    """
    limit = max(1, min(int(limit), 100))
    payload = _fetch(
        SEARCH_URL,
        params={"query": query, "tags": tags, "hitsPerPage": limit},
    )
    return [_normalize_hit(h) for h in (payload.get("hits") or [])]


@mcp.tool()
def front_page(limit: int = 25) -> list[dict[str, Any]]:
    """Return the current HN front page (highest-ranked stories)."""
    limit = max(1, min(int(limit), 100))
    payload = _fetch(
        SEARCH_URL,
        params={"tags": "front_page", "hitsPerPage": limit},
    )
    return [_normalize_hit(h) for h in (payload.get("hits") or [])]


@mcp.tool()
def story_with_comments(story_id: str, limit: int = 50) -> dict[str, Any]:
    """Return a single story plus a flattened slice of its comment tree."""
    limit = max(1, min(int(limit), 200))
    payload = _fetch(f"{ITEM_URL}/{story_id}")
    comments: list[dict[str, Any]] = []
    _flatten_children(payload.get("children") or [], comments, limit)
    return {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "url": payload.get("url") or HN_PERMALINK.format(id=payload.get("id")),
        "points": payload.get("points"),
        "author": payload.get("author"),
        "created_at": payload.get("created_at"),
        "comments": comments,
    }


def _flatten_children(
    children: list[dict[str, Any]],
    out: list[dict[str, Any]],
    limit: int,
) -> None:
    for child in children:
        if len(out) >= limit:
            return
        out.append(
            {
                "id": child.get("id"),
                "author": child.get("author"),
                "text": (child.get("text") or "")[:500],
                "created_at": child.get("created_at"),
            }
        )
        if child.get("children"):
            _flatten_children(child["children"], out, limit)


def main() -> None:
    """Entry point for ``python -m smithic.mcp.custom.hn_server``."""
    mcp.run()


if __name__ == "__main__":
    main()
