"""A native session's ``/compact`` reaches the chat as the same cards Claude
Code's does (#1831): "Context Compacted" with the sizes when it worked, and
"Compaction failed" with the reason when nothing could be reclaimed. The
command goes to the engine bare, without the turn note."""

import pytest

from app.core.provider.events import ResultMessage, SystemMessage
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.engine.native import NativeLoop
from fluid.providers.base import StreamChunk
from fluid.store.base import MemoryStore
from tests.test_harness_adapter import FakeProvider, _collect

_REPLY = [
    StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
    StreamChunk("block_start", {"index": 0, "block_type": "text"}),
    StreamChunk("text_delta", {"index": 0, "text": "ok " + "pad " * 300}),
    StreamChunk("block_stop", {"index": 0}),
    StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
]


async def _adapter(monkeypatch):
    loop = NativeLoop(provider=FakeProvider(_REPLY), store=MemoryStore(), model="fake")
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    monkeypatch.setattr(adapter, "_turn_context_note", lambda: "TURN NOTE")
    await loop._ensure_session()
    return adapter, loop


def _cards(events, subtype):
    return [e for e in events if isinstance(e, SystemMessage) and e.subtype == subtype]


@pytest.mark.asyncio
async def test_native_compact_shows_the_compacted_card(mock_db, monkeypatch):
    from fluid.events import ContextCompaction
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions, session_events

    sessions.create_session("native-manual-compact", "default")
    adapter, loop = await _adapter(monkeypatch)
    try:
        for i in range(6):
            adapter.set_turn_source("user")
            await adapter.send_query(f"turn {i} " + "words " * 200)
            await _collect(adapter)
        adapter.set_turn_source("user")
        await adapter.send_query("/compact")
        events = await _collect(adapter)
        [card] = _cards(events, "compact_boundary")
        meta = card.data["compact_metadata"]
        assert meta["trigger"] == "manual"
        assert meta["pre_tokens"] > meta["post_tokens"] > 0
        assert not _cards(events, "compact_failed")
        assert isinstance(events[-1], ResultMessage) and not events[-1].is_error
        # The command went to the engine bare: no note became part of it.
        nodes = await loop.store.load_path(loop.session_id)
        assert not any("/compact" in str(n.message.content) for n in nodes)
        rec = await TurnRecorder.maybe_create("native-manual-compact")
        for card in _cards(events, "compact_boundary"):
            await rec.on_event({"type": "system", "subtype": card.subtype, "data": card.data})
        await rec.close()
        before = [r for r in session_events.get_events("native-manual-compact")
                  if r["event_type"] == "system.compaction_boundary"]
        assert len(before) == 1
        # A fresh adapter has no /compact-request flag. Persisted mechanical
        # rungs must still stay invisible and replay must not duplicate summary.
        fresh = HarnessAdapter()
        rec = await TurnRecorder.maybe_create("native-manual-compact")
        for event in await loop.store.events_since(loop.session_id, -1):
            if isinstance(event, ContextCompaction):
                for card in fresh._translate(event):
                    await rec.on_event({"type": "system", "subtype": card.subtype, "data": card.data})
        await rec.close()
        after = [r for r in session_events.get_events("native-manual-compact")
                 if r["event_type"] == "system.compaction_boundary"]
        assert after == before
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_native_compact_with_nothing_to_reclaim_shows_the_failed_card(monkeypatch):
    adapter, _ = await _adapter(monkeypatch)
    try:
        adapter.set_turn_source("wakeup")
        await adapter.send_query("/compact")
        events = await _collect(adapter)
        [card] = _cards(events, "compact_failed")
        assert card.data["requested_by"] == "app"
        assert card.data["reason"]
        assert not _cards(events, "compact_boundary")
    finally:
        await adapter.disconnect()

