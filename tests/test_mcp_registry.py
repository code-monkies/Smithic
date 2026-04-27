"""MCP registry resolution tests."""

from __future__ import annotations

from smithic.mcp.registry import build_mcp_servers, has_producthunt_token, web_source_label


def test_web_uses_tavily_when_key_set() -> None:
    servers = build_mcp_servers(["web"], env={"TAVILY_API_KEY": "tvly-..."})
    assert "tavily" in servers
    assert "fetch" not in servers
    assert servers["tavily"]["command"] == "npx"


def test_web_falls_back_to_fetch_without_key() -> None:
    servers = build_mcp_servers(["web"], env={})
    assert "fetch" in servers
    assert "tavily" not in servers


def test_reddit_resolves_to_module() -> None:
    servers = build_mcp_servers(["reddit"], env={})
    assert "reddit" in servers
    assert servers["reddit"]["args"][:2] == ["-m", "smithic.mcp.custom.reddit_server"]


def test_hn_resolves_to_module() -> None:
    servers = build_mcp_servers(["hn"], env={})
    assert "hn" in servers
    assert servers["hn"]["args"][:2] == ["-m", "smithic.mcp.custom.hn_server"]


def test_producthunt_resolves_with_token() -> None:
    servers = build_mcp_servers(["producthunt"], env={"PRODUCTHUNT_TOKEN": "ph-tok"})
    assert "producthunt" in servers
    assert servers["producthunt"]["args"][:2] == ["-m", "smithic.mcp.custom.ph_server"]


def test_producthunt_skipped_without_token() -> None:
    servers = build_mcp_servers(["producthunt"], env={})
    assert "producthunt" not in servers


def test_producthunt_blank_token_treated_as_missing() -> None:
    servers = build_mcp_servers(["producthunt"], env={"PRODUCTHUNT_TOKEN": "  "})
    assert "producthunt" not in servers


def test_unknown_sources_skipped_silently() -> None:
    servers = build_mcp_servers(["futurething"], env={})
    assert servers == {}


def test_combined_sources_dedup() -> None:
    servers = build_mcp_servers(["web", "reddit", "hn", "web"], env={})
    assert set(servers) == {"fetch", "reddit", "hn"}


def test_web_source_label_reports_choice() -> None:
    assert web_source_label({"TAVILY_API_KEY": "x"}) == "tavily"
    assert web_source_label({}) == "fetch"


def test_has_producthunt_token() -> None:
    assert has_producthunt_token({"PRODUCTHUNT_TOKEN": "x"}) is True
    assert has_producthunt_token({"PRODUCTHUNT_TOKEN": ""}) is False
    assert has_producthunt_token({}) is False
