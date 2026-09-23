"""Delegated streaming persistence through PgFluidStore's delta batch (#1284).

Every test drives the real ``DelegatedLoop`` against the repository's fake
stream-json CLI (``fluid/tests/fixtures/fake_claude.py``, scenario ``s``) in a
subprocess, on the pg_shim. Cross-process crash and deferred-COMMIT cases need
real PostgreSQL: ``tests/db/test_fluid_delegated_batching_postgres.py``.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import aclosing
from pathlib import Path

import pytest

from app.core.provider import fluid_pg_store as module
from app.core.provider.fluid_pg_store import PgFluidStore
from app.db.async_database import run_sync

from fluid.engine.delegated import DelegatedLoop
from fluid.events import Interrupt, UserTurn
from fluid.events.model import (
    ErrorEvent,
    Interrupted,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)
from fluid.store.base import RECONSTRUCTIBLE_EVENTS

FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"


def _loop(store, *, deltas: str = "70,8", mode: str = "", session_id=None) -> DelegatedLoop:
    return DelegatedLoop(
        store=store,
        model="claude-fake",
        claude_cmd=[sys.executable, str(FAKE)],
        session_id=session_id,
        partial=True,
        env={
            "FAKE_CLAUDE_SCENARIO": "s",
            "FAKE_CLAUDE_DELTAS": deltas,
            "FAKE_CLAUDE_STREAM_MODE": mode,
        },
    )


def _store() -> PgFluidStore:
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60  # Count/barrier boundaries, not wall-clock speed.
    return store


def _record_transactions(monkeypatch) -> list:
    """Each ``_append_events_sync`` call is one event transaction."""
    calls: list = []
    original = module._append_events_sync

    def record(sid, events):
        original(sid, events)
        calls.append(list(events))

    monkeypatch.setattr(module, "_append_events_sync", record)
    return calls


async def _run(loop: DelegatedLoop, content: str = "go") -> None:
    await loop.submit(UserTurn(content=content))
    await asyncio.wait_for(loop.wait_turn(), 30)


async def _durable(sid: str) -> list:
    return [e async for e in PgFluidStore().iter_events(sid, 0)]


def _assert_barrier_shape(transactions: list) -> None:
    """A durable event only ever closes a transaction; deltas never follow it."""
    for events in transactions:
        assert all(isinstance(e, RECONSTRUCTIBLE_EVENTS) for e in events[:-1])
        assert [e.seq for e in events] == list(range(events[0].seq, events[-1].seq + 1))
        if isinstance(events[-1], RECONSTRUCTIBLE_EVENTS):
            assert len(events) == PgFluidStore.STREAM_BATCH_LIMIT


@pytest.mark.asyncio
async def test_delegated_stream_batches_deltas_between_durable_barriers(mock_db, monkeypatch):
    store = _store()
    transactions = _record_transactions(monkeypatch)
    loop = _loop(store)
    try:
        await _run(loop)
        sid = loop.session_id
        published = loop.bus.log(sid)
    finally:
        await loop.aclose()

    replay = await _durable(sid)
    assert [(e.seq, type(e)) for e in replay] == [(e.seq, type(e)) for e in published]
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert isinstance(replay[-1], TurnComplete)
    assert any(isinstance(e, ToolUse) for e in replay) and any(isinstance(e, ToolResult) for e in replay)
    deltas = [e for e in replay if isinstance(e, TextDelta)]
    assert len(deltas) == 78
    first_text = "".join(f"a{i} " for i in range(70))
    assert "".join(e.text for e in deltas[:70]) == first_text
    # The reconstructible claim: the stored assistant node carries the same text.
    path = await PgFluidStore().load_path(sid)
    assert any(n.message.role == "assistant" and n.message.text() == first_text for n in path)

    _assert_barrier_shape(transactions)
    assert sum(len(t) for t in transactions) == len(replay)
    assert len(transactions) < len(replay) - 60
    full_batches = [t for t in transactions if isinstance(t[-1], TextDelta)]
    assert len(full_batches) == 2  # 70 deltas: 32 + 32, then 6 ride the BlockStop barrier.
    assert not store._pending_events


@pytest.mark.asyncio
async def test_delegated_interrupt_commits_buffered_tail_with_terminal(mock_db, monkeypatch):
    store = _store()
    transactions = _record_transactions(monkeypatch)
    sid = "delegated-interrupt"
    loop = _loop(store, deltas="5,0", mode="interrupt", session_id=sid)
    try:
        await loop.submit(UserTurn(content="start then stop"))
        seen = 0
        async with aclosing(loop.bus.subscribe(sid)) as events:
            async for event in events:
                seen += isinstance(event, TextDelta)
                if seen == 5:
                    break
        head = await run_sync(module._last_event_seq_sync, sid)
        assert len(store._pending_events[sid]) == 5  # Accepted, not committed yet.
        assert head == loop.bus.last_seq(sid) - 5
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 30)
    finally:
        await loop.aclose()

    replay = await _durable(sid)
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert isinstance(replay[-1], Interrupted)
    assert sum(isinstance(e, TextDelta) for e in replay) == 5
    tail = next(t for t in transactions if any(isinstance(e, TextDelta) for e in t))
    assert [type(e) for e in tail][:5] == [TextDelta] * 5  # The BlockStop barrier took them.
    _assert_barrier_shape(transactions)
    assert not store._pending_events


@pytest.mark.asyncio
async def test_delegated_cli_failure_commits_tail_with_error_event(mock_db, monkeypatch):
    store = _store()
    transactions = _record_transactions(monkeypatch)
    loop = _loop(store, deltas="5", mode="die")
    try:
        await _run(loop)
        sid = loop.session_id
    finally:
        await loop.aclose()
    replay = await _durable(sid)
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert isinstance(replay[-1], ErrorEvent)
    assert "closed before result" in replay[-1].message
    assert [type(e) for e in replay[-6:]] == [TextDelta] * 5 + [ErrorEvent]
    # Committed no later than the terminal (the park check's replay flushes
    # them just ahead of it), and never out of order.
    assert [type(e) for t in transactions[-2:] for e in t] == [TextDelta] * 5 + [ErrorEvent]
    assert not store._pending_events


@pytest.mark.asyncio
async def test_delegated_cancelled_turn_flushes_tail_without_terminal(mock_db):
    store = _store()
    sid = "delegated-cancel"
    loop = _loop(store, deltas="5,0", mode="interrupt", session_id=sid)
    closed = False
    try:
        await loop.submit(UserTurn(content="park mid-block"))
        seen = 0
        async with aclosing(loop.bus.subscribe(sid)) as events:
            async for event in events:
                seen += isinstance(event, TextDelta)
                if seen == 5:
                    break
        published = [e.seq for e in loop.bus.log(sid)]
        assert len(store._pending_events[sid]) == 5
        await loop.aclose()  # Cancels the turn: no durable terminal event follows.
        closed = True
    finally:
        if not closed:
            await loop.aclose()
    assert [e.seq for e in await _durable(sid)] == published
    assert not store._pending_events


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_delegated_transient_commit_failure_surfaces_and_retries_batch(mock_db, monkeypatch, committed):
    store = _store()
    original = module._append_events_sync
    failures = []

    def fail_first_full_batch(sid, events):
        if not failures and len(events) == store.STREAM_BATCH_LIMIT:
            failures.append([e.seq for e in events])
            if committed:
                original(sid, events)  # The commit landed; only its acknowledgement was lost.
            raise RuntimeError("commit rejected")
        original(sid, events)

    monkeypatch.setattr(module, "_append_events_sync", fail_first_full_batch)
    loop = _loop(store)
    try:
        await _run(loop)
        sid = loop.session_id
        published = loop.bus.log(sid)
    finally:
        await loop.aclose()

    assert failures
    assert isinstance(published[-1], ErrorEvent)  # Surfaced: the turn ended on it.
    assert "commit rejected" in published[-1].message
    replay = await _durable(sid)
    # The rejected batch stayed queued and rode the terminal write: no loss, no gap.
    assert [e.seq for e in replay] == [e.seq for e in published]
    assert isinstance(replay[-1], ErrorEvent)
    assert not store._pending_events


@pytest.mark.asyncio
async def test_delegated_persistent_commit_failure_leaves_no_phantom_events(mock_db, monkeypatch):
    store = _store()
    original = module._append_events_sync
    broken = []

    def fail_from_first_batch(sid, events):
        if broken or len(events) == store.STREAM_BATCH_LIMIT:
            broken.append(len(events))
            raise RuntimeError("database unavailable")
        original(sid, events)

    monkeypatch.setattr(module, "_append_events_sync", fail_from_first_batch)
    loop = _loop(store)
    try:
        await loop.submit(UserTurn(content="go"))
        with pytest.raises(RuntimeError, match="database unavailable"):
            await asyncio.wait_for(loop.wait_turn(), 30)
        sid = loop.session_id
        published = loop.bus.log(sid)
    finally:
        await loop.aclose()

    assert isinstance(published[-1], ErrorEvent)  # Live clients still saw the failure.
    first_delta = next(e.seq for e in published if isinstance(e, TextDelta))
    committed = [e.seq for e in await _durable(sid)]
    assert committed == list(range(1, first_delta))
    # Same store, same answer: no cached tail that the database never took.
    with pytest.raises(RuntimeError, match="database unavailable"):
        await store.events_since(sid, 0)
    assert sid not in store._events
    monkeypatch.setattr(module, "_append_events_sync", original)
    await store.flush_events(sid)
    recovered = [e.seq for e in await _durable(sid)]
    # The accepted deltas were retained in order; the unacknowledged terminal
    # was the caller's to retry, so the log ends at a clean prefix.
    assert recovered == list(range(1, recovered[-1] + 1))
    assert recovered[-1] >= first_delta + store.STREAM_BATCH_LIMIT - 1
    assert all(isinstance(e, TextDelta) for e in (await _durable(sid))[first_delta - 1:])


@pytest.mark.asyncio
async def test_bus_replay_behind_the_ring_reads_bounded_pages(mock_db, monkeypatch):
    from fluid.engine.native import NativeLoop
    from fluid.events.bus import EventBus

    store = _store()
    await store.create_session("paged-bus")
    loop = NativeLoop(
        provider=object(), store=store, model="test", session_id="paged-bus",
        bus=EventBus(log_ring_size=16),
    )
    pages = []
    real_page = store.events_page

    async def page(sid, seq, limit=store.EVENT_CACHE_LIMIT):
        events = await real_page(sid, seq, limit)
        pages.append(len(events))
        return events

    async def unbounded(*args):
        raise AssertionError("bus replay materialized the history after its cursor")

    monkeypatch.setattr(store, "events_page", page)
    monkeypatch.setattr(store, "events_since", unbounded)
    loop.bus.set_replay_source(page)
    try:
        for i in range(300):
            await loop._emit(TextDelta(session_id="paged-bus", index=0, text=str(i)))
        await loop._emit(TurnComplete(session_id="paged-bus", stop_reason="end_turn", num_model_calls=1))
        seqs = []
        async with aclosing(loop.bus.subscribe("paged-bus", since_seq=0)) as events:
            async for event in events:
                seqs.append(event.seq)
                if event.seq == 301:
                    break
    finally:
        await loop.aclose()
    assert seqs == list(range(1, 302))
    assert pages and max(pages) <= store.EVENT_CACHE_LIMIT
    assert len(pages) >= 3


def test_native_attaches_the_paged_replay_source(mock_db):
    from fluid.engine.native import NativeLoop

    store = PgFluidStore()
    loop = NativeLoop(provider=object(), store=store, model="test")
    assert loop.bus.replay_source == store.events_page
