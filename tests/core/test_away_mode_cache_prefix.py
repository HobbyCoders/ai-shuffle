"""Away mode never touches the provider's cached prefix.

The instruction rides the tail of each new user message (the turn's context
note), never the system prompt or the tool schemas, and switching Away on or
off is a database write that does not reconnect the session. So across turns
with Away off, on and off again, every request's system prompt and tools are
byte-identical, and each request's messages extend the previous request's
messages unchanged (an earlier user message keeps exactly the text it was sent
with). Both legs are covered: the native loop against the fake provider
server in Anthropic and chat-completions formats, and the delegated Claude
Code leg against the fake CLI.
"""
from __future__ import annotations

import sys

import pytest

import app.core.provider.harness_adapter as ha
from app.core import away_mode
from tests.core.test_delegated_leg import FAKE, _ArgvLoop, _preset_options, _run_turn
from tests.core.test_native_scripted_provider_app import (  # noqa: F401 — fixtures
    Chat,
    Messages,
    NativeRun,
    _isolated_home,
    provider,
)

# off → on → on → off: two toggles and one repeated away turn.
SEQUENCE = (False, True, True, False)


@pytest.fixture
def away_state(monkeypatch):
    """Away mode as the session row would answer it, flipped between turns."""
    state = {"away": False}
    monkeypatch.setattr(away_mode, "is_away", lambda _sid: state["away"])
    monkeypatch.setattr(away_mode, "assistant_work", lambda _sid: False)
    return state


def _content(messages: list) -> list:
    """Messages without Anthropic's rolling ``cache_control`` breakpoint.

    The client marks the newest message as the cache breakpoint on every call,
    so the marker moves forward each turn by design; it is not message content.
    """
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(messages)


def _user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "openrouter"])
async def test_native_prefix_is_identical_with_away_on_off_and_across_a_toggle(
    monkeypatch, tmp_path, provider, away_state, kind
):
    reply = Messages if kind == "anthropic" else Chat
    provider.script(*[reply.text(f"reply {n}") for n in range(len(SEQUENCE))])
    async with NativeRun(monkeypatch, tmp_path, provider, kind=kind) as run:
        engine = run.adapter.engine
        for n, away in enumerate(SEQUENCE):
            away_state["away"] = away
            await run.turn(f"turn {n}")
            # A toggle never reconnects: the same engine serves every turn.
            assert run.adapter.engine is engine

    bodies = [request.body for request in provider.requests]
    assert len(bodies) == len(SEQUENCE)
    for earlier, later in zip(bodies, bodies[1:]):
        # The cached prefix: system prompt and tool schemas never change.
        assert later.get("system") == earlier.get("system")
        assert later.get("tools") == earlier.get("tools")
        # Earlier messages (and, for chat-completions, the system message at
        # their head) are resent byte for byte; only new turns are appended.
        assert _content(later["messages"][: len(earlier["messages"])]) == _content(earlier["messages"])
    for body, away in zip(bodies, SEQUENCE):
        newest = _user_text(body["messages"][-1])
        assert body["messages"][-1]["role"] == "user"
        # The agent still gets the instruction on every away turn, at the tail.
        assert ("The owner is away" in newest) is away
        assert "owner is away" not in str(body.get("system") or "").lower()
        assert "owner is away" not in str(body.get("tools") or "").lower()


@pytest.mark.asyncio
async def test_delegated_toggle_neither_respawns_the_cli_nor_changes_its_prompt(
    monkeypatch, tmp_path, away_state
):
    """Claude Code holds the conversation; its cached prefix is the system
    prompt fixed at spawn plus the history. Away rides the newest prompt, so a
    toggle must not spawn the CLI again (a new system prompt) or rewrite it."""
    monkeypatch.setattr(ha, "DelegatedLoop", _ArgvLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _ArgvLoop.argvs = []
    sent: list[str] = []

    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(tmp_path))
    real_submit = adapter.engine.submit

    async def record(turn):
        content = getattr(turn, "content", None)
        if isinstance(content, str):
            sent.append(content)
        return await real_submit(turn)

    monkeypatch.setattr(adapter.engine, "submit", record)
    append = adapter.engine.append_system_prompt
    try:
        for n, away in enumerate(SEQUENCE):
            away_state["away"] = away
            result = await _run_turn(adapter, f"turn {n}")
            assert not result.is_error
            assert adapter.engine.append_system_prompt == append
    finally:
        await adapter.disconnect()

    assert len(_ArgvLoop.argvs) == 1, "a toggle must not respawn the CLI"
    assert "owner is away" not in append.lower()
    assert [("The owner is away" in text) for text in sent] == list(SEQUENCE)
    for n, text in enumerate(sent):
        assert text.startswith(f"turn {n}")
