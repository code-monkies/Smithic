"""Shared fake-SDK helpers for stage tests.

The real ``AssistantMessage``/``ResultMessage`` types are used so the stage
code's ``isinstance`` checks succeed. We only fake the network — never the
type contract.
"""

from __future__ import annotations

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def assistant_msg(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-sonnet-test")


def result_msg(
    *,
    total_cost_usd: float = 0.0,
    input_tokens: int = 50,
    output_tokens: int = 25,
    session_id: str = "sess-1",
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


def scripted_query(scripts: list[list[object]]) -> object:
    """Build a stand-in for ``claude_agent_sdk.query`` that yields scripted messages.

    Each call consumes the next list in ``scripts`` (saturates at the last entry).
    """
    calls = {"i": 0}

    async def fake_query(*, prompt: str, options: object):
        idx = min(calls["i"], len(scripts) - 1)
        calls["i"] += 1
        for msg in scripts[idx]:
            yield msg

    return fake_query
