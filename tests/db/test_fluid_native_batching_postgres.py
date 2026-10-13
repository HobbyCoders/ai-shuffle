"""Opt-in native batching contracts on disposable PostgreSQL (#1284).

The pg_shim suite (``tests/core/test_fluid_pg_store.py``) pins the store's
batching and paging logic; these cases drive a real ``NativeLoop`` turn, with
a scripted streaming provider, against PostgreSQL itself. They cover what the
shim cannot: real transactions per batch, replay beyond the bounded cache,
sequence continuity across a fresh process, abrupt producer exit at buffered,
batch and terminal boundaries (``native_crash_producer.py``), and failures
raised by PostgreSQL's own COMMIT through a deferred constraint trigger.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.db.native_crash_producer import STREAMED_DELTAS, scripted_provider
from tests.db.postgres_harness import (  # noqa: F401  (pg_schema: autouse fixture)
    pg_schema,
    requires_real_pg,
)

pytestmark = requires_real_pg

ROOT = Path(__file__).resolve().parents[2]
PRODUCER = Path(__file__).with_name("native_crash_producer.py")
MODEL = "scripted-native"


def _loop(store, session_id, deltas):
    from fluid.engine.native import NativeLoop

    return NativeLoop(
        provider=scripted_provider(deltas),
        store=store,
        model=MODEL,
        session_id=session_id,
    )


async def _turn(store, session_id, deltas, prompt="go"):
    """Run one real native turn; return the events the bus published for it."""
    from fluid.events import UserTurn

    loop = _loop(store, session_id, deltas)
    try:
        before = len(loop.bus.log(session_id))
        await loop.submit(UserTurn(content=prompt))
        await asyncio.wait_for(loop.wait_turn(), 60)
        return loop.bus.log(session_id)[before:]
    finally:
        await loop.aclose()


async def _durable(sid):
    from app.core.provider.fluid_pg_store import PgFluidStore

    return [e async for e in PgFluidStore().iter_events(sid, 0)]


def _sql(statement, params=()):
    from app.db import base

    with base.get_db() as conn:
        conn.cursor().execute(statement, params)


def _event_transactions(monkeypatch):
    """Record every event transaction the store opens, as its event count."""
    from app.core.provider import fluid_pg_store as module

    sizes = []
    original = module._append_events_sync

    def counted(session_id, events):
        original(session_id, events)
        sizes.append(len(events))

    monkeypatch.setattr(module, "_append_events_sync", counted)
    return sizes


async def test_real_native_turn_batches_and_replays_beyond_the_bounded_cache(
    monkeypatch,
):
    from fluid.events.model import TextDelta, TurnComplete, TurnStarted

    from app.core.provider.fluid_pg_store import PgFluidStore

    sid = f"native-batch-{uuid4().hex[:8]}"
    deltas = 300  # Well past EVENT_CACHE_LIMIT (128) and several 32-delta batches.
    transactions = _event_transactions(monkeypatch)
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60  # Count boundaries, independent of host speed.
    published = await _turn(store, sid, deltas)

    assert sum(isinstance(e, TextDelta) for e in published) == deltas
    assert isinstance(published[0], TurnStarted) and isinstance(
        published[-1], TurnComplete
    )
    assert not store._pending_events  # The terminal barrier flushed the tail.
    # Every published event is durable, in order, and each delta batch holds
    # the full 32 events instead of one transaction per event.
    assert sum(transactions) == len(published)
    assert max(transactions) >= store.STREAM_BATCH_LIMIT
    assert len(transactions) <= len(published) // 8

    replay = await _durable(sid)
    assert [(e.seq, type(e)) for e in replay] == [(e.seq, type(e)) for e in published]
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert "".join(e.text for e in replay if isinstance(e, TextDelta)) == "".join(
        f"w{i} " for i in range(deltas)
    )

    resumed = PgFluidStore()
    assert await resumed.last_event_seq(sid) == replay[-1].seq
    assert sid not in resumed._events  # The sequence lookup loads no history.
    page = await resumed.events_page(sid, 0)
    assert [e.seq for e in page] == list(range(1, resumed.EVENT_CACHE_LIMIT + 1))
    full = await resumed.events_since(sid, 0)
    assert [e.seq for e in full] == [e.seq for e in replay]
    assert len(resumed._events[sid]) == resumed.EVENT_CACHE_LIMIT
    # A cursor behind the cached tail goes back to PostgreSQL, not the cache.
    assert [e.seq for e in await resumed.events_since(sid, 5)] == [
        e.seq for e in replay[5:]
    ]


async def test_resumed_native_producer_continues_the_durable_sequence():
    from fluid.events.model import TurnComplete

    from app.core.provider.fluid_pg_store import PgFluidStore

    sid = f"native-resume-{uuid4().hex[:8]}"
    first = await _turn(PgFluidStore(), sid, 40, "first")
    head = await PgFluidStore().last_event_seq(sid)
    assert head == first[-1].seq

    # A fresh store and engine, as after a worker restart.
    second = await _turn(PgFluidStore(), sid, 40, "second")
    assert second[0].seq == head + 1
    replay = await _durable(sid)
    assert [e.seq for e in replay] == list(range(1, second[-1].seq + 1))
    assert sum(isinstance(e, TurnComplete) for e in replay) == 2


@pytest.mark.parametrize(
    "boundary,exit_after,deltas_kept",
    [
        ("buffered", 5, 0),
        ("batch", 37, 32),
        ("terminal", 0, STREAMED_DELTAS),
    ],
)
async def test_abrupt_native_producer_exit_keeps_committed_prefix_and_restart_continues(
    tmp_path,
    boundary,
    exit_after,
    deltas_kept,
):
    from fluid.events.model import TextDelta, TurnComplete, TurnStarted

    from app.core.provider.fluid_pg_store import PgFluidStore

    sid = f"native-crash-{boundary}-{uuid4().hex[:8]}"
    env = dict(
        os.environ,
        DATA_DIR=str(tmp_path),
        TESTING="1",
        PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "fluid")]),
    )
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(PRODUCER), sid, boundary, str(exit_after)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert child.returncode == 17, child.stderr.decode(errors="replace")[-2000:]

    kept = await _durable(sid)
    assert [e.seq for e in kept] == list(range(1, len(kept) + 1))
    assert sum(isinstance(e, TextDelta) for e in kept) == deltas_kept
    assert isinstance(kept[0], TurnStarted)
    if boundary == "terminal":
        assert isinstance(kept[-1], TurnComplete)
    else:
        # Only reconstructible deltas were lost: every durable event before
        # the first delta survived, and nothing durable follows the kept tail.
        first = next(
            (i for i, e in enumerate(kept) if isinstance(e, TextDelta)), len(kept)
        )
        assert all(isinstance(e, TextDelta) for e in kept[first:])
        assert not any(isinstance(e, TurnComplete) for e in kept)
    head = await PgFluidStore().last_event_seq(sid)
    assert head == len(kept)

    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60
    published = await _turn(store, sid, 3, "after restart")
    assert published[0].seq == head + 1
    replay = await _durable(sid)
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert [(e.seq, type(e)) for e in replay[:head]] == [(e.seq, type(e)) for e in kept]
    assert isinstance(replay[-1], TurnComplete)


@pytest.fixture
def reject_on_commit():
    """Install a deferred trigger that fails COMMIT for one session's rows."""
    installed = []

    def install(session_id, condition):
        _sql(
            "CREATE OR REPLACE FUNCTION reject_native_event_for_test() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            f"IF NEW.session_id = '{session_id}' AND ({condition}) THEN "
            "RAISE EXCEPTION 'injected commit failure'; END IF; RETURN NULL; END $$"
        )
        _sql(
            "CREATE CONSTRAINT TRIGGER reject_native_event AFTER INSERT ON fluid_session_events "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_native_event_for_test()"
        )
        installed.append(True)

    yield install
    if installed:
        _sql("DROP TRIGGER IF EXISTS reject_native_event ON fluid_session_events")
        _sql("DROP FUNCTION IF EXISTS reject_native_event_for_test()")


