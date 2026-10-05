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
async def test_native_compact_shows_the_compacted_card(monkeypatch):
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
