"""Delegated child text is live immediately but persisted in bounded batches."""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock

from app.core import query_engine as qe, turn_recorder as tr
from app.core.provider.events import AssistantMessage, TextBlock
from tests.core.engine_harness import StubPool, collect
from tests.core.test_cross_turn_background_subagent_routing import (
    _chat_patches, _fake_state, _stream_text_delta,
)


async def test_final_child_block_does_not_repeat_streamed_text():
    messages = [_stream_text_delta("child", "hello "), _stream_text_delta("child", "world")]
    messages += [AssistantMessage(content=[TextBlock(text="hello world")],
                                  model="claude", parent_tool_use_id="child")]
    # A later message without partials must still use the fallback.
    messages += [AssistantMessage(content=[TextBlock(text="next")],
                                  model="claude", parent_tool_use_id="child")]
    with ExitStack() as stack:
        _chat_patches(stack, StubPool(_fake_state(messages)))
        frames = await collect(qe._stream_to_websocket_impl("prompt", "coalesce-child", "claude-code"))
    assert [f["content"] for f in frames if f["type"] == "subagent_chunk"] == ["hello ", "world", "next"]


def _recorder():
    rec = tr.TurnRecorder("coalesce", None, None, None, "runner")
    rec._append = AsyncMock()
    return rec


def _chunks(rec):
    return [event for call in rec._append.call_args_list for event in call.args[0]
            if event["type"] == "subagent.progress"]


async def test_thousands_of_tokens_are_batched_and_flushed_before_tool(monkeypatch):
    rec = _recorder()
    monkeypatch.setattr(tr, "FLUSH_INTERVAL_SECONDS", 60)
    for _ in range(5000):
        await rec._on_event_locked({"type": "subagent_chunk", "agent_id": "child", "content": "x"})
    await rec._on_event_locked({"type": "subagent_tool_use", "agent_id": "child", "name": "Read"})
    chunks = [e for e in _chunks(rec) if e["payload"]["kind"] == "chunk"]
    assert len(chunks) == 3
    assert "".join(e["payload"]["payload"]["content"] for e in chunks) == "x" * 5000
    assert _chunks(rec)[-1]["payload"]["kind"] == "tool_use"


async def test_timer_flushes_without_another_provider_event(monkeypatch):
    monkeypatch.setattr(tr, "FLUSH_INTERVAL_SECONDS", 0.001)
    rec = _recorder()
    await rec._on_event_locked({"type": "subagent_chunk", "agent_id": "a", "content": "one"})
    await rec._on_event_locked({"type": "subagent_chunk", "agent_id": "b", "content": "two"})
    await asyncio.wait_for(rec._flush_task, 1)
    assert [(e["payload"]["task_id"], e["payload"]["payload"]["content"]) for e in _chunks(rec)] == [("a", "one"), ("b", "two")]
