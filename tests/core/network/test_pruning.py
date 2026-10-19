"""Retention cannot stall heartbeats, and preserves snapshot heads in batches."""
import asyncio
import re
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock

import psycopg2.extensions
import pytest

from app.db import database, network as store, retention
from app.network import engine, peer_snapshot, transport
from tests.core.network.harness import Worker


def test_prune_batches_preserve_latest_and_recent_ops(tmp_path, monkeypatch):
    worker = Worker("pruning", tmp_path)
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    transactions = []
    get_db = retention.get_db

    @contextmanager
    def tracked_db():
        transactions.append("acquire")
        with get_db() as conn:
            yield conn
        transactions.append("release")

    try:
        with worker.active():
            engine.create_network("Pruning")
            # Drive the real producer, not just rows shaped like its output.
            for version in range(6):
                database.set_system_setting("tts_model", str(version))
                engine.scan_collection("system_settings")
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE sync_ops SET created_at = %s", ("2000-01-01T00:00:00+00:00",))
                cur.execute("SELECT ingest_id, collection, record_id FROM sync_ops ORDER BY ingest_id")
                rows = cur.fetchall()
                latest = {(r["collection"], r["record_id"]): r["ingest_id"] for r in rows}
                # A recent, superseded op must survive too.
                recent = rows[-2]["ingest_id"]
                cur.execute("UPDATE sync_ops SET created_at = %s WHERE ingest_id = %s",
                            ("2999-01-01T00:00:00+00:00", recent))
            keep = set(latest.values()) | {recent}
            monkeypatch.setattr(retention, "get_db", tracked_db)
            expected = len(rows) - len(keep)
            assert expected >= 4
            queries = []
            worker.raw.set_trace_callback(queries.append)
            try:
                assert store.prune_ops() == expected
            finally:
                worker.raw.set_trace_callback(None)
            boundaries = [int(m.group(1)) for q in queries
                          if (m := re.search(r"o.ingest_id > (\d+)", q))]
            assert len(boundaries) >= 3
            assert boundaries[0] == 0
            assert all(a < b for a, b in zip(boundaries, boundaries[1:]))
            assert transactions == ["acquire", "release"] * (expected // 2 + 1)
            remaining = worker.sql("SELECT ingest_id FROM sync_ops")
            assert {r["ingest_id"] for r in remaining} == keep
            assert store.prune_ops() == 0
    finally:
        worker.close()
        engine.reset_for_tests()


def test_postgres_batches_set_local_timeout_and_release(monkeypatch):
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    cursors = []
    transactions = []

    @contextmanager
    def get_db():
        cur = MagicMock(spec=psycopg2.extensions.cursor)
        cur.rowcount = 2 if len(cursors) < 2 else 1
        cursors.append(cur)
        transactions.append("acquire")
        yield MagicMock(cursor=lambda: cur)
        transactions.append("commit-release")

    monkeypatch.setattr(retention, "get_db", get_db)
    assert retention.delete_batches("DELETE bounded", ()) == 5
    assert transactions == ["acquire", "commit-release"] * 3
    for cur in cursors:
        assert cur.execute.call_args_list[0].args == ("SET LOCAL statement_timeout = '30s'",)
        assert cur.execute.call_args_list[1].args == ("DELETE bounded", ())


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_slow_or_raising_prune_does_not_block_presence(monkeypatch, raises):
    entered = threading.Event()
    release = threading.Event()
    published = asyncio.Event()
    loop = asyncio.get_running_loop()
    count = 0
    cleanup = MagicMock()

    def prune(*, stop):
        entered.set()
        assert release.wait(5), "test did not release prune"
        if raises:
            raise RuntimeError("prune failed")
        return 1

    def publish(**kwargs):
        nonlocal count
        if entered.is_set():
            count += 1
            if count >= 2:
                loop.call_soon_threadsafe(published.set)

    monkeypatch.setattr(transport, "PRUNE_STARTUP_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(transport, "PRESENCE_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(transport, "_capabilities", lambda: {})
    monkeypatch.setattr(store, "prune_ops", prune)
    monkeypatch.setattr(engine, "publish_presence", publish)
    monkeypatch.setattr(store, "expire_conflicts", cleanup)
    monkeypatch.setattr(store, "expire_joins", cleanup)
    monkeypatch.setattr(peer_snapshot, "cleanup", cleanup)
    pruning = asyncio.create_task(transport.prune_loop())
    presence = asyncio.create_task(transport.presence_loop())
    try:
        await asyncio.wait_for(published.wait(), 3)
        assert not release.is_set()  # repeated heartbeats while prune is blocked
        assert cleanup.call_count >= 3
        release.set()
        published.clear()
        await asyncio.wait_for(published.wait(), 3)
        assert not pruning.done()  # failure is contained, next daily run scheduled
    finally:
        release.set()
        pruning.cancel()
        presence.cancel()
        await asyncio.gather(pruning, presence, return_exceptions=True)


@pytest.mark.asyncio
async def test_prune_delays_first_run_and_waits_between_failures(monkeypatch):
    delays = []
    prune = MagicMock(side_effect=RuntimeError("failed"))

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 1:
            prune.assert_not_called()
        else:
            prune.assert_called_once()
            raise asyncio.CancelledError

    monkeypatch.setattr(transport.asyncio, "sleep", sleep)
    monkeypatch.setattr(store, "prune_ops", prune)
    with pytest.raises(asyncio.CancelledError):
        await transport.prune_loop()
    assert delays == [300, 86400]
    assert prune.call_args.kwargs["stop"].is_set()


def test_stop_between_batches_commits_current_batch_and_does_not_reacquire(monkeypatch):
    stop = threading.Event()
    cur = MagicMock()
    cur.rowcount = retention.BATCH_SIZE
    acquisitions = []

    @contextmanager
    def get_db():
        acquisitions.append("acquire")
        yield MagicMock(cursor=lambda: cur)
        stop.set()
        acquisitions.append("commit-release")

    monkeypatch.setattr(retention, "get_db", get_db)
    assert retention.delete_batches("DELETE bounded", (), stop=stop) == retention.BATCH_SIZE
    assert acquisitions == ["acquire", "commit-release"]
    assert retention.delete_batches("DELETE bounded", (), stop=stop) == 0
    assert acquisitions == ["acquire", "commit-release"]


@pytest.mark.asyncio
async def test_cancelling_prune_signals_running_thread(monkeypatch):
    entered = asyncio.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def prune(*, stop):
        loop.call_soon_threadsafe(entered.set)
        assert stop.wait(3), "cancellation did not signal the pruning thread"
        loop.call_soon_threadsafe(finished.set)
        return 0

    monkeypatch.setattr(transport, "PRUNE_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(store, "prune_ops", prune)
    task = asyncio.create_task(transport.prune_loop())
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(finished.wait(), 2)