@pytest.mark.asyncio
async def test_native_automatic_compaction_is_recorded_for_reload(mock_db, monkeypatch):
    from fluid.context.compaction import CompactionConfig
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions, session_events

    sessions.create_session("native-compact-record", "default")
    # Drive real NativeLoop turns with billed usage above the arming threshold,
    # so the compactor (not this test) produces the promotions and sizes.
    chunks = [StreamChunk("message_start", {"usage": {"input_tokens": 3000}}), *_REPLY[1:]]
    loop = NativeLoop(provider=FakeProvider(chunks), store=MemoryStore(), model="fake",
        compaction=CompactionConfig(trigger_tokens=500, trigger_window_ratio=0,
                                   keep_recent_turns=1))
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    monkeypatch.setattr(adapter, "_turn_context_note", lambda: "")
    rec = await TurnRecorder.maybe_create("native-compact-record")
    try:
        await loop._ensure_session()
        for i in range(6):
            await adapter.send_query(f"turn {i} " + "words " * 200)
            for event in await _collect(adapter):
                if isinstance(event, SystemMessage):
                    await rec.on_event({"type": "system", "subtype": event.subtype, "data": event.data})
        await rec.close()
        rows = session_events.get_events("native-compact-record")
        cards = [r for r in rows if r["event_type"] == "system.compaction_boundary"]
        assert cards, "automatic native promotions never reached the durable transcript"
        assert all(r["created_at"] for r in cards)
        metadata = cards[0]["payload"]["data"]["compact_metadata"]
        assert metadata["trigger"] == "auto"
        assert metadata["pre_tokens"] > metadata["post_tokens"] > 0
    finally:
        await rec.close()
        await adapter.disconnect()

async def _multi_promotion_turn(mock_db, monkeypatch, profile_id="default"):
    from fluid.context.compaction import CompactionConfig
    from fluid.context.trim import TrimConfig
    from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
    from fluid.store.base import SessionNode
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions

    sid = "native-multi-compact"
    sessions.create_session(sid, profile_id)
    loop = NativeLoop(provider=FakeProvider(_REPLY), store=MemoryStore(), model="fake",
        context_window=100_000,
        compaction=CompactionConfig(trigger_tokens=2000, trigger_window_ratio=0, cache_schedule=False,
            keep_recent_turns=50, recency_tail_tokens=0, trim=TrimConfig(trigger_tokens=2000,
                keep_recent_tool_results=2, min_result_chars=500)))
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    monkeypatch.setattr(adapter, "_turn_context_note", lambda: "")
    await loop._ensure_session()
    parent = None
    for i in range(5):
        messages = [
            Message(role="user", content=[Text(f"question {i}")]),
            Message(role="assistant", content=[ToolUseBlock(id=f"call{i}", name="Read", input={})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id=f"call{i}",
                content=[Image(media_type="image/png", data="a" * 20_000)] if i == 0
                else [Text("x" * 4000)])]),
            Message(role="assistant", content=[Text("done")]),
        ]
        for j, message in enumerate(messages):
            node = SessionNode(node_id=f"node_{i}_{j}", parent_id=parent, message=message)
            await loop.store.append(loop.session_id, node)
            parent = node.node_id
    rec = await TurnRecorder.maybe_create(sid)
    await adapter.send_query("next question")
    events = await _collect(adapter)
    for event in events:
        if isinstance(event, SystemMessage):
            await rec.on_event({"type": "system", "subtype": event.subtype, "data": event.data})
    await rec.close()
    return sid, adapter, loop, events


