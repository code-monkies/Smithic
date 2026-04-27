"""Product Hunt MCP server tests — token gate + GraphQL response shape."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import smithic.mcp.custom.ph_server as ph


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    ph._cache.clear()


def test_token_gate_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRODUCTHUNT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="PRODUCTHUNT_TOKEN"):
        ph._token()


def test_token_gate_returns_token_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCTHUNT_TOKEN", "secret-token")
    assert ph._token() == "secret-token"


def test_search_posts_normalizes_graphql(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = {
        "data": {
            "posts": {
                "edges": [
                    {
                        "node": {
                            "id": "1",
                            "name": "FastAPI Healthz",
                            "tagline": "Drop-in liveness for FastAPI",
                            "description": "Adds a /healthz endpoint with k8s probes.",
                            "url": "https://producthunt.com/posts/fastapi-healthz",
                            "votesCount": 42,
                            "commentsCount": 7,
                            "createdAt": "2026-04-01T00:00:00Z",
                            "topics": {"edges": [{"node": {"name": "Developer Tools"}}]},
                        }
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(ph, "_graphql", lambda q, v: canned)

    posts = ph.search_posts("healthz", limit=5)
    assert len(posts) == 1
    p = posts[0]
    assert p["title"] == "FastAPI Healthz"
    assert p["votes"] == 42
    assert p["topics"] == ["Developer Tools"]
    assert "Drop-in liveness" in p["tagline"]


def test_search_posts_falls_back_when_filter_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = {
        "data": {
            "posts": {
                "edges": [
                    {
                        "node": {
                            "id": "1",
                            "name": "Other Tool",
                            "tagline": "unrelated",
                            "description": "",
                            "url": "https://producthunt.com/x",
                            "votesCount": 5,
                            "commentsCount": 0,
                            "createdAt": "2026-04-01T00:00:00Z",
                        }
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(ph, "_graphql", lambda q, v: canned)
    # Nothing matches "healthz" — should still return the unfiltered list rather than [].
    posts = ph.search_posts("healthz", limit=5)
    assert len(posts) == 1


def test_trending_posts_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_graphql(query: str, variables: dict[str, Any]):
        captured["variables"] = variables
        return {
            "data": {
                "posts": {
                    "edges": [
                        {
                            "node": {
                                "id": "2",
                                "name": "X",
                                "tagline": "x",
                                "url": "https://producthunt.com/x",
                                "votesCount": 100,
                                "commentsCount": 3,
                                "createdAt": "2026-04-25T00:00:00Z",
                            }
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(ph, "_graphql", fake_graphql)
    posts = ph.trending_posts(time_window="day", limit=10)
    assert len(posts) == 1
    assert posts[0]["votes"] == 100
    assert "postedAfter" in captured["variables"]


def test_graphql_raises_on_errors_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCTHUNT_TOKEN", "x")

    def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "rate limited"}]})

    transport = httpx.MockTransport(transport_handler)
    original = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(ph.httpx, "Client", patched)
    with pytest.raises(RuntimeError, match="rate limited"):
        ph._graphql("query {}", {})
