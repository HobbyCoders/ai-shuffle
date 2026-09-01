"""EventBus replay/ordering + Anthropic request-serialization tests."""

import asyncio
import contextlib
import dataclasses
import json
import time
import typing
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
from fluid.events.model import (
    SCHEMA_VERSION,
    UnknownEvent,
    event_from_dict,
    event_to_dict,
)
from fluid.messages import Message, Reasoning, Text
from fluid.providers.anthropic.serialize import build_request_body
from fluid.providers.base import ProviderRequest
from fluid.store.base import MemoryStore


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


# --- Codec tolerance: rows from another build never make a session unloadable


def test_codec_stamps_a_schema_version() -> None:
    row = event_to_dict(TextDelta(session_id="s1", index=0, text="hi"))
    assert row["_v"] == SCHEMA_VERSION == 1
    assert row["_cls"] == "TextDelta"


def test_unknown_class_decodes_to_a_placeholder() -> None:
    row = {"_cls": "FutureEvent", "_v": 1, "session_id": "s1", "seq": 7, "ts": 12.5,
           "payload": {"k": "v"}}
    event = event_from_dict(row)
    assert isinstance(event, UnknownEvent)
    assert event.cls_name == "FutureEvent"
    assert (event.session_id, event.seq, event.ts) == ("s1", 7, 12.5)
    assert event.raw == row
    # Written back byte-for-byte, so a rewrite of the file keeps the row.
    assert event_to_dict(event) == row


def test_extra_field_from_a_newer_build_is_dropped() -> None:
    row = event_to_dict(TextDelta(session_id="s1", index=0, text="hi"))
    row["added_later"] = True
    event = event_from_dict(row)
    assert isinstance(event, TextDelta)
    assert event.text == "hi"


def test_missing_required_field_decodes_to_a_placeholder() -> None:
    row = {"_cls": "TextDelta", "_v": 1, "session_id": "s1", "seq": 3}
    event = event_from_dict(row)
    assert isinstance(event, UnknownEvent)
    assert event.cls_name == "TextDelta" and event.seq == 3
    assert event.raw == row


def test_row_without_cls_at_all_decodes_to_a_placeholder() -> None:
    event = event_from_dict({"session_id": "s1"})
    assert isinstance(event, UnknownEvent)
    assert event.cls_name == "" and event.session_id == "s1"


def _minimal_instance(cls: type[Event]) -> Event:
    """Build ``cls`` with a dummy value for every field that has no default."""
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        hint = str(hints.get(f.name, ""))
        if "tuple" in hint:
            kwargs[f.name] = ("a", "b")
        elif "dict" in hint:
            kwargs[f.name] = {"k": 1}
        elif "list" in hint:
            kwargs[f.name] = [["x", "y"]]
        elif "float" in hint:
            kwargs[f.name] = 1.5
        elif "bool" in hint:
            kwargs[f.name] = True
        elif "int" in hint:
            kwargs[f.name] = 2
        else:
            kwargs[f.name] = f"{f.name}-value"
    return cls(**kwargs)


def test_every_registered_event_type_still_round_trips() -> None:
    from fluid.events import model as model_mod

    assert len(model_mod._EVENT_TYPES) >= 25
    for name, cls in model_mod._EVENT_TYPES.items():
        if cls.__module__ != model_mod.__name__:
            continue  # types registered by other tests in this process
        event = _minimal_instance(cls)
        row = json.loads(json.dumps(event_to_dict(event)))
        assert row["_v"] == SCHEMA_VERSION, name
        revived = event_from_dict(row)
        assert type(revived) is cls, name
        assert revived == event, name


# --- Bus seq continuity across a resume ----------------------------------------


async def test_seq_continues_after_the_persisted_maximum() -> None:
    """Two runs against one session id number their events as one stream."""
    store = MemoryStore()
    sid = "s-resume"

    async def run(bus: EventBus, n: int) -> None:
        for i in range(n):
            ev = await bus.publish(TextDelta(session_id=sid, index=0, text=f"t{i}"))
            await store.append_event(sid, ev)

    first = EventBus()
    await run(first, 5)
    assert first.last_seq(sid) == 5

    second = EventBus()  # a new process: fresh bus, same session on disk
    persisted = await store.events_since(sid, 0)
    second.seed(sid, max(e.seq for e in persisted))
    await run(second, 3)

    seqs = [e.seq for e in await store.events_since(sid, 0)]
    assert seqs == list(range(1, 9))
    assert [e.seq for e in await store.events_since(sid, 3)] == [4, 5, 6, 7, 8]
    # A seed never moves numbering backwards.
    second.seed(sid, 2)
    assert (await second.publish(TextDelta(session_id=sid, index=0, text="x"))).seq == 9


async def test_native_loop_seeds_the_bus_when_resuming(tmp_path) -> None:
    from fluid.engine.native import NativeLoop

    store = MemoryStore()
    meta = await store.create_session(cwd=str(tmp_path))
    sid = meta.session_id
    stale = EventBus()
    for i in range(4):
        await store.append_event(
            sid, await stale.publish(TextDelta(session_id=sid, index=0, text=f"t{i}"))
        )

    class Silent:
        name = "silent"

        async def stream(self, request):
            return
            yield

    loop = NativeLoop(provider=Silent(), store=store, model="m", session_id=sid)
    await loop._ensure_session()
    assert loop.bus.last_seq(sid) == 4
    assert loop.bus.replay_source is not None


# --- Bus memory: bounded ring, no state from lookups, prompt unsubscribe ------


async def test_log_lookup_of_an_unseen_session_creates_nothing() -> None:
    bus = EventBus()
    for i in range(1000):
        assert bus.log(f"missing-{i}") == []
        assert bus.last_seq(f"missing-{i}") == 0
    assert bus._logs == {} and bus._last_seq == {} and bus._subs == {}


