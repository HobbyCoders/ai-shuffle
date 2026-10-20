"""Local buffered catch-up starts without needing a peer exchange."""
import asyncio
from types import SimpleNamespace

import pytest

from app.network import engine, transport




def test_idle_pending_probe_is_one_read_and_no_state_lock(workers, monkeypatch):
    worker = workers("idle-pending")
    with worker.active():
        engine.create_network("Idle")
        statements = []
        worker.raw.set_trace_callback(statements.append)

        def no_lock(_cursor):
            raise AssertionError("idle readiness probe took the state lock")

        monkeypatch.setattr("app.db.network.lock_state", no_lock)
        try:
            assert engine.resume_pending().applied == 0
        finally:
            worker.raw.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1 and "sync_pending" in reads[0]
    assert not any(sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in statements)


def test_unenrolled_pending_pass_does_not_open_a_database(workers, monkeypatch):
    worker = workers("unenrolled-pending")
    with worker.active():
        def no_db():
            raise AssertionError("unenrolled idle pass opened the database")

        monkeypatch.setattr(engine, "get_db", no_db)
        assert engine.resume_pending().applied == 0


async def test_pending_loop_runs_at_startup_and_backs_off_when_idle(monkeypatch):
    calls, delays, kicks = [], [], []
    outcomes = iter([engine.ApplyResult(applied=64), engine.ApplyResult(), RuntimeError("db offline")])

    async def to_thread(fn):
        calls.append(fn)
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(engine, "kick", lambda: kicks.append(True))
    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await transport.pending_loop()
    assert calls == [engine.resume_pending] * 3
    assert delays == [1.0, 30.0, 30.0]
    assert kicks == [True]
