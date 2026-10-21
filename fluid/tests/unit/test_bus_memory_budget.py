"""Event rings must be byte bounded without losing durable replay."""
import asyncio
import contextlib
import sys

from fluid.events.bus import EventBus
from fluid.events.model import ToolResult


async def test_deep_json_cannot_hide_an_oversize_payload():
    output = "x" * 40000
    for _ in range(30):
        output = [output]
    bus = EventBus(log_ring_max_bytes=4096)
    await bus.publish(ToolResult(session_id="deep", tool_use_id="deep", name="Read", output=output))
    assert bus.log("deep") == []
    assert bus.memory_stats()["event_bus_ring_estimated_bytes"] == 0


async def test_large_tool_results_do_not_fill_count_bounded_ring():
    bus = EventBus()
    for i in range(100):
        await bus.publish(ToolResult(session_id="s", tool_use_id=str(i), name="Read",
                                     output=str(i) + "x" * 262144))
    assert sum(sys.getsizeof(e.output) for e in bus.log("s")) <= 8 * 1024 * 1024
    assert bus.last_seq("s") == 100
    assert bus.floor_seq("s") > 1


async def test_byte_eviction_replays_gap_from_durable_source(tmp_path):
    from fluid.store.jsonl import JsonlStore
    store = JsonlStore(tmp_path)
    await store.create_session("s")
    bus = EventBus(log_ring_max_bytes=4096)
    for i in range(20):
        event = await bus.publish(ToolResult(
            session_id="s", tool_use_id=str(i), name="Read", output="x" * 2048))
        await store.append_event("s", event)  # the engine's real persistence path
    store.close()
    reopened = JsonlStore(tmp_path)
    bus.set_replay_source(reopened.events_since)
    assert len(bus.log("s")) < 20
    seen = []
    try:
        async with asyncio.timeout(2), contextlib.aclosing(bus.subscribe("s")) as sub:
            async for event in sub:
                seen.append(event.seq)
                if len(seen) == 20:
                    break
        assert seen == list(range(1, 21))
        assert bus.subscriber_count("s") == 0
        assert bus.memory_stats()["event_bus_ring_estimated_bytes"] <= 4096
    finally:
        reopened.close()


async def test_oversize_event_still_delivered_live():
    from fluid.store.base import MemoryStore
    store = MemoryStore()
    bus = EventBus(log_ring_max_bytes=1024, replay_source=store.events_since)
    sub = bus.subscribe("s", from_store=False)
    receive = asyncio.create_task(anext(sub))
    await asyncio.sleep(0)
    await bus.publish(ToolResult(session_id="s", tool_use_id="big", name="Read",
                                 output="x" * 5000))
    event = await asyncio.wait_for(receive, 1)
    assert len(event.output) == 5000
    assert not bus.log("s")
    assert bus.floor_seq("s") == 2
    await sub.aclose()


async def test_delegated_emit_byte_bounds_active_ring_and_replays_every_event(tmp_path):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events.model import TurnComplete
    from fluid.store.jsonl import JsonlStore

    store = JsonlStore(tmp_path)
    await store.create_session("s")
    bus = EventBus(subscriber_queue_size=4, log_ring_max_bytes=16384)
    loop = DelegatedLoop(store=store, model="fake", bus=bus)
    assert bus.replay_source is not None
    try:
        async with contextlib.aclosing(bus.subscribe("s", from_store=False)) as sub:
            first = asyncio.create_task(anext(sub))
            await asyncio.sleep(0)
            emitted = []
            for i in range(40):
                emitted.append(await loop._emit(ToolResult(
                    session_id="s", tool_use_id=str(i), name="Read", output="x" * 8192)))
                assert bus.memory_stats()["event_bus_ring_estimated_bytes"] <= 16384
            emitted.append(await loop._emit(TurnComplete(
                session_id="s", stop_reason="end_turn", num_model_calls=1)))
            seen = [await asyncio.wait_for(first, 1)]
            async with asyncio.timeout(2):
                while len(seen) < len(emitted):
                    seen.append(await anext(sub))
            assert seen == emitted
            assert await store.events_since("s", 0) == emitted
            assert len(bus.log("s")) < len(emitted)
    finally:
        await loop.aclose()
        store.close()


def test_delegated_preserves_explicit_replay_source():
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    store = MemoryStore()
    source = store.events_since
    bus = EventBus(replay_source=source)
    DelegatedLoop(store=store, model="fake", bus=bus)
    assert bus.replay_source is source


def test_delegated_argv_only_without_store():
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop(store=None, model="opus")
    assert loop.bus.replay_source is None
    assert "opus" in loop._build_argv()