async def test_in_process_log_is_a_ring() -> None:
    bus = EventBus(log_ring_size=16)
    for i in range(100):
        await bus.publish(TextDelta(session_id="s1", index=0, text=f"t{i}"))
    log = bus.log("s1")
    assert len(log) == 16
    assert [e.seq for e in log] == list(range(85, 101))
    assert bus.last_seq("s1") == 100 and bus.floor_seq("s1") == 85


async def test_subscribe_below_the_ring_floor_replays_from_the_store() -> None:
    store = MemoryStore()
    bus = EventBus(log_ring_size=4, replay_source=store.events_since)
    for i in range(10):
        ev = await bus.publish(TextDelta(session_id="s1", index=0, text=f"t{i}"))
        await store.append_event("s1", ev)
    seen: list[int] = []
    async with contextlib.aclosing(bus.subscribe("s1", since_seq=2)) as events:
        async for e in events:
            seen.append(e.seq)
            if e.seq == 10:
                break
    assert seen == list(range(3, 11))  # 3..6 from the store, 7..10 from the ring


async def test_live_subscriber_skips_persisted_history_on_resume() -> None:
    """The renderer's cursor is taken before the resume seed lands; it must
    not replay the previous run from the store when the seq jumps."""
    store = MemoryStore()
    sid = "s-live"
    old = EventBus()
    for i in range(3):
        await store.append_event(
            sid, await old.publish(TextDelta(session_id=sid, index=0, text=f"old{i}"))
        )
    bus = EventBus(replay_source=store.events_since)
    start = bus.last_seq(sid)  # 0: nothing known about the session yet
    seen: list[str] = []

    async def watch() -> None:
        async with contextlib.aclosing(
            bus.subscribe(sid, since_seq=start, from_store=False)
        ) as events:
            async for e in events:
                seen.append(e.text)
                if len(seen) == 2:
                    break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0.01)
    bus.seed(sid, 3)  # the engine opens the session mid-subscription
    await bus.publish(TextDelta(session_id=sid, index=0, text="new0"))
    await bus.publish(TextDelta(session_id=sid, index=0, text="new1"))
    await asyncio.wait_for(task, timeout=2)
    assert seen == ["new0", "new1"]


async def test_subscribe_below_the_floor_without_a_store_starts_at_the_floor() -> None:
    bus = EventBus(log_ring_size=4)
    for i in range(10):
        await bus.publish(TextDelta(session_id="s1", index=0, text=f"t{i}"))
    seen: list[int] = []
    async with contextlib.aclosing(bus.subscribe("s1", since_seq=0)) as events:
        async for e in events:
            seen.append(e.seq)
            if e.seq == 10:
                break
    assert seen == [7, 8, 9, 10]


async def test_closing_a_subscription_unregisters_at_once() -> None:
    bus = EventBus()
    await bus.publish(TextDelta(session_id="s1", index=0, text="t0"))
    for _ in range(200):
        async with contextlib.aclosing(bus.subscribe("s1")) as events:
            async for _event in events:
                break
    assert bus.subscriber_count("s1") == 0
    assert "s1" not in bus._subs  # the list itself is dropped, not just emptied


# ─── Anthropic history fidelity ──────────────────────────────────────────────


def test_unsigned_thinking_is_not_resent() -> None:
    from fluid.messages import Thinking

    request = ProviderRequest(
        model="m",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(role="assistant", content=[
                Thinking("cut off"),
                Reasoning(origin="anthropic",
                          raw={"type": "thinking", "thinking": "cut off", "signature": ""}),
                Text("answer"),
            ]),
        ],
    )
    body = build_request_body(request)
    assert body["messages"][1]["content"] == [{"type": "text", "text": "answer"}]


def test_empty_text_blocks_leave_the_wire() -> None:
    from fluid.messages import ToolResultBlock, ToolUseBlock

    request = ProviderRequest(
        model="m",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(role="assistant", content=[
                Text(""), ToolUseBlock(id="t1", name="Read", input={}),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="t1", content=[Text("")]),
            ]),
        ],
    )
    body = build_request_body(request)
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
    ]
    result = body["messages"][2]["content"][0]
    assert result["type"] == "tool_result"
    assert "content" not in result


def test_a_message_that_serializes_to_nothing_keeps_roles_alternating() -> None:
    from fluid.messages import Thinking

    request = ProviderRequest(
        model="m",
        messages=[
            Message(role="user", content=[Text("first")]),
            Message(role="assistant", content=[
                Thinking("unsigned"),
                Reasoning(origin="anthropic",
                          raw={"type": "thinking", "thinking": "unsigned", "signature": ""}),
            ]),
            Message(role="user", content=[Text("second")]),
        ],
    )
    body = build_request_body(request)
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert [b["text"] for b in body["messages"][0]["content"]] == ["first", "second"]


def test_redacted_thinking_and_server_tool_blocks_resend_verbatim() -> None:
    redacted = {"type": "redacted_thinking", "data": "OPAQUE"}
    search = {"type": "server_tool_use", "id": "srvtoolu_1",
              "name": "web_search", "input": {"query": "x"}}
    result = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []}
    request = ProviderRequest(
        model="m",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(role="assistant", content=[
                Reasoning(origin="anthropic", raw=redacted),
                Reasoning(origin="anthropic", raw=search),
                Reasoning(origin="anthropic", raw=result),
                Text("done"),
            ]),
        ],
    )
    body = build_request_body(request)
    assert body["messages"][1]["content"] == [
        redacted, search, result, {"type": "text", "text": "done"},
    ]
