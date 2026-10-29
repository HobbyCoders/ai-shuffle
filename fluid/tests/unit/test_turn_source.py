"""Turn sources, host-owned parked wakes, and pre-upgrade held-peer recovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from fluid.engine.native import NativeLoop
from fluid.events import TURN_SOURCES, TurnStarted
from fluid.events.model import event_from_dict, event_to_dict
from fluid.events.ops import AgentMessage, BackgroundTaskFinished, Resume, UserTurn
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.registry import BackgroundTaskReceipt


def test_turn_sources_are_the_six_documented_values() -> None:
    assert TURN_SOURCES == (
        "user", "agent_message", "wakeup", "unpark", "promotion", "automation",
    )


@pytest.mark.parametrize("source", TURN_SOURCES)
def test_turn_started_source_round_trips_through_the_codec(source: str) -> None:
    event = TurnStarted(
        session_id="s", node_id="n", turn_id="t", seq=4, ts=1.5,
        model="m", provider="p", source=source,
    )
    row = event_to_dict(event)
    assert row["source"] == source
    # The store writes JSON; the value has to survive that too.
    revived = event_from_dict(json.loads(json.dumps(row)))
    assert revived == event
    assert revived.source == source


def test_turn_started_without_source_decodes_with_the_default() -> None:
    """A row persisted before the field existed still loads, as unrecorded."""
    legacy = {
        "_cls": "TurnStarted", "_v": 1, "session_id": "s", "node_id": "n",
        "turn_id": "t", "seq": 2, "ts": 0.0, "model": "m", "provider": "p",
    }
    revived = event_from_dict(legacy)
    assert isinstance(revived, TurnStarted)
    assert revived.source is None
    assert TurnStarted(session_id="s", model="m", provider="p").source is None


class _Provider:
    name = "fake"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"})


def _sources(loop: NativeLoop) -> list[str | None]:
    return [e.source for e in loop.bus.log(loop.session_id) if isinstance(e, TurnStarted)]


async def test_native_turns_record_their_source() -> None:
    provider = _Provider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        await loop.submit(UserTurn(content="timer", source="wakeup"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        await loop.submit(AgentMessage(text="peer says hi", sender="worker"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert _sources(loop) == ["user", "wakeup", "agent_message"]
    finally:
        await loop.aclose()


async def test_resume_records_its_source() -> None:
    provider = _Provider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        # Nothing to resume (the head is an answer), but the turn still starts
        # and names its source.
        await loop.submit(Resume(source="promotion"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert _sources(loop) == ["user", "promotion"]
    finally:
        await loop.aclose()


async def _parked_loop(provider: _Provider) -> NativeLoop:
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    await loop.submit(UserTurn(content="delegate"))
    await asyncio.wait_for(loop.wait_turn(), 5)
    await loop._background_task_started(BackgroundTaskReceipt(
        task_id="task_1", worker_session_id=None, description="worker",
    ))
    return loop


async def test_parked_peer_wake_and_later_message_share_one_running_turn() -> None:
    class BlockingProvider(_Provider):
        def __init__(self):
            super().__init__()
            self.block = False
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.active = 0
            self.max_active = 0

        async def stream(self, request):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if self.block:
                    self.block = False
                    self.started.set()
                    await self.release.wait()
                async for chunk in super().stream(request):
                    yield chunk
            finally:
                self.active -= 1

    provider = BlockingProvider()
    loop = await _parked_loop(provider)
    try:
        provider.block = True
        await loop.submit(AgentMessage(text="first peer message", sender="worker"))
        await asyncio.wait_for(provider.started.wait(), 5)
        running = loop._turn_task
        await loop.submit(AgentMessage(text="second peer message", sender="worker"))
        assert loop._turn_task is running
        provider.release.set()
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert _sources(loop) == ["user", "agent_message"]
        assert provider.max_active == 1
        prompts = [request.messages[-1].text() for request in provider.requests]
        assert sum(prompt.count("first peer message") for prompt in prompts) == 1
        assert sum(prompt.count("second peer message") for prompt in prompts) == 1
    finally:
        provider.release.set()
        await loop.aclose()


async def test_background_wake_records_unpark() -> None:
    provider = _Provider()
    loop = await _parked_loop(provider)
    try:
        await loop.submit(BackgroundTaskFinished(task_id="task_1", summary="done"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert _sources(loop) == ["user", "unpark"]
    finally:
        await loop.aclose()


@pytest.mark.parametrize("urgent", [False, True])
async def test_host_owned_peer_message_to_a_parked_parent_is_refused(urgent) -> None:
    """The host owns every idle wake, including ordinary parked messages."""
    provider = _Provider()
    loop = await _parked_loop(provider)
    try:
        calls = len(provider.requests)
        with pytest.raises(RuntimeError, match="no turn is running"):
            await loop.submit(AgentMessage(
                text="halfway", sender="worker", wake=False, urgent=urgent,
            ))
        assert not loop.turn_running
        assert loop._queued_peer_messages == []
        assert len(provider.requests) == calls
    finally:
        await loop.aclose()


async def test_host_owned_peer_message_to_an_idle_unparked_parent_still_raises() -> None:
    provider = _Provider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        with pytest.raises(RuntimeError, match="no turn is running"):
            await loop.submit(AgentMessage(text="x", sender="worker", wake=False))
        assert loop._queued_peer_messages == []
    finally:
        await loop.aclose()


async def test_turn_running_tracks_the_turn_task() -> None:
    loop = NativeLoop(provider=_Provider(), store=MemoryStore(), model="m")
    try:
        assert loop.turn_running is False
        await loop.submit(UserTurn(content="hi"))
        assert loop.turn_running is True
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert loop.turn_running is False
    finally:
        await loop.aclose()


async def test_a_rebuilt_parked_engine_delivers_a_held_message_once() -> None:
    """A pre-upgrade held message is recovered once from memory and the log."""
    store = MemoryStore()
    first = await _parked_loop_on(_Provider(), store)
    provider = _Provider()
    rebuilt = NativeLoop(
        provider=provider, store=store, model="m", session_id=first.session_id,
    )
    try:
        assert await rebuilt._queue_if_parked(AgentMessage(text="halfway", sender="worker"))
        assert not rebuilt.turn_running
        await rebuilt.submit(BackgroundTaskFinished(task_id="task_1", summary="done"))
        await asyncio.wait_for(rebuilt.wait_turn(), 5)
        assert provider.requests[-1].messages[-1].text().count("halfway") == 1
    finally:
        await rebuilt.aclose()
        await first.aclose()


async def _parked_loop_on(provider: _Provider, store: MemoryStore) -> NativeLoop:
    loop = NativeLoop(provider=provider, store=store, model="m")
    await loop.submit(UserTurn(content="delegate"))
    await asyncio.wait_for(loop.wait_turn(), 5)
    await loop._background_task_started(BackgroundTaskReceipt(
        task_id="task_1", worker_session_id=None, description="worker",
    ))
    return loop
