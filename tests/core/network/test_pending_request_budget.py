"""Context reload, request budget and lock regressions for pending recovery."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.db import database, network as store
from app.network import catalog, engine, session_active, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_pending_apply_budget import (  # noqa: F401
    backlog, pending_chunk_delay, pending_semantics_clock, pg_workers, private_cluster,
)
from tests.core.network.test_pending_retry_backoff import stalled_heads, warnings


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_routine_context_reload_preserves_backoff_and_fifth_ready_head(request, use_postgres, monkeypatch, caplog):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    worker, blocked, attempts, now = stalled_heads(factory, monkeypatch)
    caplog.set_level(logging.WARNING, logger=engine.__name__)
    with worker.active():
        first = engine.resume_pending()
        assert first.retry == 4 and first._pending_more
        previous = engine.require_context()
        snapshot = engine.load_context(publish=False)
        assert snapshot is not previous and engine.cached_context() is previous
        now[0] = 5
        refreshed = engine.load_context()
        assert refreshed is not previous
        assert refreshed._pending_retries is previous._pending_retries
        second = engine.resume_pending()
        assert second.applied == 1 and second.retry == 0
        assert attempts[4:] == ["good"] and len(warnings(caplog)) == 4
        assert not second._pending_more
        for when, delay in [(30, 60), (90, 120)]:
            now[0] = when
            engine.load_context()
            assert engine.resume_pending().retry == 4
            assert all(retry.delay == delay for retry in engine.require_context()._pending_retries.values())


async def test_ready_unattempted_work_does_not_take_idle_sleep(monkeypatch):
    delays = []
    results = iter([engine.ApplyResult(retry=4, _pending_more=True),
                    engine.ApplyResult(applied=1), engine.ApplyResult()])

    async def to_thread(fn):
        return next(results)

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(engine, "kick", lambda: None)
    with pytest.raises(asyncio.CancelledError):
        await transport.pending_loop()
    assert delays == [0.0, 1.0, 30.0]


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("pending_chunk_delay", [0.0, 0.3], ids=["fast", "slow"], indirect=True)
def test_inbound_continuation_is_capped_and_background_finishes(
        request, use_postgres, monkeypatch, pending_chunk_delay, pending_semantics_clock):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 450)
    wakes = []
    monkeypatch.setattr(engine, "_pending_kick", lambda: wakes.append(True))
    with b.active():
        first = engine.apply_batch(ops[:1])
        assert wakes == [True]  # wake an idle background loop immediately
        assert first.applied == 1 + engine.INBOUND_PENDING_CHUNKS * engine.APPLY_CHUNK
        assert first._pending_more and first.retry == first.rejected == 0
        total, part = first.applied, first
        for _ in range(10):
            if not part._pending_more:
                break
            part = engine.resume_pending()
            assert part.retry == part.rejected == 0
            total += part.applied
        assert not part._pending_more and total == len(ops)
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.count_pending(cur, a.device_id, 1) == 0
            assert store.get_watermark(cur, a.device_id, 1) == ops[-1]["origin_seq"]
        assert engine.apply_batch(ops).duplicate == len(ops)


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("duplicate", [False, True], ids=["new", "duplicate"])
def test_no_pending_successor_does_not_acquire_a_second_state_lock(request, use_postgres, duplicate, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 2)
    with b.active():
        if duplicate:
            engine.apply_batch(ops[:1])
        else:
            with store.get_db() as conn:
                store.clear_pending_stream(conn.cursor(), a.device_id, 1)
        locks = []
        real_lock = store.lock_state

        def lock(cursor):
            locks.append(True)
            return real_lock(cursor)

        monkeypatch.setattr(store, "lock_state", lock)
        result = engine.apply_batch(ops[:1])
        assert (result.duplicate if duplicate else result.applied) == 1
        assert len(locks) == 1 and not result._pending_more


async def test_duplicate_settle_is_not_refused_for_a_rejected_successor(workers, monkeypatch):
    a, b = workers("sender"), workers("receiver")
    with a.active():
        engine.create_network("Settle")
        database.create_session(session_id="chat", profile_id="default", title="Chat")
        catalog.publish_owned()
    join(a, b)
    converge([a, b])
    with a.active():
        row, _ = session_active._write("chat", False, local=True)
        signed = row["_settled_membership"]
    with b.active():
        assert engine.apply_batch([signed]).rejected == 0
        with store.get_db() as conn:
            # A malformed successor must not change the result of the valid
            # duplicate settle. Actual signed settle production is above.
            store.stash_pending(conn.cursor(), {**signed, "origin_seq": signed["origin_seq"] + 1,
                                                "signature": "invalid"})
        drained = []
        real_drain = engine._apply_pending_chunk

        def drain(*args):
            drained.append(True)
            return real_drain(*args)

        monkeypatch.setattr(engine, "_apply_pending_chunk", drain)
        result = await session_active.handle_dismiss({"session_id": "chat", "is_active": False,
                                                      "settled_membership": signed}, a.device_id)
        assert "dismissed" in result and not drained
        assert engine.resume_pending().rejected == 1


async def test_pending_wake_interrupts_idle_delay(monkeypatch):
    loop = asyncio.get_running_loop()
    seen, first, wake = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    def resume():
        calls.append(True)
        loop.call_soon_threadsafe(first.set)
        if len(calls) == 2:
            loop.call_soon_threadsafe(seen.set)
        return engine.ApplyResult()

    monkeypatch.setattr(transport, "_loop", loop)
    monkeypatch.setattr(transport, "_pending_wake", wake)
    monkeypatch.setattr(engine, "resume_pending", resume)
    task = asyncio.create_task(transport.pending_loop())
    try:
        # A kick may arrive before or during the wait: neither is lost.
        transport._pending_kick_from_thread()
        # The first pass clears a pre-start kick, so issue one once it probes.
        await asyncio.wait_for(first.wait(), timeout=2)
        transport._pending_kick_from_thread()
        await asyncio.wait_for(seen.wait(), timeout=2)
        assert len(calls) >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
