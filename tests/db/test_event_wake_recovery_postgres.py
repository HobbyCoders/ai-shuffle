"""A real second process receives a retried final wake on disposable PostgreSQL."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from app.db import base, database, session_events
from app.db.schema_postgres import create_postgres_schema
from app.network import catalog
from tests.db.schema_lifecycle_helpers import pg16_bindir
from tests.db.test_event_write_lock_timeout_postgres import (  # noqa: F401
    connect, session_id, stalled_notifier,
)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("needs PostgreSQL 16 server binaries")
    root = tmp_path_factory.mktemp("wake-retry-pg")
    data = root / "data"
    subprocess.run([str(Path(bindir) / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust",
                    "--no-sync", "--encoding=UTF8", "--no-locale", "-c", "dynamic_shared_memory_type=mmap"],
                   check=True, capture_output=True, timeout=30)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = (f"-p {port} -h 127.0.0.1 -k '' -c fsync=off -c synchronous_commit=local "
               "-c synchronous_standby_names=ghost_standby")
    subprocess.run([str(Path(bindir) / "pg_ctl"), "-D", str(data), "-w", "-l", str(root / "log"),
                    "-o", options, "start"], check=True, capture_output=True, timeout=30)
    try:
        yield dict(host="127.0.0.1", port=port, user="postgres", dbname="postgres", connect_timeout=3)
    finally:
        subprocess.run([str(Path(bindir) / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "-t", "20", "stop"],
                       check=True, capture_output=True, timeout=25)


@pytest.fixture(autouse=True)
def route_private_database(cluster, connect, monkeypatch, _auto_db_shim):
    # Only explicitly constructed connections to OUR cluster; never enable the
    # configured application pool or the live worker's descriptor.
    monkeypatch.setattr(base, "get_connection", lambda: base._PgDictConnection(connect()))
    monkeypatch.setattr(database, "get_connection", lambda: base._PgDictConnection(connect()))
    monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())
    with base.get_db() as conn:
        create_postgres_schema(conn.cursor())

_READER = textwrap.dedent("""
    import asyncio, json, sys
    from app.core.config import settings
    from app.core.event_gateway import EventGateway, LOG_RECHECK
    request = json.load(sys.stdin)
    type(settings).get_database_connection = lambda self: request["kwargs"]
    async def main():
        gateway = EventGateway()
        queue = gateway.subscribe(request["sid"])
        try:
            async with asyncio.timeout(8):
                while not gateway.is_connected():
                    await asyncio.sleep(.01)
                # Discard the reconnect recheck; this test needs the later wake.
                while not queue.empty():
                    queue.get_nowait()
                print("ready", flush=True)
                value = await queue.get()
                print(json.dumps({"seq": value if value is not LOG_RECHECK else None,
                                  "recheck": value is LOG_RECHECK}), flush=True)
        finally:
            gateway.stop()
    asyncio.run(main())
