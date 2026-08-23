"""Empty completions are retried, not accepted.

Two shapes of "the provider answered 200 with nothing in it" (both observed on
OpenRouter): a terminal usage chunk reporting zero tokens with no content, and
a stream that closes before any terminal chunk. The engine re-sends the same
history up to EMPTY_COMPLETION_MAX_RETRIES times, announcing each attempt with
a ProviderRetry event, and never persists an empty assistant node.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.engine import native
from fluid.engine.native import EMPTY_COMPLETION_MAX_RETRIES, NativeLoop
from fluid.events import (
    ErrorEvent,
    Interrupt,
    Interrupted,
    ProviderRetry,
    TextDelta,
    TurnComplete,
    UserTurn,
)
from fluid.events.model import event_from_dict, event_to_dict
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry

TRUNCATED_MESSAGE = (
    "stream ended before the provider sent a terminal usage chunk "
    "(empty or truncated response)"
)


class ScriptedProvider:
    """Replays one scripted chunk list per model call; repeats the last."""

    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _empty() -> list[StreamChunk]:
    """Terminal usage chunk, zero tokens, no content blocks at all."""
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 0}}),
        StreamChunk("usage", {"usage": {"input_tokens": 0, "output_tokens": 0},
                              "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _truncated() -> list[StreamChunk]:
    """Stream dies before any terminal chunk (client-synthesized error)."""
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 0}}),
        StreamChunk("error", {"message": TRUNCATED_MESSAGE, "retriable": False}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _tool_call(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _thinking_only() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 120}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking"}),
        StreamChunk("thinking_delta", {"index": 0, "text": "The answer is 42."}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _weather_server():
    @tool("get_weather", "Get weather",
          {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    return create_inproc_server("wx", [get_weather])


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are instant unless a test asks for a real delay."""
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


async def _run(provider: ScriptedProvider, **kwargs) -> tuple[NativeLoop, MemoryStore, str]:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha",
        session_id=sid, **kwargs,
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return loop, store, sid


def _retries(events: list) -> list[ProviderRetry]:
    return [e for e in events if isinstance(e, ProviderRetry)]


async def _assistant_nodes(store: MemoryStore, sid: str) -> list:
    path = await store.load_path(sid)
    return [n for n in path if n.message.role == "assistant"]


# ─── (a) zero-token empty completions ────────────────────────────────────────


async def test_two_empty_completions_then_a_real_answer() -> None:
    provider = ScriptedProvider([_empty(), _empty(), _text("done")])
    loop, store, sid = await _run(provider)

    events = loop.bus.log(sid)
    retries = _retries(events)
    assert [r.attempt for r in retries] == [1, 2]
    assert {r.max_attempts for r in retries} == {EMPTY_COMPLETION_MAX_RETRIES}
    assert {r.model for r in retries} == {"stealth/ox-alpha"}
    assert all(r.reason for r in retries)
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert provider.calls == 3

    # Exactly one assistant node — the empty calls left nothing behind.
    nodes = await _assistant_nodes(store, sid)
    assert len(nodes) == 1
    assert nodes[0].message.text() == "done"

    # The retries are durable and round-trip through the codec with the class
    # intact, so a reloaded session replays them.
    persisted = [e for e in await store.events_since(sid, 0)
                 if isinstance(e, ProviderRetry)]
    assert len(persisted) == 2
    for ev in persisted:
        assert event_from_dict(event_to_dict(ev)) == ev


# ─── (b) the cap ─────────────────────────────────────────────────────────────


async def test_eleven_empty_completions_give_up() -> None:
    provider = ScriptedProvider([_empty()])
    loop, store, sid = await _run(provider)

    events = loop.bus.log(sid)
    retries = _retries(events)
    assert [r.attempt for r in retries] == list(range(1, EMPTY_COMPLETION_MAX_RETRIES + 1))
    assert provider.calls == EMPTY_COMPLETION_MAX_RETRIES + 1

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "giving up" in errors[0].message
    assert "11 times in a row" in errors[0].message
    assert errors[0].retriable is False
    assert not any(isinstance(e, TurnComplete) for e in events)

    assert await _assistant_nodes(store, sid) == []


# ─── (c) truncated stream ────────────────────────────────────────────────────


async def test_truncated_stream_is_retried_like_an_empty_one() -> None:
    provider = ScriptedProvider([_truncated(), _truncated(), _text("recovered")])
    loop, store, sid = await _run(provider)

    events = loop.bus.log(sid)
    assert [r.attempt for r in _retries(events)] == [1, 2]
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, TurnComplete) for e in events)

    nodes = await _assistant_nodes(store, sid)
    assert len(nodes) == 1
    assert nodes[0].message.text() == "recovered"


# ─── (d) reasoning-only completions are real answers ─────────────────────────


async def test_reasoning_only_completion_is_not_retried() -> None:
    provider = ScriptedProvider([_thinking_only()])
    loop, store, sid = await _run(provider)

    events = loop.bus.log(sid)
    assert _retries(events) == []
    assert provider.calls == 1
    assert any(isinstance(e, TurnComplete) for e in events)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "The answer is 42."

    nodes = await _assistant_nodes(store, sid)
    assert len(nodes) == 1
    assert nodes[0].message.text() == "The answer is 42."


# ─── (e) genuine provider errors keep the existing path ──────────────────────


async def test_real_provider_error_is_not_retried() -> None:
    bad_request = [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("error", {"message": "status 400: invalid request",
                              "retriable": False}),
    ]
    provider = ScriptedProvider([bad_request, _text("never reached")])
    loop, _store, sid = await _run(provider)

    events = loop.bus.log(sid)
    assert _retries(events) == []
    assert provider.calls == 1
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert "invalid request" in errors[0].message


# ─── (f) interrupt lands during the backoff ──────────────────────────────────


async def test_interrupt_during_backoff_ends_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.5)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_empty()])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    first_retry = asyncio.Event()

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, ProviderRetry):
                first_retry.set()
                return

    watcher = asyncio.create_task(watch())
    clock = asyncio.get_running_loop()
    started = clock.time()
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(first_retry.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    elapsed = clock.time() - started
    watcher.cancel()

    events = loop.bus.log(sid)
    assert any(isinstance(e, Interrupted) for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert provider.calls == 1
    # Ten full backoffs would be 5s; the interrupt cuts the wait short.
    assert elapsed < 2.0
    assert await _assistant_nodes(store, sid) == []


# ─── (g) the cap counts CONSECUTIVE empties ──────────────────────────────────


async def test_counter_resets_after_a_successful_call() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    scripts = [
        _empty(), _empty(),
        _tool_call("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        *[_empty() for _ in range(9)],
        _text("It is sunny in Paris."),
    ]
    provider = ScriptedProvider(scripts)
    loop, store, sid = await _run(
        provider, registry=registry, permissions=PermissionEngine("never_ask"),
    )

    events = loop.bus.log(sid)
    assert [r.attempt for r in _retries(events)] == [1, 2, *range(1, 10)]
    assert not any(isinstance(e, ErrorEvent) for e in events)
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    assert provider.calls == len(scripts)

    nodes = await _assistant_nodes(store, sid)
    assert len(nodes) == 2  # the tool call and the final answer
    assert nodes[-1].message.text() == "It is sunny in Paris."