async def test_native_terminal_commit_failure_leaves_no_phantom_terminal(
    reject_on_commit,
):
    from fluid.events.model import ErrorEvent, TextDelta, TurnComplete

    from app.core.provider.fluid_pg_store import PgFluidStore

    sid = f"native-commit-terminal-{uuid4().hex[:8]}"
    reject_on_commit(sid, "NEW.event->>'_cls' = 'TurnComplete'")
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60
    published = await _turn(store, sid, 40)

    # Live subscribers saw the terminal and then the failure that lost it.
    terminal = next(e for e in published if isinstance(e, TurnComplete))
    assert isinstance(published[-1], ErrorEvent)
    assert "injected commit failure" in published[-1].message
    replay = await _durable(sid)
    assert not any(isinstance(e, TurnComplete) for e in replay)
    assert [e.seq for e in replay] == [
        e.seq for e in published if e.seq != terminal.seq
    ]
    # The stored assistant node flushed every delta before the terminal ran.
    assert sum(isinstance(e, TextDelta) for e in replay) == 40
    assert sid not in store._events  # No cached copy of the rejected transaction.
    assert not any(
        isinstance(e, TurnComplete) for e in await store.events_since(sid, 0)
    )


async def test_native_batched_tail_commit_failure_keeps_accepted_deltas_for_retry(
    reject_on_commit,
):
    from fluid.events.model import TextDelta, TurnComplete

    from app.core.provider.fluid_pg_store import PgFluidStore

    sid = f"native-commit-tail-{uuid4().hex[:8]}"
    # w35 is in the 8-delta tail after the first full batch; the barrier that
    # carries that tail is the transaction whose COMMIT fails.
    reject_on_commit(sid, "NEW.event->>'text' = 'w35 '")
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60
    with pytest.raises(Exception, match="injected commit failure"):
        await _turn(store, sid, 40)

    committed = await _durable(sid)
    deltas = [e for e in committed if isinstance(e, TextDelta)]
    assert [e.text for e in deltas] == [f"w{i} " for i in range(32)]
    assert [e.seq for e in committed] == list(range(1, len(committed) + 1))
    assert not any(isinstance(e, TurnComplete) for e in committed)
    assert sid not in store._events
    assert len(store._pending_events[sid]) == 8  # Accepted deltas await a retry.

    _sql("DROP TRIGGER reject_native_event ON fluid_session_events")
    await store.flush_events(sid)
    recovered = await _durable(sid)
    assert [e.text for e in recovered if isinstance(e, TextDelta)] == [
        f"w{i} " for i in range(40)
    ]
    assert [e.seq for e in recovered] == list(range(1, len(recovered) + 1))