@pytest.mark.asyncio
async def test_multi_promotion_batch_records_one_card(mock_db, monkeypatch):
    from fluid.events import ContextCompaction
    from app.db import session_events

    sid, adapter, loop, events = await _multi_promotion_turn(mock_db, monkeypatch)
    try:
        promotions = [e for e in await loop.store.events_since(loop.session_id, -1)
                      if isinstance(e, ContextCompaction)]
        assert [e.reason for e in promotions] == ["image_consumed", "trigger"]
        [card] = _cards(events, "compact_boundary")
        [row] = [r for r in session_events.get_events(sid)
                 if r["event_type"] == "system.compaction_boundary"]
        assert row["payload"]["data"] == card.data
        assert card.data["compact_metadata"] == {
            "trigger": "auto", "pre_tokens": promotions[0].before_tokens,
            "post_tokens": promotions[-1].after_tokens, "context_window": 100_000,
        }
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_compaction_replay_through_fresh_adapter_and_recorder_is_idempotent(mock_db, monkeypatch):
    from fluid.events import ContextCompaction
    from fluid.events.model import event_from_dict, event_to_dict
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events

    sid, adapter, loop, _ = await _multi_promotion_turn(mock_db, monkeypatch)
    try:
        before = [r for r in session_events.get_events(sid)
                  if r["event_type"] == "system.compaction_boundary"]
        fresh = HarnessAdapter()
        fresh._engine = loop
        fresh._options = SessionOptions(model="fake")
        loop.context_window = 200_000  # recovery's current model is not the boundary's window
        rec = await TurnRecorder.maybe_create(sid)
        for event in await loop.store.events_since(loop.session_id, -1):
            if isinstance(event, ContextCompaction):
                # Recovery reads codec-decoded rows, not the live objects.
                restored = event_from_dict(event_to_dict(event))
                for card in fresh._translate(restored):
                    await rec.on_event({"type": "system", "subtype": card.subtype, "data": card.data})
        await rec.close()
        after = [r for r in session_events.get_events(sid)
                 if r["event_type"] == "system.compaction_boundary"]
        assert after == before
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_overflow_escalation_batch_card_spans_the_whole_batch(mock_db):
    """A provider-overflow escalation sizes each rung on its own (the ladder
    climbs as far as the window needs in one call). Its single card must read
    the batch's first before -> last after, not just the last rung's step."""
    from fluid.context.compaction import CompactionConfig
    from fluid.events import ContextCompaction
    from fluid.events.model import event_from_dict, event_to_dict
    from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
    from fluid.store.base import SessionNode
    from app.core.turn_recorder import TurnRecorder
    from app.db import sessions, session_events

    def node(nid, role, blocks):
        return SessionNode(node_id=nid, parent_id=None, message=Message(role=role, content=blocks))

    nodes = []
    for t in [0, 1, 2, 3, 9]:
        size = 40_000 if t == 9 else 8_000
        nodes.append(node(f"u{t}", "user", [Text(f"turn {t}")]))
        for k in range(3):
            nodes.append(node(f"a{t}_{k}", "assistant", [
                ToolUseBlock(id=f"c{t}_{k}", name="Read", input={"file_path": f"{k}.py"})]))
            nodes.append(node(f"r{t}_{k}", "user", [ToolResultBlock(
                tool_use_id=f"c{t}_{k}", content=[Text(f"payload-{k} " + "x" * size)])]))
        if t != 9:
            nodes.append(node(f"done{t}", "assistant", [Text("done " + "p " * 2000)]))

    sessions.create_session("native-escalation-card", "default")
    loop = NativeLoop(provider=FakeProvider(_REPLY), store=MemoryStore(), model="fake",
                      context_window=20_000, compaction=CompactionConfig(keep_recent_turns=1))
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    sid = (await loop._ensure_session()).session_id
    escalations = loop._compactor.escalate(
        nodes, overhead_tokens=0, context_window=20_000, output_reserve_tokens=32_000)
    assert len(escalations) >= 2
    assert escalations[-1].before_tokens < escalations[0].before_tokens
    # Emitted exactly as NativeLoop's overflow recovery emits a batch.
    for index, promo in enumerate(escalations):
        await loop._emit_compaction(
            sid, "turn-1", promo, batch_final=index == len(escalations) - 1,
            batch_before_tokens=escalations[0].before_tokens)
    rec = await TurnRecorder.maybe_create("native-escalation-card")
    for event in loop.bus.log(sid):
        if isinstance(event, ContextCompaction):
            # Recovery reads codec-decoded rows; the batch start must survive.
            for card in adapter._translate(event_from_dict(event_to_dict(event))):
                await rec.on_event({"type": "system", "subtype": card.subtype, "data": card.data})
    await rec.close()
    [row] = [r for r in session_events.get_events("native-escalation-card")
             if r["event_type"] == "system.compaction_boundary"]
    assert row["payload"]["data"]["compact_metadata"] == {
        "trigger": "auto", "pre_tokens": escalations[0].before_tokens,
        "post_tokens": escalations[-1].after_tokens, "context_window": 20_000,
    }
