"""EventBus replay/ordering + Anthropic request-serialization tests."""

import asyncio
import time
from dataclasses import dataclass

import pytest

from fluid.events import (
    WAKE_AUTO,
    WAKE_NOT_APPLICABLE,
    WAKE_SUPPRESSED,
    Event,
    EventBus,
    SubagentEvent,
    TextDelta,
    register_event_type,
)
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Reasoning, Text
from fluid.providers.anthropic.serialize import build_request_body
from fluid.providers.base import ProviderRequest


async def test_bus_seq_and_replay() -> None:
    bus = EventBus()
    for i in range(5):
        await bus.publish(TextDelta(session_id="s1", index=0, text=f"t{i}"))

    seen: list[str] = []

    async def consume() -> None:
        async for e in bus.subscribe("s1", since_seq=2):
            seen.append(e.text)
            if len(seen) == 5:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(TextDelta(session_id="s1", index=0, text="t5"))
    await bus.publish(TextDelta(session_id="s1", index=0, text="t6"))
    await asyncio.wait_for(task, timeout=2)
    # replayed t2..t4 (since_seq=2 is exclusive), then live t5, t6
    assert seen == ["t2", "t3", "t4", "t5", "t6"]


async def test_bus_lagging_subscriber_recovers_from_log() -> None:
    bus = EventBus(subscriber_queue_size=2)
    received: list[str] = []
    started = asyncio.Event()

    async def slow_consume() -> None:
        async for e in bus.subscribe("s1"):
            started.set()
            received.append(e.text)
            if len(received) == 10:
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(slow_consume())
    await asyncio.sleep(0.01)
    for i in range(10):  # overflow the size-2 queue while consumer sleeps
        await bus.publish(TextDelta(session_id="s1", index=0, text=f"t{i}"))
    await asyncio.wait_for(task, timeout=5)
    assert received == [f"t{i}" for i in range(10)]  # nothing lost, order kept


def test_cache_breakpoints_placed_on_system_and_history_tail() -> None:
    request = ProviderRequest(
        model="m",
        system="be helpful",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(role="assistant", content=[Text("hello")]),
            Message(role="user", content=[Text("again")]),
        ],
        cache_breakpoints=2,
    )
    body = build_request_body(request)
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # only the two allowed positions are marked
    marked = sum(
        1
        for m in body["messages"]
        for c in m["content"]
        if "cache_control" in c
    )
    assert marked == 1


def test_foreign_reasoning_dropped_anthropic_kept() -> None:
    request = ProviderRequest(
        model="m",
        messages=[
            Message(role="assistant", content=[
                Reasoning(origin="openai_responses", encrypted_content="xxx"),
                Reasoning(origin="anthropic",
                          raw={"type": "thinking", "thinking": "t", "signature": "s"}),
                Text("answer"),
            ]),
            Message(role="user", content=[Text("next")]),
        ],
    )
    body = build_request_body(request)
    first = body["messages"][0]["content"]
    assert first == [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": "answer"},
    ]


def test_effort_maps_to_output_config() -> None:
    request = ProviderRequest(
        model="m",
        messages=[Message(role="user", content=[Text("hi")])],
        effort="xhigh",
    )
    body = build_request_body(request)
    assert body["output_config"] == {"effort": "xhigh"}


