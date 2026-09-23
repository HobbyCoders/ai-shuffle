"""TurnStarted records what started the turn, and parked parents hold peer mail.

Fix-now #6 and #7 (fluid half). The source makes wake metrics measurable from
the event log; before it, a peer wake and a user prompt looked identical. The
hold makes a host-owned (wake=False) peer message to a parked parent wait for
the parent's next wake instead of bouncing back to a host that woke it.
"""

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


def test_turn_sources_are_the_five_documented_values() -> None:
    assert TURN_SOURCES == ("user", "agent_message", "wakeup", "unpark", "promotion")


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
        yield StreamChunk("text_delta", text="ok")
        yield StreamChunk("done", stop_reason="end_turn")


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


async def test_background_wake_records_unpark() -> None:
    provider = _Provider()
    loop = await _parked_loop(provider)
    try:
        await loop.submit(BackgroundTaskFinished(task_id="task_1", summary="done"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert _sources(loop) == ["user", "unpark"]
    finally:
        await loop.aclose()


async def test_host_owned_peer_message_to_a_parked_parent_is_held() -> None:
    """wake=False used to raise before the park check, so the host woke the
    parent for every interim message. It is held now and costs nothing."""
    provider = _Provider()
    loop = await _parked_loop(provider)
    try:
        calls = len(provider.requests)
        await loop.submit(AgentMessage(text="halfway", sender="worker", wake=False))
        assert not loop.turn_running
        assert len(loop._queued_peer_messages) == 1
        assert len(provider.requests) == calls
        # The held text rides the next wake.
        await loop.submit(BackgroundTaskFinished(task_id="task_1", summary="done"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        assert "halfway" in provider.requests[-1].messages[-1].text()
    finally:
        await loop.aclose()


async def test_urgent_host_owned_message_to_a_parked_parent_is_refused() -> None:
    """Urgent skips the hold; with wake=False the host schedules the wake."""
    provider = _Provider()
    loop = await _parked_loop(provider)
    try:
        with pytest.raises(RuntimeError, match="no turn is running"):
            await loop.submit(AgentMessage(
                text="blocked on you", sender="worker", wake=False, urgent=True,
            ))
        assert loop._queued_peer_messages == []
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
