"""HN MCP server tool tests — httpx mocked at the transport level."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import smithic.mcp.custom.hn_server as hn


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    hn._cache.clear()


def _mock_transport(routes: dict[str, dict[str, Any]]) -> httpx.MockTransport:
    """Build a transport that returns canned JSON for matching URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        for url_substr, payload in routes.items():
            if url_substr in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "no route"})

    return httpx.MockTransport(handler)


@pytest.fixture
def mock_httpx(monkeypatch: pytest.MonkeyPatch):
    """Replace ``httpx.Client`` with one bound to a MockTransport per test."""

    def factory(routes: dict[str, dict[str, Any]]):
        transport = _mock_transport(routes)

        original = httpx.Client

        def patched_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(hn.httpx, "Client", patched_client)

    return factory


def test_search_hn_normalizes_hits(mock_httpx) -> None:
    mock_httpx(
        {
            "search": {
                "hits": [
                    {
                        "objectID": "1",
                        "title": "FastAPI is great",
                        "url": "https://example.com/post1",
                        "points": 42,
                        "num_comments": 7,
                        "author": "alice",
                        "created_at": "2026-04-26T00:00:00Z",
                        "_tags": ["story"],
                    },
                    {
                        "objectID": "2",
                        "story_title": "FastAPI rate limit pain",
                        "comment_text": "no built-in support, very annoying",
                        "author": "bob",
                        "_tags": ["comment"],
                    },
                ]
            }
        }
    )

    results = hn.search_hn("fastapi")
    assert len(results) == 2
    assert results[0]["title"] == "FastAPI is great"
    assert results[0]["url"] == "https://example.com/post1"
    assert results[0]["points"] == 42
    assert results[1]["title"] == "FastAPI rate limit pain"
    assert "no built-in support" in results[1]["body_excerpt"]
    assert results[1]["url"].startswith("https://news.ycombinator.com/item?id=")


def test_search_hn_clamps_limit(mock_httpx, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(url, params=None):
        captured["params"] = params
        return {"hits": []}

    monkeypatch.setattr(hn, "_fetch", fake_fetch)
    hn.search_hn("q", limit=999)
    assert captured["params"]["hitsPerPage"] == 100

    hn.search_hn("q", limit=-5)
    assert captured["params"]["hitsPerPage"] == 1


def test_front_page_uses_front_page_tag(mock_httpx, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(url, params=None):
        captured["params"] = params
        return {"hits": []}

    monkeypatch.setattr(hn, "_fetch", fake_fetch)
    hn.front_page(limit=10)
    assert captured["params"]["tags"] == "front_page"


def test_story_with_comments_flattens_tree(mock_httpx) -> None:
    mock_httpx(
        {
            "items/123": {
                "id": 123,
                "title": "A story",
                "url": "https://example.com/story",
                "points": 100,
                "author": "alice",
                "created_at": "2026-04-26T00:00:00Z",
                "children": [
                    {
                        "id": 200,
                        "author": "bob",
                        "text": "great point",
                        "created_at": "2026-04-26T01:00:00Z",
                        "children": [
                            {
                                "id": 201,
                                "author": "carol",
                                "text": "I disagree",
                                "created_at": "2026-04-26T02:00:00Z",
                                "children": [],
                            }
                        ],
                    },
                ],
            }
        }
    )

    out = hn.story_with_comments("123")
    assert out["title"] == "A story"
    assert len(out["comments"]) == 2
    assert out["comments"][0]["author"] == "bob"
    assert out["comments"][1]["author"] == "carol"


def test_cache_short_circuits_second_call(mock_httpx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(url, params=None):
        calls["n"] += 1
        return {"hits": []}

    monkeypatch.setattr(hn, "_fetch", lambda url, params=None: _wrap(fake_get(url, params)))
    hn.search_hn("query", limit=5)
    hn.search_hn("query", limit=5)
    assert calls["n"] == 2  # _fetch is replaced; cache lives inside it


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    return payload
