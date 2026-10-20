"""Review regressions: commit gaps and byte-evicted live consumers."""
import asyncio
import contextlib

import pytest

from fluid.events.bus import EventBus, _field_names, _retained_size
from fluid.events.model import TextDelta, ToolResult
from fluid.store.base import MemoryStore


@pytest.mark.parametrize("later_committed", [False, True])
async def test_catchup_waits_for_oversized_uncommitted_event(later_committed):
    committed = []
    async def replay(sid, seq):
        return [event for event in committed if event.seq > seq]
    bus = EventBus(log_ring_max_bytes=16384, replay_source=replay)
    first = await bus.publish(TextDelta(session_id="s", index=0, text="one"))
    committed.append(first)
    second = await bus.publish(ToolResult(session_id="s", tool_use_id="big", name="Read", output="x" * 40000))
    third = await bus.publish(TextDelta(session_id="s", index=0, text="three"))
    if later_committed:
        committed.append(third)
    async with contextlib.aclosing(bus.subscribe("s", since_seq=1)) as sub:
        pending = asyncio.create_task(anext(sub))
        await asyncio.sleep(0.1)
        assert not pending.done(), "must not skip uncommitted seq 2"
        committed[:] = [first, second, third]
        assert (await asyncio.wait_for(pending, 1)).seq == 2
        assert (await asyncio.wait_for(anext(sub), 1)).seq == 3


@pytest.mark.parametrize("durable", [False, True])
async def test_lagging_live_only_subscriber_does_not_lose_byte_evicted_events(durable):
    store = MemoryStore()
    bus = EventBus(subscriber_queue_size=4, log_ring_max_bytes=65536,
                   replay_source=store.events_since if durable else None)
    # Previously persisted history is not part of this live subscription.
    bus.seed("s", 3)
    async with contextlib.aclosing(bus.subscribe("s", from_store=False)) as sub:
        first = asyncio.create_task(anext(sub))
        await asyncio.sleep(0)
        for i in range(40):
            event = await bus.publish(ToolResult(session_id="s", tool_use_id=str(i), name="Read", output="x" * 8192))
            await store.append_event("s", event)
        seen = [(await asyncio.wait_for(first, 1)).seq]
        for _ in range(39):
            seen.append((await asyncio.wait_for(anext(sub), 1)).seq)
        assert seen == list(range(4, 44))
        if durable:
            assert bus.memory_stats()["event_bus_ring_estimated_bytes"] <= 65536
    assert bus.memory_stats()["event_bus_ring_estimated_bytes"] <= 65536


def test_retained_size_reuses_event_field_names():
    _field_names.cache_clear()
    event = TextDelta(session_id="s", index=0, text="text")
    assert _retained_size(event) == _retained_size(event)
    assert _field_names.cache_info().misses == 1
    assert _field_names.cache_info().hits == 1
