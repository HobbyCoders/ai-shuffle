"""The per-turn model-call cap holds for every provider dispatch.

native.md Medium #4: the cap was checked only before tool dispatch and the
max_tokens continuation, so an empty-response retry or a Stop-hook
continuation sent request N+1 on a turn capped at N. Every dispatch now counts
(any of them can bill) and the cap is checked before each one.
"""

from __future__ import annotations

import asyncio

import pytest

from fluid.engine import native
from fluid.events import HookEvent, ProviderRetry, ToolResult, TurnComplete, Usage
from fluid.events.model import GuardrailBlocked, PermissionRequest
from fluid.events.ops import RespondPermission, UserTurn
from fluid.hooks.bus import TURN_END, HookBus, HookDecision
from fluid.messages import Text
from fluid.providers.base import StreamChunk
from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _assistant_nodes,
    _empty,
    _run,
    _text,
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


def _complete(loop, sid) -> TurnComplete:
    done = [e for e in loop.bus.log(sid) if isinstance(e, TurnComplete)]
    assert len(done) == 1
    return done[0]


async def test_an_empty_response_retry_does_not_exceed_the_cap() -> None:
    provider = ScriptedProvider([_empty(), _text("late answer")])
    loop, _store, sid = await _run(provider, max_model_calls_per_turn=1)

    assert provider.calls == 1
    assert not [e for e in loop.bus.log(sid) if isinstance(e, ProviderRetry)]
    done = _complete(loop, sid)
    assert done.stop_reason == "max_calls"
    assert done.num_model_calls == 1


async def test_the_cap_still_allows_the_retries_it_has_room_for() -> None:
    provider = ScriptedProvider([_empty(), _text("answer")])
    loop, _store, sid = await _run(provider, max_model_calls_per_turn=2)

    assert provider.calls == 2
    done = _complete(loop, sid)
    assert done.stop_reason == "end_turn"
    assert done.num_model_calls == 2


async def test_a_stop_hook_continuation_does_not_exceed_the_cap() -> None:
    hooks = HookBus()
    fired: list[str] = []

    async def keep_going(event, payload):
        fired.append(event)
        return HookDecision(action="deny", note="tests are not run yet")

    hooks.on(TURN_END, keep_going)
    provider = ScriptedProvider([_text("first"), _text("second")])
    loop, _store, sid = await _run(provider, hooks=hooks, max_model_calls_per_turn=1)

    assert provider.calls == 1
    assert fired == [TURN_END]  # the TurnEnd hooks ran once, not again at the end
    warnings = [
        e.message for e in loop.bus.log(sid)
        if isinstance(e, HookEvent) and e.hook_event == TURN_END
    ]
    assert any("model-call limit" in m for m in warnings)
    assert _complete(loop, sid).num_model_calls == 1


def _dropped_after_closed_call() -> list[StreamChunk]:
    """Visible text and one finished tool call, then the peer drops the line."""
    return _text("Partial answer")[:3] + [
        StreamChunk("block_start", {"index": 1, "block_type": "tool_use",
                                    "tool_name": "mcp__test__touch", "tool_use_id": "cut"}),
        StreamChunk("tool_args_delta", {"index": 1, "partial_json": "{}"}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("error", {
            "message": "RemoteProtocolError: incomplete chunked read",
            "retriable": False,
            "transport_failure": {
                "chunks_delivered": True, "exception_type": "RemoteProtocolError",
                "elapsed_seconds": 3.0, "lines_received": 4, "retrying": False,
            },
        }),
    ]


async def test_a_dropped_connection_at_the_cap_announces_no_reconnect() -> None:
    """S7: the drop path published "reconnecting" and backed off, then the cap
    check stopped the re-send. At the cap nothing is announced; the failed
    call is still billed and kept, and its published tool call is closed."""
    provider = ScriptedProvider([_dropped_after_closed_call(), _text("never sent")])
    loop, store, sid = await _run(provider, max_model_calls_per_turn=1)

    events = loop.bus.log(sid)
    assert provider.calls == 1
    assert not [e for e in events if isinstance(e, ProviderRetry)]
    done = _complete(loop, sid)
    assert done.stop_reason == "max_calls" and done.num_model_calls == 1
    assert any(isinstance(e, Usage) for e in events)  # the failed call is recorded
    closed = [e for e in events if isinstance(e, ToolResult) and e.tool_use_id == "cut"]
    assert len(closed) == 1 and closed[0].is_error
    texts = [
        block.text for node in await _assistant_nodes(store, sid)
        for block in node.message.content if isinstance(block, Text)
    ]
    assert texts == ["Partial answer"]


async def test_a_refusal_at_the_cap_does_not_ask_to_switch_models() -> None:
    """S7: a provider refusal opened the switch-model prompt and, on allow,
    reported "Continued this turn on…" though the cap kept the retry from
    going out. At the cap the person is not asked and the turn ends on it."""
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore
    from tests.unit.test_guardrail import _Provider, _refusal

    provider = _Provider([
        _refusal("claude-opus-5-5", {"category": "cyber"}),
        _text("never sent"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-opus-5-5",
        max_model_calls_per_turn=1,
    )

    async def allow_any_prompt() -> None:
        while True:
            log = loop.bus.log(loop.session_id) if loop._session is not None else []
            asked = next((e for e in log if isinstance(e, PermissionRequest)), None)
            if asked is not None:
                await loop.submit(RespondPermission(request_id=asked.request_id, decision="allow"))
                return
            await asyncio.sleep(0.01)

    answerer = asyncio.create_task(allow_any_prompt())
    try:
        await loop.submit(UserTurn(content="scan this host"))
        await asyncio.wait_for(loop.wait_turn(), 10)
    finally:
        answerer.cancel()
    events = loop.bus.log(loop.session_id)
    assert provider.models == ["claude-opus-5-5"]
    assert not [e for e in events if isinstance(e, PermissionRequest)]
    outcomes = [e.outcome for e in events if isinstance(e, GuardrailBlocked)]
    assert outcomes == ["stopped"]
    done = [e for e in events if isinstance(e, TurnComplete)]
    assert len(done) == 1 and done[0].stop_reason == "max_calls"