""")


def test_final_wake_lost_to_notify_lock_reaches_a_second_process_after_retry(cluster, connect, session_id):
    env = {key: value for key, value in os.environ.items() if key not in ("AI_SHUFFLE_ENV_FILE", "DATABASE_URL")}
    child = subprocess.Popen([sys.executable, "-c", _READER], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    child.stdin.write(json.dumps({"kwargs": cluster, "sid": session_id}))
    child.stdin.close()
    child.stdin = None
    ready = threading.Event()
    lines = []

    def read():
        for line in child.stdout:
            lines.append(line)
            if line.strip() == "ready":
                ready.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert ready.wait(5), "second-process gateway never became ready"
        with stalled_notifier(connect):
            started = time.monotonic()
            session_events.send_wake(session_id, 8, recheck_log=True)
            elapsed = time.monotonic() - started
            assert .2 < elapsed < 1.5  # Its own short lock wait, never the holder's flush.
        # No later session write occurs. Only the background retry can wake it.
        child.wait(timeout=5)
        reader.join(2)
        assert child.returncode == 0, child.stderr.read()
        assert json.loads(lines[-1]) == {"seq": None, "recheck": True}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        reader.join(2)
        # No retry may outlive this module's private pool.
        deadline = time.monotonic() + 3
        while session_events._wake_retry_running and time.monotonic() < deadline:
            time.sleep(.01)
        assert not session_events._wake_retry_running


@pytest.mark.parametrize("rollback", [False, True])
def test_mirror_notify_is_outside_the_data_transaction(connect, session_id, monkeypatch, rollback):
    calls = []
    monkeypatch.setattr(session_events, "send_wake", lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(RuntimeError) if rollback else nullcontext():
        with base.get_db() as conn:
            cur = conn.cursor()
            # Producer updates its real counter but does NOT take the global notify lock.
            catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 8})
            assert calls == []
            cur.execute("SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() "
                        "AND locktype='object' AND database=0 AND objid=0 "
                        "AND classid='pg_database'::regclass AND granted")
            assert cur.fetchone()["n"] == 0
            if rollback:
                raise RuntimeError("rollback")
    assert calls == ([] if rollback else [((session_id, 8), {"recheck_log": True})])


def test_retry_thread_exhaustion_remains_best_effort(monkeypatch):
    monkeypatch.setattr(session_events, "_wake_retries", {})
    monkeypatch.setattr(session_events, "_wake_retry_running", False)

    def cannot_start(_self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    session_events._retry_wake("chat", 8, True)
    assert not session_events._wake_retry_running
    assert session_events._wake_retries == {"chat": (8, True)}


def test_retry_batch_is_bounded_and_coalesces_recheck_hints(monkeypatch):
    # Controlled queue check; the preceding test exercises the actual resend.
    with session_events._wake_retry_lock:
        assert not session_events._wake_retry_running
    monkeypatch.setattr(session_events, "_wake_retries", {})
    monkeypatch.setattr(session_events, "_wake_retry_running", True)
    session_events._retry_wake("chat", 8, True)
    session_events._retry_wake("chat", 9, False)
    assert session_events._wake_retries["chat"] == (9, True)
    for n in range(1100):
        session_events._retry_wake(f"chat-{n}", n, False)
    assert len(session_events._wake_retries) == 1024


@pytest.mark.parametrize("boundary", ["apply_op", "apply_envelope", "pending_append", "pending_envelope"])
def test_savepoint_rollback_drops_only_the_abandoned_mirror_wake(session_id, monkeypatch, boundary):
    from app.network import engine

    calls = []
    monkeypatch.setattr(session_events, "send_wake", lambda *a, **k: calls.append(a))
    with base.get_db() as conn:
        cur = conn.cursor()
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 7})
        engine._savepoint(cur, f"SAVEPOINT {boundary}")
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 8})
        engine._savepoint(cur, f"ROLLBACK TO SAVEPOINT {boundary}")
        engine._savepoint(cur, f"RELEASE SAVEPOINT {boundary}")
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 9})
    assert calls == [(session_id, 7), (session_id, 9)]


def test_rejected_mirror_envelope_does_not_wake_when_the_next_one_commits(session_id, monkeypatch):
    from app.network import engine

    calls = []
    monkeypatch.setattr(session_events, "send_wake", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(engine.store, "lock_state", lambda cur: None)
    monkeypatch.setattr(engine, "_wire_stream", lambda *a: None)
    monkeypatch.setattr(engine, "_apply_event_append", lambda *a, **k: False)
    monkeypatch.setattr(engine, "_lock_deleted_session_handoffs", lambda *a: None)
    monkeypatch.setattr(engine, "_lock_mirror_event_counters", lambda *a: None)

    def apply(cur, data, **kwargs):
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": data["seq"]})
        cur.execute("UPDATE sessions SET title=%s WHERE id=%s", (str(data["seq"]), session_id))
        if data["seq"] == 8:
            raise engine.ApplyError("rejected after materialization")
        return "applied"

    monkeypatch.setattr(engine, "apply_envelope", apply)
    assert engine._apply_chunk([{"seq": 8}, {"seq": 9}], None, engine.ApplyResult())
    assert calls == [(session_id, 9)]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT title FROM sessions WHERE id=%s", (session_id,))
        assert cur.fetchone()["title"] == "9"


@pytest.mark.parametrize("context", [base.get_db, base.transaction])
@pytest.mark.parametrize("inner_name", ["sp_inner", "sp_outer"])
def test_released_nested_savepoint_callbacks_still_belong_to_outer(session_id, monkeypatch, context, inner_name):
    from app.db import database

    calls = []
    monkeypatch.setattr(session_events, "send_wake", lambda *a, **k: calls.append(a))
    with context() as conn:
        cur = conn.cursor()
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 7})
        with pytest.raises(ValueError):
            with database._savepoint(cur, "sp_outer"):
                catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 8})
                with database._savepoint(cur, inner_name):
                    catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 9})
                raise ValueError("roll back released inner callbacks as well")
        catalog.after_event_mirrored(cur, {"session_id": session_id, "seq": 10})
    assert calls == [(session_id, 7), (session_id, 10)]


def test_lock_retry_discards_failed_callbacks_without_touching_outer_savepoints(
    connect, session_id, monkeypatch,
):
    from app.core.global_events import global_events
    from app.db import composer_suggestions, sessions

    wakes, writes, published, registered, delivered, outer = [], [], [], [], [], []
    monkeypatch.setattr(session_events, "send_wake", lambda *a, **k: wakes.append(a))
    monkeypatch.setattr(session_events, "notify_session_write", lambda *a: writes.append(a))
    monkeypatch.setattr(global_events, "lifecycle", lambda *a, **k: published.append(a))
    monkeypatch.setattr(session_events, "LOCK_RETRY_DELAYS_SECONDS", (0,))
    record = sessions._record_user_message
    supersede = composer_suggestions.supersede
    attempts = 0

    def observe_callback(*args, **kwargs):
        callback = record(*args, **kwargs)
        assert callback is not None
        attempt = len(registered) + 1
        registered.append(attempt)

        def notify():
            delivered.append(attempt)
            callback()

        return notify

    def contend_after_callback(conn, sid):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # The user-activity callback is already registered. Fail on a REAL
            # lock timeout later in the same transaction, not a mocked append.
            assert registered == [1]
            cursor = conn.cursor()
            cursor.execute("SET LOCAL lock_timeout = '100ms'")
            cursor.execute("SELECT pg_advisory_xact_lock(191734)")
        return supersede(conn, sid)

    holder = connect()
    holder.cursor().execute("SELECT pg_advisory_xact_lock(191734)")

    def release_for_retry(_delay):
        assert delivered == published == wakes == writes == []
        assert session_events.get_events(session_id) == []
        assert outer == []
        holder.rollback()

    monkeypatch.setattr(sessions, "_record_user_message", observe_callback)
    monkeypatch.setattr(composer_suggestions, "supersede", contend_after_callback)
    monkeypatch.setattr(session_events.time, "sleep", release_for_retry)
    try:
        with base.transaction() as conn:
            base.after_commit(conn, lambda: outer.append("kept"))
            with pytest.raises(ValueError, match="outer savepoint"):
                with database._savepoint(conn.cursor(), "around_retry"):
                    base.after_commit(conn, lambda: outer.append("abandoned"))
                    assert session_events.append_events_with_lock_retry(session_id, [{
                        "id": "retry-" + session_id, "type": "message.user",
                        "payload": {"content": "already submitted"},
                    }]) == (1, 1)
                    assert outer == []
                    assert registered == [1, 2] and delivered == [2]
                    assert len(published) == 1
                    assert wakes == [(session_id, 1)]
                    assert writes == [(session_id, "session_events")]
                    raise ValueError("outer savepoint rolls back, not independent retry")
            assert outer == []
        assert outer == ["kept"]
        assert attempts == 2 and delivered == [2]
        assert [row["id"] for row in session_events.get_events(session_id)] == ["retry-" + session_id]
    finally:
        holder.rollback()


@pytest.mark.parametrize("context", [base.get_db, base.transaction])
@pytest.mark.parametrize("rollback", [False, True])
def test_caller_append_reader_wakes_follow_savepoint_outcome(session_id, monkeypatch, context, rollback):
    wakes = []
    monkeypatch.setattr(session_events, "send_wake", lambda *a, **k: wakes.append(a))

    def append(conn, label):
        session_events.append_events(session_id, [{
            "type": "system.note", "payload": {"text": label},
        }], _conn=conn)

    with context() as conn:
        append(conn, "before")
        with pytest.raises(ValueError) if rollback else nullcontext():
            with database._savepoint(conn.cursor(), "reader_wake"):
                append(conn, "inside")
                if rollback:
                    raise ValueError("discard inner append and reader wake")
        append(conn, "after")
        assert wakes == []
    labels = ["before", "after"] if rollback else ["before", "inside", "after"]
    assert [row["payload"]["text"] for row in session_events.get_events(session_id)] == labels
    assert wakes == [(session_id, seq) for seq in range(1, len(labels) + 1)]
