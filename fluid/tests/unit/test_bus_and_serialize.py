"""EventBus replay/ordering + Anthropic request-serialization tests."""

import asyncio

from omni.events import EventBus, TextDelta
from omni.messages import Message, Reasoning, Text
from omni.providers.anthropic.serialize import build_request_body
from omni.providers.base import ProviderRequest


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
