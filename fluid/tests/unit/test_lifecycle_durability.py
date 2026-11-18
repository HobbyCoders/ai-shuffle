"""Lifecycle writes that fail must stay retryable (issue #1769 cases 1 and 4),
a failed replay read must not count as rehydrated, and a handoff's model call
is billed, interruptible and sees the session's tool activity."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

import fluid.engine.background as background_mod
import fluid.events.bus as bus_mod
from fluid.engine.native import NativeLoop
from fluid.events import (
    BackgroundTaskFinished,
    Handoff,
    HandoffCreated,
    Interrupt,
    SessionWoken,
    SubagentEvent,
    Usage,
    UserTurn,
)
from fluid.events.bus import EventBus
from fluid.events.model import TextDelta
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.registry import BackgroundTaskReceipt

from tests.unit.test_background_rehydrate import _loop, _rehydrate, _store_with, _sub
from tests.unit.test_tool_loop import ScriptedProvider, _text

SID = "parent"


class FailOnce(MemoryStore):
    """Refuses the first event that matches ``predicate``, before committing it."""

    def __init__(self, predicate) -> None:
        super().__init__()
        self.predicate = predicate
        self.refused = 0

    async def append_event(self, session_id, event):
        if not self.refused and self.predicate(event):
            self.refused += 1
            raise OSError("store unavailable")
        await super().append_event(session_id, event)


def _is_terminal(task_id: str):
    return lambda e: (isinstance(e, SubagentEvent) and e.task_id == task_id
                      and e.kind in {"finished", "failed", "cancelled"})


def _subagent(events, task_id: str, kind: str) -> list[SubagentEvent]:
    return [e for e in events if isinstance(e, SubagentEvent)
            and e.task_id == task_id and e.kind == kind]


async def _stored(store, sid=SID):
    return await store.events_since(sid, 0)


# ─── #1769 case 1 + 4: a refused terminal write ──────────────────────────────


async def test_refused_terminal_write_stays_retryable_and_wakes_once() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", summary="42", wake=True)

    with pytest.raises(OSError):
        await loop.submit(done)

    # Nothing claims the task ended: not memory, not the live bus.
    assert "t" in loop._background_pending
    assert "t" not in loop._background_terminal
    assert "t" not in loop._background_completions
    assert _subagent(loop.bus.log(SID), "t", "finished") == []

    await loop.submit(done)  # the host retries; this write lands
    await loop.wait_turn()

    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    assert len(_subagent(loop.bus.log(SID), "t", "finished")) == 1
    woken = [e for e in await _stored(store) if isinstance(e, SessionWoken)]
    assert [w.task_ids for w in woken] == [("t",)]
    await loop.aclose()


async def test_live_subscriber_sees_one_terminal_after_a_refused_write() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    seen: list = []

    async def watch() -> None:
        async for event in loop.events(since_seq=0):
            seen.append(event)

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    done = BackgroundTaskFinished(task_id="t", status="finished", wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    await loop.submit(done)
    await loop.aclose()
    await asyncio.wait_for(watcher, 5)

    terminals = _subagent(seen, "t", "finished")
    assert len(terminals) == 1
    assert [e.seq for e in _subagent(await _stored(store), "t", "finished")] == [terminals[0].seq]


async def test_refused_terminal_on_early_completion_path_is_retried_by_the_receipt() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    # The worker finished before its spawn receipt arrived.
    await loop.submit(BackgroundTaskFinished(task_id="t", status="failed", wake=False))
    assert "t" in loop._background_early

    receipt = BackgroundTaskReceipt(task_id="t")
    with pytest.raises(OSError):
        await loop._background_task_started(receipt)
    assert "t" in loop._background_early and "t" not in loop._background_terminal

    await loop._background_task_started(receipt)  # retried spawn handshake
    assert "t" in loop._background_terminal and "t" not in loop._background_early
    assert len(_subagent(await _stored(store), "t", "failed")) == 1
    assert len(_subagent(loop.bus.log(SID), "t", "failed")) == 1
    await loop.aclose()


async def test_refused_completion_pending_write_is_retried() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "completion_pending")
    await store.create_session(SID)
    loop = _loop(store)
    done = BackgroundTaskFinished(task_id="t", status="finished", wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    assert "t" not in loop._background_early

    await loop.submit(done)
    assert "t" in loop._background_early
    assert len(_subagent(await _stored(store), "t", "completion_pending")) == 1
    await loop.aclose()


# ─── the bus: persist first, then publish ───────────────────────────────────


async def test_reserved_seq_is_awaited_past_the_grace_period(monkeypatch) -> None:
    monkeypatch.setattr(bus_mod, "REPLAY_GAP_GRACE_SECONDS", 0.05)
    store = MemoryStore()
    bus = EventBus(replay_source=store.events_since)
    async with contextlib.aclosing(bus.subscribe("s")) as sub:
        first = asyncio.create_task(anext(sub))
        await asyncio.sleep(0)
        held = bus.reserve(TextDelta(session_id="s", index=0, text="slow write"))
        later = await bus.publish(TextDelta(session_id="s", index=0, text="later"))
        await asyncio.sleep(0.2)  # well past the grace period
        assert not first.done(), "a reserved seq must not be skipped while its write runs"
        bus.deliver(held)
        assert [(await asyncio.wait_for(first, 1)).seq,
                (await asyncio.wait_for(anext(sub), 1)).seq] == [held.seq, later.seq]
    assert [e.seq for e in bus.log("s")] == [1, 2]


async def test_released_seq_is_skipped() -> None:
    store = MemoryStore()
    bus = EventBus(replay_source=store.events_since)
    async with contextlib.aclosing(bus.subscribe("s")) as sub:
        first = asyncio.create_task(anext(sub))
        await asyncio.sleep(0)
        bus.release(bus.reserve(TextDelta(session_id="s", index=0, text="refused")))
        later = await bus.publish(TextDelta(session_id="s", index=0, text="later"))
        assert (await asyncio.wait_for(first, 2)).seq == later.seq


# ─── a failed replay read is not a rehydration ──────────────────────────────


async def test_failed_replay_read_is_retried_and_owed_wake_delivered(monkeypatch) -> None:
    store = await _store_with(
        _sub("owed", "started"),
        _sub("owed", "finished", summary="the result", wake=True),
    )
    real = background_mod.iter_events_of_class
    failures = {"left": 1}

    def flaky(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(background_mod, "iter_events_of_class", flaky)
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


# ─── handoff: billed, interruptible, sees tool activity ─────────────────────


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
            id=f"tu{i}", name="Bash", input={"command": f"psql -c 'step {i}'" + " x" * 400})]))
        await add(Message(role="user", content=[ToolResultBlock(
            tool_use_id=f"tu{i}", content=[Text(f"ALTER TABLE ok {i}" + " y" * 4000)])]))
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


async def test_handoff_transcript_keeps_tool_activity_within_a_bound() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider()
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    await loop.submit(Handoff())

    prompt = provider.requests[-1].messages[0].content[0].text
    assert "Bash" in prompt and "step 59" in prompt and "ALTER TABLE ok 59" in prompt
    assert len(prompt) <= 70_000
    assert [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    await loop.aclose()


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


async def test_close_cancels_a_running_handoff_call() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    await asyncio.wait_for(loop.aclose(), 5)
    with pytest.raises(RuntimeError, match="handoff not created"):
        await asyncio.wait_for(handoff, 5)
