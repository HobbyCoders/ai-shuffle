"""Checkpoint producers book once and stop through the host's idle teardown."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import archive_guard, query_persistence, query_session_lifecycle
from app.core.provider.events import ResultMessage
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.db import database
from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM
from fluid.context.compaction import CompactionConfig
from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, Usage, UserTurn
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore, SessionNode


class Provider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.main_started = asyncio.Event()
        self.main_release = asyncio.Event()
        self.hold_main = False

    def estimate_cost(self, model, usage):
        return 0.125

    async def stream(self, request):
        yield StreamChunk("message_start", {"usage": {
            "input_tokens": 100, "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 10,
        }})
        if request.system == CHECKPOINT_SYSTEM:
            self.started.set()
            await self.release.wait()
        elif self.hold_main:
            self.main_started.set()
            await self.main_release.wait()
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "checkpoint or reply"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 7}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {"stop_reason": "end_turn"})


async def setup_loop(monkeypatch):
    sid = "checkpoint-fixture"
    database.create_session(session_id=sid, profile_id="default")
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="fixture", extra={"app_session_id": sid})
    provider = Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="fixture", context_window=40_000,
        background_usage_sink=adapter._book_background_usage,
        compaction=CompactionConfig(
            summary_enabled=True, keep_recent_turns=1, recency_tail_tokens=0,
            max_resident_span_digests=1000, trigger_tokens=10_000_000,
        ),
    )
    adapter._engine = loop
    adapter._connected = True
    await loop._ensure_session()
    adapter._sdk_session_id = loop.session_id
    path = []
    for i in range(30):
        node = SessionNode(
            node_id=f"node_{i}", parent_id=path[-1].node_id if path else None,
            message=Message(role="user" if i % 2 == 0 else "assistant",
                            content=[Text(text="word " * 2000)]),
        )
        await loop.store.append(loop.session_id, node)
        path.append(node)
    loop._compactor._digest_until(path, overhead_tokens=0, target=0)
    monkeypatch.setattr("app.core.usage_attribution.usage_attribution", lambda _: {})
    return sid, adapter, loop, provider


async def collect_turn(adapter, loop):
    messages = [message async for message in adapter.receive_events()]
    await asyncio.wait_for(loop.wait_turn(), 5)
    assert not any(isinstance(e, ErrorEvent) for e in loop.bus.log(loop.session_id))
    terminal = next(m for m in messages if isinstance(m, ResultMessage))
    assert not terminal.is_error
    assert terminal.stop_reason == "end_turn"
    return terminal


async def persist(sid, result):
    await query_persistence._persist_turn_results(
        sid, None, {
            "usage_settlement_key": "checkpoint-test-turn",
            "tokens_in": result.usage["input_tokens"],
            "tokens_out": result.usage["output_tokens"],
            "cache_read_tokens": result.usage["cache_read_input_tokens"],
            "cache_creation_tokens": result.usage["cache_creation_input_tokens"],
            "total_cost_usd": result.total_cost_usd,
            "num_turns": result.num_turns,
        }, None,
    )


@pytest.mark.parametrize("during_turn", [False, True])
async def test_background_books_without_terminal_and_never_in_next_turn(mock_db, monkeypatch, during_turn):
    sid, adapter, loop, provider = await setup_loop(monkeypatch)
    try:
        loop._schedule_checkpoint(loop.session_id, "origin")
        task = loop._summary_task
        await asyncio.wait_for(provider.started.wait(), 3)
        if during_turn:
            provider.hold_main = True
            await loop.submit(UserTurn(content="continue"))
            await asyncio.wait_for(provider.main_started.wait(), 3)
        provider.release.set()
        await asyncio.wait_for(task, 3)
        row = database.get_session(sid)
        assert row["total_cost_usd"] == 0.125
        assert row["total_tokens_in"] == 100
        assert row["total_tokens_out"] == 7
        assert row["total_cache_read_tokens"] == 20
        assert row["total_cache_creation_tokens"] == 10
        [event] = [e for e in loop.bus.log(loop.session_id)
                   if isinstance(e, Usage) and e.raw.get("auxiliary_background")]
        assert adapter._translate(event) == []
        # Re-delivery uses the same durable claim, not token equality.
        await adapter._book_background_usage(event)
        assert database.get_session(sid)["total_cost_usd"] == 0.125
        if not during_turn:
            await loop.submit(UserTurn(content="next"))
        provider.main_release.set()
        terminal = await collect_turn(adapter, loop)
        assert terminal.total_cost_usd == 0.125
        assert terminal.usage["input_tokens"] == 100
        await persist(sid, terminal)
        await persist(sid, terminal)
        row = database.get_session(sid)
        assert row["total_cost_usd"] == 0.25
        assert row["total_tokens_in"] == 200
        assert row["total_tokens_out"] == 14
        assert mock_db.execute("SELECT COUNT(*) FROM usage_log WHERE session_id=?", (sid,)).fetchone()[0] == 2
    finally:
        await loop.aclose()


async def test_manual_summary_books_via_terminal_only(mock_db, monkeypatch):
    sid, adapter, loop, provider = await setup_loop(monkeypatch)
    provider.release.set()
    try:
        await loop.submit(UserTurn(content="/compact"))
        terminal = await collect_turn(adapter, loop)
        assert terminal.total_cost_usd == 0.125
        assert database.get_session(sid)["total_cost_usd"] == 0
        await persist(sid, terminal)
        await persist(sid, terminal)
        assert database.get_session(sid)["total_cost_usd"] == 0.125
        assert database.get_session(sid)["total_tokens_in"] == 100
    finally:
        await loop.aclose()


@pytest.mark.parametrize("archive", [False, True])
async def test_idle_host_stop_cancels_and_books_reported_usage(mock_db, monkeypatch, archive):
    sid, adapter, loop, provider = await setup_loop(monkeypatch)
    monkeypatch.setattr(query_session_lifecycle, "get_session_pool", lambda: SimpleNamespace(
        peek=lambda _: SimpleNamespace(adapter=adapter),
    ))
    monkeypatch.setattr(query_session_lifecycle, "enter_user_stop", AsyncMock())
    monkeypatch.setattr("app.core.turn_manager.interrupt_async", AsyncMock())
    try:
        loop._schedule_checkpoint(loop.session_id, "origin")
        task = loop._summary_task
        await asyncio.wait_for(provider.started.wait(), 3)
        loop._pending_summary = ("first", "last", "pending")
        if archive:
            await archive_guard.stop_work(sid)
        else:
            await query_session_lifecycle.stop_all_background_tasks(sid, user_stop=True)
        assert task.cancelled()
        assert loop._pending_summary is None
        # Only the side call is cancelled: a continuation turn running on the
        # same engine is ended (or not) by the caller's own interrupt.
        assert not loop._interrupted.is_set()
        loop._schedule_checkpoint(loop.session_id, "after-stop")
        assert loop._summary_task is None
        row = database.get_session(sid)
        assert row["total_cost_usd"] == 0.125
        assert row["total_tokens_in"] == 100
        assert row["total_tokens_out"] == 0
        await adapter.interrupt()
        assert database.get_session(sid)["total_cost_usd"] == 0.125
    finally:
        await loop.aclose()
