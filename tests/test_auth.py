"""Auth mode detection, env construction, and preflight tests."""

from __future__ import annotations

import pytest

from smithic.auth import AuthError, detect_mode, env_for_mode, is_metered, preflight


class TestDetectMode:
    def test_bedrock_wins_over_api_key(self) -> None:
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": "sk-..."}
        assert detect_mode(env) == "bedrock"

    def test_vertex_wins_over_api_key(self) -> None:
        env = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": "sk-..."}
        assert detect_mode(env) == "vertex"

    def test_foundry_wins_over_api_key(self) -> None:
        env = {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_API_KEY": "sk-..."}
        assert detect_mode(env) == "foundry"

    def test_api_when_only_api_key_set(self) -> None:
        assert detect_mode({"ANTHROPIC_API_KEY": "sk-..."}) == "api"

    def test_subscription_when_nothing_set(self) -> None:
        assert detect_mode({}) == "subscription"


class TestEnvForMode:
    def test_api_injects_nothing(self) -> None:
        assert env_for_mode("api") == {}

    def test_subscription_clears_api_key(self) -> None:
        # Critical: ANTHROPIC_API_KEY would otherwise override subscription auth.
        assert env_for_mode("subscription") == {"ANTHROPIC_API_KEY": ""}

    def test_bedrock_disables_betas(self) -> None:
        env = env_for_mode("bedrock")
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"

    def test_vertex_disables_betas(self) -> None:
        env = env_for_mode("vertex")
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"

    def test_foundry_disables_betas(self) -> None:
        env = env_for_mode("foundry")
        assert env["CLAUDE_CODE_USE_FOUNDRY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError):
            env_for_mode("nonsense")  # type: ignore[arg-type]


class TestIsMetered:
    def test_only_api_is_metered(self) -> None:
        assert is_metered("api") is True
        for mode in ("subscription", "bedrock", "vertex", "foundry"):
            assert is_metered(mode) is False  # type: ignore[arg-type]


class TestPreflight:
    def test_api_without_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
            preflight("api")

    def test_api_with_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert preflight("api") == "api"

    def test_auto_resolves_to_api_when_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_USE_FOUNDRY", raising=False)
        assert preflight("auto") == "api"
