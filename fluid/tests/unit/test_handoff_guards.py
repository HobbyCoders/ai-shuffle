"""A handoff's model call sees which tools the session used, without their
arguments or output and within a bound; only one handoff runs at a time; and a
closing engine refuses a handoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fluid.engine.native as native_mod
import pytest
from fluid.engine.native import NativeLoop, _clip, _handoff_transcript
from fluid.events import Handoff, HandoffCreated
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode

SECRET = "sk-test-SECRET-0123456789"


class _HandoffProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, *, hold: bool = False) -> None:
        self.requests: list[ProviderRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 700}})
        self.started.set()
        await self.release.wait()
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


async def test_a_second_handoff_while_one_runs_fails_fast() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hold=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    first = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    with pytest.raises(RuntimeError, match="another handoff is already running"):
        await asyncio.wait_for(loop.submit(Handoff()), 5)
    assert len(provider.requests) == 1

    provider.release.set()  # the one handoff running still completes
    await asyncio.wait_for(first, 5)
    created = [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    assert len(created) == 1 and created[0].new_session_id in store._sessions
    assert loop._handoff_running is False
    await loop.aclose()


async def test_a_handoff_on_a_closed_loop_creates_no_session() -> None:
    store = MemoryStore()
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m")
    await loop.aclose()
    with pytest.raises(RuntimeError, match="the engine is closing"):
        await loop.submit(Handoff())
    assert store._sessions == {}
