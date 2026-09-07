"""Resume — re-run the agent loop from the session head, appending nothing.

A turn that dies on an ErrorEvent (the empty-completion cap, a 400, a dropped
connection) leaves the head on a user node the model never answered: either the
original text prompt or the tool results it was about to read. Resume picks the
loop back up from that exact node, so the retry sends the same history the
failed call saw and the transcript gains only the answer.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.engine import native
from fluid.engine.native import NativeLoop
from fluid.events import (
    ErrorEvent,
    Resume,
    TurnComplete,
    TurnStarted,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.registry import ToolRegistry

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _empty,
    _text,
    _tool_call,
    _weather_server,
)

NOTHING_TO_RESUME = "nothing to resume: the last message is already an assistant reply"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty-completion backoff is not what these tests are about."""
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


def _bad_request() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("error", {"message": "status 400: invalid request",
                              "retriable": False}),
    ]


def _rescript(provider: ScriptedProvider, scripts: list[list[StreamChunk]]) -> None:
    """Point the fake provider at a new script and forget the old call count."""
    provider.scripts = scripts
    provider.calls = 0


async def _turn(loop: NativeLoop, op) -> None:
    await loop.submit(op)
    await asyncio.wait_for(loop.wait_turn(), timeout=10)


def _events_since(loop: NativeLoop, sid: str, mark: int) -> list:
    return loop.bus.log(sid)[mark:]


# ─── (a) resume from tool results, over the persisted store ──────────────────


async def test_resume_continues_from_a_tool_result_head(tmp_path) -> None:
    """The retry cap gives up with the tool results at the head; Resume answers
    them. Runs on the JSONL store so ``load_path`` is exercised against real
    persistence, not just the in-memory dict."""
    store = JsonlStore(tmp_path / "sessions")
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    # One tool call, then nothing but empties: the last script repeats, so the
    # loop burns every retry and ends the turn on an ErrorEvent.
    provider = ScriptedProvider([
        _tool_call("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _empty(),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
        registry=registry, permissions=PermissionEngine("never_ask"),
    )

    await _turn(loop, UserTurn(content="weather in Paris?"))
    errors = [e for e in loop.bus.log(sid) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "giving up" in errors[0].message

    before = await store.load_path(sid)
    head = before[-1]
    assert head.message.role == "user"  # the tool results, unanswered
    assert [b.tool_use_id for b in head.message.content] == ["t1"]

    # A fresh store instance would replay the same log; the point of this test
    # is that Resume reads the head back through load_path rather than from any
    # in-process turn state.
    mark = len(loop.bus.log(sid))
    _rescript(provider, [_text("It is sunny in Paris.")])
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1
    assert started[0].node_id == head.node_id
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert provider.calls == 1

    after = await store.load_path(sid)
    assert len(after) == len(before) + 1  # only the answer was written
    assert [n.node_id for n in after[:-1]] == [n.node_id for n in before]
    answer = after[-1]
    assert answer.message.role == "assistant"
    assert answer.parent_id == head.node_id
    assert answer.message.text() == "It is sunny in Paris."

    # Same story on disk: a store that reloads the log sees one added node.
    reloaded = JsonlStore(tmp_path / "sessions")
    replayed = await reloaded.load_path(sid)
    assert [n.node_id for n in replayed] == [n.node_id for n in after]


# ─── (b) nothing to resume ───────────────────────────────────────────────────


async def test_resume_on_an_assistant_head_reports_nothing_to_resume() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_text("all done")])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, UserTurn(content="go"))
    before = await store.load_path(sid)
    assert before[-1].message.role == "assistant"

    mark = len(loop.bus.log(sid))
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1
    assert started[0].node_id == before[-1].node_id
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].message == NOTHING_TO_RESUME
    assert errors[0].retriable is False
    assert not any(isinstance(e, TurnComplete) for e in events)

    assert provider.calls == 1  # the model was never asked again
    after = await store.load_path(sid)
    assert [n.node_id for n in after] == [n.node_id for n in before]


async def test_resume_on_an_empty_session_reports_nothing_to_resume() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_text("unreachable")])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, Resume())

    events = loop.bus.log(sid)
    assert [e.node_id for e in events if isinstance(e, TurnStarted)] == [""]
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and errors[0].message == NOTHING_TO_RESUME
    assert provider.calls == 0
    assert await store.load_path(sid) == []


# ─── (c) one turn at a time ──────────────────────────────────────────────────


class BlockingProvider:
    """Parks inside the stream until released."""

    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await self.release.wait()
        for chunk in _text("finally"):
            yield chunk


async def test_resume_during_a_running_turn_raises() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = BlockingProvider()
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    with pytest.raises(RuntimeError, match="a turn is already running"):
        await loop.submit(Resume())

    provider.release.set()
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    assert (await store.load_path(sid))[-1].message.role == "assistant"


# ─── (d) resume from a text prompt the first call never answered ─────────────


async def test_resume_after_a_first_call_failure_answers_the_prompt() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_bad_request()])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, UserTurn(content="what is 2 + 2?"))
    errors = [e for e in loop.bus.log(sid) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "invalid request" in errors[0].message

    before = await store.load_path(sid)
    assert len(before) == 1
    prompt_node = before[0]
    assert prompt_node.message.role == "user"

    mark = len(loop.bus.log(sid))
    _rescript(provider, [_text("4")])
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1 and started[0].node_id == prompt_node.node_id
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)

    after = await store.load_path(sid)
    assert len(after) == 2
    assert after[0].node_id == prompt_node.node_id
    assert after[1].parent_id == prompt_node.node_id
    assert after[1].message.text() == "4"
    # The resumed call saw the same single-message history the failed one did.
    assert provider.calls == 1
