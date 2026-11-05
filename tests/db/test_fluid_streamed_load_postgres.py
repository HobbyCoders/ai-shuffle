"""Real server-cursor timeouts, rollback, pool reuse, and atomic cold hydration."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from threading import Event
import time
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401 (autouse)

pytestmark = requires_real_pg


async def _transcript():
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    writer = PgFluidStore()
    session_id = f"ses_{uuid4().hex}"
    await writer.create_session(session_id)
    parent = None
    # Cross the 32-row FETCH boundary; the writer is the real producing path.
    for i in range(33):
        key = f"node-{i:02d}"
        await writer.append(session_id, SessionNode(
            node_id=key, parent_id=parent,
            message=Message(role="user", content=[Text(text=key)])))
        parent = key
    return PgFluidStore(), session_id


@pytest.fixture
def short_idle_budget(monkeypatch):
    from app.core.provider import fluid_pg_store as module
    from app.db import base

    completed = []
    real_db = base.get_db
    budgets = ("idle_in_transaction_session_timeout", "statement_timeout", "lock_timeout")

    def settings(conn):
        values = []
        cur = conn.cursor()
        for key in budgets:
            cur.execute(f"SHOW {key}")
            values.append(cur.fetchone()[key])
        return tuple(values)

    @contextmanager
    def opened():
        previous = None
        try:
            with real_db() as conn:
                previous = settings(conn)
                pid = conn.get_backend_pid()
                original = conn
                cur = conn.cursor()
                cur.execute("SET LOCAL idle_in_transaction_session_timeout = '60ms'")
                cur.execute("SET LOCAL statement_timeout = '100ms'")
                yield conn
        finally:
            # The real get_db has committed/rolled back and returned its slot.
            if previous is not None:
                with real_db() as conn:
                    if not original.closed:
                        assert conn.get_backend_pid() == pid
                    assert settings(conn) == previous
                    completed.append(pid)

    monkeypatch.setattr(module, "get_db", opened)
    return completed


def _empty_cache(store, session_id):
    assert session_id not in store._loaded
    assert session_id not in store._sessions
    assert not store._nodes
    assert not store._node_sizes


@pytest.mark.asyncio
async def test_slow_decode_survives_idle_budget_and_keeps_pool_available(monkeypatch, short_idle_budget):
    from app.core.provider import fluid_pg_store as module
    from app.db import base

    cold, session_id = await _transcript()
    original = module.Message.from_dict
    entered = Event()
    batch_clock = {}

    def slow_decode(raw):
        now = time.monotonic()
        text = raw["content"][0]["text"]
        if text == "node-00":
            batch_clock["start"] = now
        elif text == "node-32":
            # Includes the next FETCH: an upper bound on the first batch's work.
            batch_clock["elapsed"] = now - batch_clock["start"]
        entered.set()
        time.sleep(0.08)  # Longer than idle budget; not an SQL execution.
        return original(raw)

    monkeypatch.setattr(module.Message, "from_dict", slow_decode)
    guard = module.transcript_decode_timeouts
    # Control: the pre-fix path really loses its transaction on this server.
    monkeypatch.setattr(module, "transcript_decode_timeouts", lambda cur: None)
    with pytest.raises((psycopg2.OperationalError, psycopg2.InterfaceError)):
        await cold.get_session(session_id)
    _empty_cache(cold, session_id)
    monkeypatch.setattr(module, "transcript_decode_timeouts", guard)
    entered.clear()
    loading = asyncio.create_task(cold.get_session(session_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)

        def unrelated_query():
            with base.get_db() as conn:
                assert len(base._pg_pool._used) == 2  # One retained by the decode.
                cur = conn.cursor()
                cur.execute("SELECT 1 AS ok")
                assert cur.fetchone()["ok"] == 1

        started = time.monotonic()
        await asyncio.wait_for(asyncio.to_thread(unrelated_query), timeout=5)
        print(f"concurrent query during slow decode: {(time.monotonic() - started) * 1000:.1f} ms")
    finally:
        assert await loading
    print(f"slow 32-row batch plus next FETCH: {batch_clock['elapsed']:.3f} s")
    assert len(cold._nodes[session_id]) == 33
    assert cold._sessions[session_id].head_node_id == "node-32"
    assert [n.message.text() for n in await cold.load_path(session_id)] == [f"node-{i:02d}" for i in range(33)]
    assert short_idle_budget  # Success committed and restored all three limits.
    assert not base._pg_pool._used


@pytest.mark.asyncio
async def test_slow_decode_failure_rolls_back_without_publishing_and_retries(monkeypatch, short_idle_budget):
    from app.core.provider import fluid_pg_store as module
    from app.db import base

    cold, session_id = await _transcript()
    original = module.Message.from_dict

    def broken(raw):
        time.sleep(0.08)
        if raw["content"][0]["text"] == "node-32":
            raise ValueError("decode failed after the next FETCH")
        return original(raw)

    monkeypatch.setattr(module.Message, "from_dict", broken)
    with pytest.raises(ValueError, match="decode failed"):
        await cold.get_session(session_id)
    _empty_cache(cold, session_id)
    assert not base._pg_pool._used
    # This is the same store, session, and PostgreSQL pool after rollback.
    monkeypatch.setattr(module.Message, "from_dict", original)
    assert await cold.get_session(session_id)
    assert len(cold._nodes[session_id]) == 33
    assert short_idle_budget
    assert not base._pg_pool._used


@pytest.mark.asyncio
async def test_fetch_still_has_statement_deadline_and_can_retry(monkeypatch, short_idle_budget):
    from app.core.provider import fluid_pg_store as module
    from app.db import base

    cold, session_id = await _transcript()
    real_cursor = base._PgDictConnection.cursor

    class SlowFetch(RealDictCursor):
        def execute(self, query, params=None):
            # Sleep in the actual FETCH, not during DECLARE or Python decode.
            return super().execute(query.replace("SELECT node_id", "SELECT pg_sleep(0.02), node_id"), params)

    def cursor(conn, *args, **kwargs):
        if kwargs.get("name"):
            kwargs["cursor_factory"] = SlowFetch
        return real_cursor(conn, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", cursor)
    with pytest.raises(psycopg2.errors.QueryCanceled, match="statement timeout"):
        await cold.get_session(session_id)
    _empty_cache(cold, session_id)
    assert not base._pg_pool._used
    monkeypatch.setattr(base._PgDictConnection, "cursor", real_cursor)
    assert await cold.get_session(session_id)
    assert len(cold._nodes[session_id]) == 33
    assert short_idle_budget


@pytest.mark.asyncio
async def test_stalled_decode_expires_without_publishing_and_retries(monkeypatch, short_idle_budget):
    from app.core.provider import fluid_pg_store as module
    from app.db import base

    cold, session_id = await _transcript()
    guard = module.transcript_decode_timeouts
    original = module.Message.from_dict

    def short_guard(cur):
        guard(cur)
        cur.execute("SHOW idle_in_transaction_session_timeout")
        assert cur.fetchone()["idle_in_transaction_session_timeout"] == "5min"
        # Exercise the finite production setting at a subsecond test timescale.
        cur.execute("SET LOCAL idle_in_transaction_session_timeout = '120ms'")

    def stalled(raw):
        if raw["content"][0]["text"] == "node-16":
            time.sleep(0.3)
        return original(raw)

    monkeypatch.setattr(module, "transcript_decode_timeouts", short_guard)
    monkeypatch.setattr(module.Message, "from_dict", stalled)
    with pytest.raises((psycopg2.OperationalError, psycopg2.InterfaceError)):
        await cold.get_session(session_id)
    _empty_cache(cold, session_id)
    assert not base._pg_pool._used
    monkeypatch.setattr(module.Message, "from_dict", original)
    monkeypatch.setattr(module, "transcript_decode_timeouts", guard)
    assert await cold.get_session(session_id)
    assert len(cold._nodes[session_id]) == 33
    assert short_idle_budget
    assert not base._pg_pool._used
