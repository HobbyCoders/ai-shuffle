"""Legacy child-frame batching; current provider messages do not use that lane."""
import asyncio
from unittest.mock import AsyncMock

from fluid.events import BlockStop, TextDelta

from app.core import turn_recorder as tr
from app.core.provider.events import AssistantMessage
from app.core.provider.factory import make_adapter
from app.core.provider.types import SessionOptions


def test_current_producer_does_not_emit_legacy_child_messages():
    adapter = make_adapter()
    adapter._options = SessionOptions(model="fake", include_partial_messages=True)
    delta = adapter._translate(TextDelta(session_id="worker", index=1, text="hello"))
    completed = adapter._translate(BlockStop(session_id="worker", index=1))
    final = next(event for event in completed if isinstance(event, AssistantMessage))
    assert delta[0].event["index"] == 1
    assert final.content[0].text == "hello"
    # Even worker-session events have no parent tool id. Real worker progress
    # uses WorkerFeed -> record_drained_event, not this legacy query-engine lane.
    assert all(event.parent_tool_use_id is None for event in delta + completed)


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
