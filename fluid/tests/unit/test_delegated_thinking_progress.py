"""The Claude CLI's live thinking estimate reaches the host, and only live."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

from fluid.engine import delegated_proto
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.events.bus import EventBus
from fluid.events.model import TextDelta, ThinkingProgress
from fluid.store.base import RECONSTRUCTIBLE_EVENTS, persist_event


def _frame(estimate, **extra) -> dict:
    return {"type": "system", "subtype": "thinking_tokens", "uuid": "u", "estimated_tokens": estimate, **extra}


def _mapper() -> StreamJsonMapper:
    return StreamJsonMapper(session_id="s", turn_id="t", model="claude-opus-5-5", user_node_id="n")


def test_estimate_becomes_a_progress_event():
    events = _mapper().feed(_frame(12400)).events
    assert len(events) == 1
    assert isinstance(events[0], ThinkingProgress)
    assert events[0].estimated_tokens == 12400


def test_estimates_are_throttled(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(delegated_proto.time, "monotonic", lambda: clock[0])
    mapper = _mapper()
    assert mapper.feed(_frame(10)).events
    clock[0] += delegated_proto._THINKING_PROGRESS_SECONDS - 0.01
    assert not mapper.feed(_frame(20)).events
    clock[0] += 0.02
    assert mapper.feed(_frame(30)).events


def test_subagent_and_unusable_estimates_are_dropped():
    assert not _mapper().feed(_frame(50, parent_tool_use_id="tool-1")).events
    for bad in (0, -1, "12", True, None):
        assert not _mapper().feed(_frame(bad)).events


class _Store:
    """Counts both ways an event can be written."""

    def __init__(self) -> None:
        self.written: list = []

    async def append_event(self, session_id, event) -> None:
        self.written.append(event)

    async def append_stream_event(self, session_id, event) -> None:
        self.written.append(event)

    async def events_since(self, session_id, seq) -> list:
        return [e for e in self.written if e.seq > seq]


class _StoreWithoutBatching:
    def __init__(self) -> None:
        self.written: list = []

    async def append_event(self, session_id, event) -> None:
        self.written.append(event)


def _estimate(tokens: int = 900) -> ThinkingProgress:
    return ThinkingProgress(session_id="s", estimated_tokens=tokens)


def _delta(text: str = "x") -> TextDelta:
    return TextDelta(session_id="s", index=0, text=text)


def test_an_estimate_reaches_neither_write_path():
    async def scenario():
        for store in (_Store(), _StoreWithoutBatching()):
            await persist_event(store, _estimate())
            assert store.written == []

    asyncio.run(scenario())
    assert ThinkingProgress not in RECONSTRUCTIBLE_EVENTS


def test_an_estimate_takes_no_number_and_enters_no_log():
    async def scenario():
        bus = EventBus()
        first = await bus.publish(_delta())
        estimate = await bus.publish(_estimate())
        second = await bus.publish(_delta())

        assert estimate.seq == -1 and estimate.ts > 0
        assert (first.seq, second.seq) == (1, 2)
        assert bus.last_seq("s") == 2
        assert [e.seq for e in bus.log("s")] == [1, 2]

    asyncio.run(scenario())


def test_a_listener_gets_the_estimate_in_order():
    async def scenario():
        bus = EventBus()
        seen: list = []

        async def listen():
            async with aclosing(bus.subscribe("s")) as events:
                async for event in events:
                    seen.append(event)
                    if len(seen) == 3:
                        return

        async def settle():
            for _ in range(5):
                await asyncio.sleep(0)

        task = asyncio.create_task(listen())
        await settle()
        await bus.publish(_delta("a"))
        await settle()
        await bus.publish(_estimate(700))
        await settle()
        await bus.publish(_delta("b"))
        await asyncio.wait_for(task, 2)
        assert [type(e).__name__ for e in seen] == ["TextDelta", "ThinkingProgress", "TextDelta"]
        assert seen[1].estimated_tokens == 700

    asyncio.run(scenario())


def test_a_listener_catching_up_drops_an_estimate_the_stream_has_passed():
    """It would otherwise put an estimate back on the line after thinking ended."""

    async def scenario():
        bus = EventBus()
        seen: list = []

        async def listen():
            async with aclosing(bus.subscribe("s")) as events:
                async for event in events:
                    seen.append(event)
                    if event.seq == 3:
                        return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        # No yield between publishes: the listener reads these from the log.
        await bus.publish(_delta("a"))
        await bus.publish(_estimate(700))
        await bus.publish(_delta("b"))
        await bus.publish(_delta("c"))
        await asyncio.wait_for(task, 2)
        assert [e.seq for e in seen] == [1, 2, 3]

    asyncio.run(scenario())


def test_replay_after_reconnect_has_no_estimate_and_no_stall():
    """The hazard of live-only: a numbered event left unsaved would be a hole."""

    async def scenario():
        store = _Store()
        bus = EventBus(log_ring_size=1)
        bus.set_replay_source(store.events_since)
        for event in (_delta("a"), _estimate(), _delta("b"), _estimate(), _delta("c")):
            await persist_event(store, await bus.publish(event))

        assert [e.seq for e in store.written] == [1, 2, 3]
        seen: list = []
        started = asyncio.get_running_loop().time()

        async def reconnect():
            async with aclosing(bus.subscribe("s", since_seq=0)) as events:
                async for event in events:
                    seen.append(event)
                    if event.seq == 3:
                        return

        await asyncio.wait_for(reconnect(), 2)
        assert [e.seq for e in seen] == [1, 2, 3]
        assert not any(isinstance(e, ThinkingProgress) for e in seen)
        # One hole would cost REPLAY_GAP_GRACE_SECONDS (0.5s).
        assert asyncio.get_running_loop().time() - started < 0.4

    asyncio.run(scenario())


def test_a_late_listener_never_sees_an_estimate_from_before_it_joined():
    async def scenario():
        bus = EventBus()
        await bus.publish(_estimate())
        await bus.publish(_delta())
        seen: list = []
        async with aclosing(bus.subscribe("s", since_seq=0)) as events:
            async for event in events:
                seen.append(event)
                if event.seq == 1:
                    break
        assert [type(e).__name__ for e in seen] == ["TextDelta"]

    asyncio.run(scenario())
