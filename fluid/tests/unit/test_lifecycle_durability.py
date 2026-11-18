"""A failed replay read must not count as rehydrated, and a handoff's model
call is billed, interruptible, admitted one at a time, refused on a closing
engine and sees which tools the session used without their arguments or
output."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import fluid.engine.background as background_mod
import fluid.engine.native as native_mod
import pytest
from fluid.engine.native import NativeLoop, _clip, _handoff_transcript
from fluid.events import Handoff, HandoffCreated, Interrupt, SessionWoken, Usage, UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode

from tests.unit.test_background_rehydrate import _loop, _rehydrate, _store_with, _sub
from tests.unit.test_tool_loop import ScriptedProvider, _text

SID = "parent"
SECRET = "sk-test-SECRET-0123456789"


# ─── a failed replay read is not a rehydration ──────────────────────────────


def _flaky_replay(monkeypatch, failures: int) -> dict[str, int]:
    real = background_mod.iter_events_of_class
    state = {"left": failures, "reads": 0}

    def flaky(store, sid, seq, classes):
        if SessionWoken in classes:  # the lifecycle history read
            state["reads"] += 1
            if state["left"]:
                state["left"] -= 1
                raise OSError("store unavailable")
        return real(store, sid, seq, classes)

    monkeypatch.setattr(background_mod, "iter_events_of_class", flaky)
    return state


async def test_failed_replay_read_is_retried_and_owed_wake_delivered(monkeypatch) -> None:
    monkeypatch.setattr(background_mod, "_REPLAY_RETRY_BASE_SECONDS", 0.0)
    store = await _store_with(
        _sub("owed", "started"),
        _sub("owed", "finished", summary="the result", wake=True),
    )
    _flaky_replay(monkeypatch, failures=1)
    provider = ScriptedProvider([_text("first"), _text("second"), _text("woken")])
    loop = _loop(store, provider=provider)

    await _rehydrate(loop)  # the read fails
    assert loop._background_rehydrated is False
    assert loop._background_completions == {}

    await loop.submit(UserTurn(content="hello again"))
    await loop.wait_turn()

    woken = [e for e in loop.bus.log(SID) if isinstance(e, SessionWoken)]
    assert [w.task_ids for w in woken] == [("owed",)]
    assert any("the result" in r.messages[-1].text() for r in provider.requests)
    await loop.aclose()


async def test_failing_replay_backs_off_and_warns_once(monkeypatch, caplog) -> None:
    store = await _store_with(_sub("t", "started"))
    state = _flaky_replay(monkeypatch, failures=10)
    loop = _loop(store)

    with caplog.at_level(logging.WARNING, logger=background_mod.__name__):
        for _ in range(5):  # a burst of lifecycle calls during an outage
            await _rehydrate(loop)
        assert state["reads"] == 1
        loop._background_replay_retry_at = 0.0  # the backoff has elapsed
        await _rehydrate(loop)
        assert state["reads"] == 2
    assert loop._background_rehydrated is False
    assert loop._background_replay_failures == 2
    assert len([r for r in caplog.records if "background task history" in r.message]) == 1
    await loop.aclose()


def test_replay_backoff_stays_capped_after_a_long_outage() -> None:
    loop = _loop(MemoryStore())
    loop._background_replay_failures = 10_000  # hours of failed reads
    loop._note_replay_failure(SID, OSError("store unavailable"))
    wait = loop._background_replay_retry_at - background_mod.time.monotonic()
    assert 0 < wait <= background_mod._REPLAY_RETRY_MAX_SECONDS


# ─── handoff: billed, interruptible, one at a time, tool names only ─────────


class _HandoffProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, *, hang: bool = False) -> None:
        self.requests: list[ProviderRequest] = []
        self.hang = hang
        self.started = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 700}})
        self.started.set()
        if self.hang:
            await asyncio.sleep(3600)
        yield StreamChunk("text_delta", {"index": 0, "text": "HANDOFF DOC"})
        yield StreamChunk("usage", {"usage": {"output_tokens": 30}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def _tool_heavy_session(store: MemoryStore) -> str:
    sid = (await store.create_session()).session_id
    parent = None
    count = 0

    async def add(message: Message) -> None:
        nonlocal parent, count
        count += 1
        node = SessionNode(node_id=f"n{count}", parent_id=parent, message=message)
        await store.append(sid, node)
        parent = node.node_id

    await add(Message(role="user", content=[Text("migrate the billing tables")]))
    for i in range(60):
        await add(Message(role="assistant", content=[ToolUseBlock(
            id=f"tu{i}", name="Bash",
            input={"command": f"PGPASSWORD={SECRET} psql -c 'step {i}'"})]))
        await add(Message(role="user", content=[ToolResultBlock(
            tool_use_id=f"tu{i}", is_error=i == 59,
            content=[Text(f"token={SECRET} ALTER TABLE ok {i}")])]))
    return sid


async def test_handoff_usage_is_recorded_and_booked() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    booked: list[Usage] = []

    async def sink(event: Usage) -> None:
        booked.append(event)

    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m",
                      session_id=sid, background_usage_sink=sink)
    await loop.submit(Handoff(goal="finish"))

    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [(u.input_tokens, u.output_tokens, u.raw.get("auxiliary_source")) for u in usage] == [
        (700, 30, "handoff")]
    assert [e.seq for e in booked] == [usage[0].seq]
    await loop.aclose()


async def test_handoff_transcript_names_tools_but_never_their_payloads() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider()
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    await loop.submit(Handoff())

    prompt = provider.requests[-1].messages[0].content[0].text
    transcript = prompt.split("--- transcript ---\n", 1)[1]
    assert "migrate the billing tables" in transcript
    assert transcript.count("[tool call: Bash]") == 60
    assert "[Bash result: ok, " in transcript and "[Bash result: error, " in transcript
    assert SECRET not in prompt
    assert "psql" not in prompt and "ALTER TABLE" not in prompt
    assert [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    await loop.aclose()


def test_handoff_transcript_never_exceeds_its_budget(monkeypatch) -> None:
    assert len(_clip("x" * 10, 5)) == 5 and _clip("x" * 10, 5).endswith("…")
    monkeypatch.setattr(native_mod, "_HANDOFF_TRANSCRIPT_CHARS", 100)
    huge = SessionNode(node_id="n1", parent_id=None,
                       message=Message(role="user", content=[Text("y" * 5000)]))
    assert len(_handoff_transcript([huge])) == 100
    small = [SessionNode(node_id=f"s{i}", parent_id=None,
                         message=Message(role="user", content=[Text("z" * 30)]))
             for i in range(10)]
    assert len(_handoff_transcript(small)) <= 100


async def test_interrupt_cancels_the_handoff_call_and_still_bills_it() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    sessions_before = set(store._sessions)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    await loop.submit(Interrupt())

    with pytest.raises(RuntimeError, match="handoff not created.*interrupted"):
        await asyncio.wait_for(handoff, 5)
    assert set(store._sessions) == sessions_before
    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [(u.input_tokens, u.raw.get("auxiliary_source")) for u in usage] == [(700, "handoff")]
    await loop.aclose()


async def test_a_second_handoff_while_one_runs_fails_fast() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    sessions_before = set(store._sessions)

    first = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    with pytest.raises(RuntimeError, match="another handoff is already running"):
        await asyncio.wait_for(loop.submit(Handoff()), 5)
    assert len(provider.requests) == 1

    await loop.submit(Interrupt())  # still reaches the one handoff running
    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.wait_for(first, 5)
    assert set(store._sessions) == sessions_before
    assert loop._handoff_running is False
    await loop.aclose()


class _SlowCreate(MemoryStore):
    """Holds ``create_session`` once ``hold`` is set, until released."""

    def __init__(self) -> None:
        super().__init__()
        self.creating = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False

    async def create_session(self, session_id=None, *, cwd=None):
        if self.hold:
            self.creating.set()
            await self.release.wait()
        return await super().create_session(session_id, cwd=cwd)


async def test_interrupt_after_the_document_arrived_does_not_undo_the_handoff() -> None:
    store = _SlowCreate()
    sid = await _tool_heavy_session(store)
    store.hold = True
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m", session_id=sid)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(store.creating.wait(), 5)
    await loop.submit(Interrupt())  # past the cutoff: the session is made
    store.release.set()
    await asyncio.wait_for(handoff, 5)

    created = [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    assert len(created) == 1 and created[0].new_session_id in store._sessions
    await loop.aclose()


async def test_a_handoff_on_a_closed_loop_creates_no_session() -> None:
    store = MemoryStore()
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m")
    await loop.aclose()
    with pytest.raises(RuntimeError, match="the engine is closing"):
        await loop.submit(Handoff())
    assert store._sessions == {}


async def test_handoff_usage_is_booked_against_the_model_it_ran_on() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)

    class _SwitchMidStream(_HandoffProvider):
        async def stream(self, request):
            async for chunk in super().stream(request):
                loop.model = "replacement"  # a model switch while it streams
                yield chunk

    loop = NativeLoop(provider=_SwitchMidStream(), store=store, model="original",
                      session_id=sid)
    await loop.submit(Handoff())
    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [u.raw.get("model") for u in usage] == ["original"]
    await loop.aclose()
