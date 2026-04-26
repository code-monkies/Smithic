"""Auth mode detection and env-var construction for the Claude Agent SDK.

The SDK delegates auth to the bundled (or system) ``claude`` CLI. That CLI can
authenticate four different ways:

- **api**         — ``ANTHROPIC_API_KEY`` env var; per-token billing on the
                    Anthropic Console. The SDK reports ``total_cost_usd`` here.
- **subscription**— A logged-in Claude Code session backed by a Pro / Max plan.
                    No API key needed; usage flows through the subscription.
                    ``total_cost_usd`` is reported as 0 in this mode.
- **bedrock**     — AWS Bedrock with ``CLAUDE_CODE_USE_BEDROCK=1``.
- **vertex**      — Google Vertex AI with ``CLAUDE_CODE_USE_VERTEX=1``.
- **foundry**     — Azure AI Foundry with ``CLAUDE_CODE_USE_FOUNDRY=1``.

``ANTHROPIC_API_KEY``, when set, takes precedence over a subscription session.
That's a Claude Code behavior, not ours — we surface it explicitly in the
preflight check so users don't accidentally burn API credits when they meant
to use their subscription.
"""

from __future__ import annotations

import os
import shutil
from typing import Literal

AuthMode = Literal["auto", "api", "subscription", "bedrock", "vertex", "foundry"]

# Bedrock/Vertex/Foundry require disabling Anthropic-specific beta headers that
# those providers don't support, otherwise calls fail.
_DISABLE_BETAS_ENV = {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"}


class AuthError(RuntimeError):
    """Raised when auth is misconfigured or unreachable."""


def detect_mode(env: dict[str, str] | None = None) -> AuthMode:
    """Pick the active auth mode based on the current environment.

    Order of precedence mirrors what the Claude CLI itself uses:

    1. Explicit cloud-provider env (Bedrock / Vertex / Foundry).
    2. ``ANTHROPIC_API_KEY`` → API mode.
    3. Otherwise assume the user has a subscription session via ``claude login``.

    The "auto" mode in config maps onto whichever of these wins.
    """
    env = env if env is not None else dict(os.environ)
    if env.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return "bedrock"
    if env.get("CLAUDE_CODE_USE_VERTEX") == "1":
        return "vertex"
    if env.get("CLAUDE_CODE_USE_FOUNDRY") == "1":
        return "foundry"
    if env.get("ANTHROPIC_API_KEY"):
        return "api"
    return "subscription"


def env_for_mode(mode: AuthMode) -> dict[str, str]:
    """Return env-var overrides to push into the SDK call for a given mode."""
    if mode == "api":
        # Nothing to inject — the SDK reads ANTHROPIC_API_KEY itself.
        return {}
    if mode == "subscription":
        # If ANTHROPIC_API_KEY is set in the parent env it would override
        # subscription auth; clear it for this call to honor user intent.
        return {"ANTHROPIC_API_KEY": ""}
    if mode == "bedrock":
        return {"CLAUDE_CODE_USE_BEDROCK": "1", **_DISABLE_BETAS_ENV}
    if mode == "vertex":
        return {"CLAUDE_CODE_USE_VERTEX": "1", **_DISABLE_BETAS_ENV}
    if mode == "foundry":
        return {"CLAUDE_CODE_USE_FOUNDRY": "1", **_DISABLE_BETAS_ENV}
    raise ValueError(f"unknown auth mode: {mode!r}")


def is_metered(mode: AuthMode) -> bool:
    """Whether the SDK reports per-call USD cost in this mode.

    Subscription usage is bundled into the plan; the SDK reports ``$0`` for
    those calls. Bedrock/Vertex/Foundry bill through the cloud provider and
    the SDK does not aggregate that cost. Only direct API mode produces a
    usable ``total_cost_usd`` figure.
    """
    return mode == "api"


def preflight(mode: AuthMode, *, cli_path: str | None = None) -> AuthMode:
    """Validate that a chosen auth mode is reachable. Returns the resolved mode.

    Raises ``AuthError`` with an actionable message if something's wrong.
    """
    resolved: AuthMode = detect_mode() if mode == "auto" else mode

    cli = cli_path or shutil.which("claude")
    if cli is None and resolved != "api":
        # API mode can run via the SDK's bundled CLI without a system `claude`,
        # but subscription/bedrock/vertex/foundry rely on a real CLI session.
        raise AuthError(
            "the `claude` CLI is required for non-API auth modes but was not "
            "found on PATH. Install it from https://claude.ai/install.sh or "
            "set [auth].cli_path in smithic.toml."
        )

    if resolved == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthError(
            "auth mode is `api` but ANTHROPIC_API_KEY is not set. "
            "Set the env var, or switch [auth].mode to `subscription` if you "
            "have a Claude Pro/Max plan."
        )

    if resolved == "subscription" and os.environ.get("ANTHROPIC_API_KEY"):
        # Not fatal, but loud — ANTHROPIC_API_KEY would otherwise win.
        # We clear it in env_for_mode() so the SDK call still uses subscription,
        # but warn so the user knows their env is contradictory.
        # Use a lazy import so logger is wired only when needed.
        from smithic.telemetry.logger import event

        event(
            "auth.warn",
            message=(
                "subscription mode requested but ANTHROPIC_API_KEY is set in "
                "the parent env; clearing it for SDK calls so subscription "
                "billing is used"
            ),
        )

    return resolved