def test_no_effort_no_output_config() -> None:
    request = ProviderRequest(
        model="m",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    body = build_request_body(request)
    assert "output_config" not in body


# --- Codec registration hook -------------------------------------------------


def test_register_event_type_round_trips_a_foreign_event() -> None:
    """An embedder's own Event subclass survives the durable codec."""

    @dataclass(frozen=True, kw_only=True)
    class HostAlert(Event):
        code: str
        detail: str = ""

    assert register_event_type(HostAlert) is HostAlert
    event = HostAlert(session_id="s1", turn_id="t1", code="quota", detail="soft")
    revived = event_from_dict(event_to_dict(event))
    assert isinstance(revived, HostAlert)
    assert revived == event


def test_register_event_type_is_idempotent() -> None:
    @dataclass(frozen=True, kw_only=True)
    class Reregistered(Event):
        pass

    register_event_type(Reregistered)
    register_event_type(Reregistered)
    assert event_from_dict(event_to_dict(Reregistered(session_id="s"))) == (
        Reregistered(session_id="s")
    )


def test_register_event_type_refuses_to_shadow_a_builtin_name() -> None:
    """Taking over ``Usage`` would decode fluid's own events into the impostor."""

    @dataclass(frozen=True, kw_only=True)
    class Usage(Event):
        pass

    with pytest.raises(ValueError):
        register_event_type(Usage)


# ─── event timestamps ────────────────────────────────────────────────────────


async def test_publish_stamps_wall_clock_alongside_seq() -> None:
    """Without a ts, a persisted stream cannot answer how long anything took."""
    bus = EventBus()
    before = time.time()
    published = await bus.publish(TextDelta(session_id="s-ts", index=0, text="x"))
    after = time.time()

    assert published.seq == 1
    assert before <= published.ts <= after
    assert bus.log("s-ts")[0].ts == published.ts


async def test_publish_keeps_a_timestamp_the_emitter_already_set() -> None:
    """A replayed event carries its original time, not the time of the replay."""
    bus = EventBus()
    published = await bus.publish(
        TextDelta(session_id="s-ts2", index=0, text="x", ts=1234.5)
    )
    assert published.ts == 1234.5


def test_unpublished_events_are_unstamped_not_epoch_zero_in_disguise() -> None:
    """The default must be inert so two identical events compare equal."""
    a = TextDelta(session_id="s", index=0, text="x")
    b = TextDelta(session_id="s", index=0, text="x")
    assert a.ts == 0.0
    assert a == b


def test_ts_round_trips_through_the_durable_codec() -> None:
    event = TextDelta(session_id="s", index=0, text="x", seq=3, ts=1700000000.25)
    assert event_from_dict(event_to_dict(event)) == event


def test_rows_persisted_before_ts_existed_still_decode() -> None:
    """Stored events predate the field; they decode as unstamped."""
    legacy = {"_cls": "TextDelta", "session_id": "s", "seq": 4, "index": 0, "text": "x"}
    revived = event_from_dict(legacy)
    assert isinstance(revived, TextDelta)
    assert revived.ts == 0.0


# ─── wake disposition ────────────────────────────────────────────────────────


def test_wake_disposition_defaults_to_auto() -> None:
    event = SubagentEvent(session_id="s", task_id="t1", kind="finished")
    assert event.wake is True
    assert event.wake_disposition == WAKE_AUTO


def test_wake_disposition_separates_suppressed_from_not_applicable() -> None:
    """``wake=False`` alone cannot say whether a wake was owed."""
    suppressed = SubagentEvent(
        session_id="s",
        task_id="t1",
        kind="finished",
        wake=False,
        wake_disposition=WAKE_SUPPRESSED,
    )
    started = SubagentEvent(
        session_id="s",
        task_id="t1",
        kind="started",
        wake_disposition=WAKE_NOT_APPLICABLE,
    )
    assert suppressed.wake is False
    assert suppressed.wake_disposition == WAKE_SUPPRESSED
    # A started event says wake=True only because that is the default; its
    # disposition is what actually carries the meaning.
    assert started.wake is True
    assert started.wake_disposition == WAKE_NOT_APPLICABLE


def test_wake_disposition_round_trips_through_the_durable_codec() -> None:
    event = SubagentEvent(
        session_id="s",
        task_id="t1",
        kind="failed",
        wake=False,
        wake_disposition=WAKE_SUPPRESSED,
    )
    assert event_from_dict(event_to_dict(event)) == event


def test_rows_persisted_before_wake_disposition_existed_still_decode() -> None:
    """An old row's ``wake=True`` already meant ``auto``; it decodes that way."""
    legacy = {
        "_cls": "SubagentEvent",
        "session_id": "s",
        "seq": 9,
        "task_id": "t1",
        "kind": "finished",
        "wake": True,
    }
    revived = event_from_dict(legacy)
    assert isinstance(revived, SubagentEvent)
    assert revived.wake_disposition == WAKE_AUTO
