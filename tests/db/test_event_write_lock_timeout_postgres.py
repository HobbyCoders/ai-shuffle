"""Event writes behind a notifying commit that is stuck flushing (#1065).

PostgreSQL holds a server-wide lock from a notifying transaction's commit through
its WAL flush. To hold it on demand, each test here runs on a private cluster
whose synchronous standby does not exist: a commit asking for synchronous
replication then hangs with that lock held, as a slow disk does for a moment. The
app's own connections use synchronous_commit=local, so only the lock can stop them.
"""
import asyncio
import json
import select
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.errors import LockNotAvailable

from tests.db.postgres_harness import requires_real_pg
from tests.db.schema_lifecycle_helpers import pg16_bindir

pytestmark = requires_real_pg

LOCK_TIMEOUT_SECONDS = 2
BIG = 3000  # over FLUSH_MAX_BUFFER_CHARS: the delta flushes as its own batch
NOTE = {"type": "system.note", "payload": {}}

_NOTIFY_LOCK = """SELECT pid, granted FROM pg_locks
                  WHERE locktype = 'object' AND database = 0 AND objid = 0
                    AND classid = 'pg_database'::regclass"""


@pytest.fixture(scope="module", autouse=True)
def cluster(tmp_path_factory):
    from app.core.config import settings
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema

    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("needs PostgreSQL 16 server binaries")
    bin_path = Path(bindir)
    root = tmp_path_factory.mktemp("event-lock-pg")
    data = root / "data"
    subprocess.run([str(bin_path / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust",
                    "--no-sync", "--encoding=UTF8", "--no-locale"],
                   check=True, capture_output=True, timeout=60)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options = (f"-p {port} -h 127.0.0.1 -k '' -c fsync=off "
               f"-c lock_timeout={LOCK_TIMEOUT_SECONDS}s -c synchronous_commit=local "
               "-c synchronous_standby_names=ghost_standby")
    subprocess.run([str(bin_path / "pg_ctl"), "-D", str(data), "-w", "-l", str(root / "log"),
                    "-o", options, "start"], check=True, capture_output=True, timeout=60)
    connection = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=port, user="postgres",
                      dbname="postgres", options="", connect_timeout=5, sslmode="disable")
    original = settings.get_database_connection
    object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
    base.init_pool(min_conn=1, max_conn=8)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield connection
    finally:
        base.close_pool()
        object.__setattr__(settings, "get_database_connection", original)
        subprocess.run([str(bin_path / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True, timeout=60)


@pytest.fixture
def connect(cluster):
    opened = []

    def open_connection(*, autocommit=False):
        conn = psycopg2.connect(host="127.0.0.1", port=cluster["port"], user="postgres",
                                dbname="postgres")
        conn.autocommit = autocommit
        opened.append(conn)
        return conn

    yield open_connection
    for conn in opened:
        conn.close()


@pytest.fixture
def session_id():
    from app.db import base

    sid = "pg-lock-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    return sid


def _count(session_id: str) -> int:
    from app.db import base

    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM session_events WHERE session_id = %s", (session_id,))
        return int(cursor.fetchone()["n"])


@contextmanager
def stalled_notifier(connect):
    """A notifying commit stuck in its flush, holding the notify lock."""
    holder = connect()
    admin = connect(autocommit=True)
    cursor = holder.cursor()
    cursor.execute("SET synchronous_commit = on")
    holder.commit()
    cursor.execute("SELECT pg_backend_pid()")
    pid = cursor.fetchone()[0]
    holder.commit()
    cursor.execute("INSERT INTO sessions (id) VALUES (%s)", ("pg-holder-" + uuid4().hex,))
    cursor.execute("SELECT pg_notify('session_events', %s)", (json.dumps({"holder": True}),))
    thread = threading.Thread(target=holder.commit)
    thread.start()
    probe = admin.cursor()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        probe.execute(_NOTIFY_LOCK)
        if (pid, True) in probe.fetchall():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the holder never took the notify lock")
    try:
        yield
    finally:
        probe.execute("SELECT pg_cancel_backend(%s)", (pid,))
        thread.join(10)
        assert not thread.is_alive()


def _listener(connect):
    conn = connect(autocommit=True)
    conn.cursor().execute("LISTEN session_events")
    return conn


def _next_wake(conn, session_id: str, timeout: float):
    """The next wake for ``session_id``, or None once ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while True:
        conn.poll()
        while conn.notifies:
            data = json.loads(conn.notifies.pop(0).payload)
            if data.get("session_id") == session_id:
                return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        select.select([conn], [], [], remaining)


# ---------------------------------------------------------------------------
# What PostgreSQL does: the facts the fix rests on
# ---------------------------------------------------------------------------

def test_lock_timeout_at_commit_means_rolled_back_on_a_live_connection(connect, session_id):
    """Why a 55P03 is safe to re-send, and the failure the fix removes."""
    writer = connect()
    cursor = writer.cursor()
    event_id = "raw-" + uuid4().hex
    with stalled_notifier(connect):
        cursor.execute(
            "INSERT INTO session_events (id, session_id, seq, event_type, payload) "
            "VALUES (%s, %s, 1, 'system.note', '{}')", (event_id, session_id),
        )
        cursor.execute("SELECT pg_notify('session_events', 'x')")
        started = time.monotonic()
        with pytest.raises(LockNotAvailable) as raised:
            writer.commit()
        assert raised.value.pgcode == "55P03"
        assert time.monotonic() - started >= LOCK_TIMEOUT_SECONDS - 0.5
        assert writer.closed == 0
        assert writer.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_IDLE
        assert _count(session_id) == 0


def test_a_commit_without_notify_ignores_the_lock(connect, session_id):
    writer = connect()
    cursor = writer.cursor()
    with stalled_notifier(connect):
        cursor.execute(
            "INSERT INTO session_events (id, session_id, seq, event_type, payload) "
            "VALUES (%s, %s, 1, 'system.note', '{}')", ("raw-" + uuid4().hex, session_id),
        )
        started = time.monotonic()
        writer.commit()
        assert time.monotonic() - started < 1
    assert _count(session_id) == 1


def test_a_transaction_that_only_notifies_does_not_wait_for_a_flush(connect):
    """Why the separate wake transaction cannot become the next holder."""
    writer = connect()
    cursor = writer.cursor()
    cursor.execute("SET synchronous_commit = on")
    writer.commit()
    cursor.execute("SELECT pg_notify('session_events', 'x')")
    started = time.monotonic()
    done = threading.Thread(target=writer.commit, daemon=True)
    done.start()
    done.join(5)
    assert not done.is_alive(), "a notify-only commit waited on the missing standby"
    assert time.monotonic() - started < 1


# ---------------------------------------------------------------------------
# append_events and its wake
# ---------------------------------------------------------------------------

def test_event_commit_is_not_held_by_a_stalled_notifier(connect, session_id, monkeypatch):
    from app.core.event_gateway import get_gateway
    from app.db import session_events as se

    delivered = []
    monkeypatch.setattr(get_gateway(), "_handle_payload", delivered.append)
    with stalled_notifier(connect):
        started = time.monotonic()
        first, last = se.append_events(session_id, [NOTE])
        elapsed = time.monotonic() - started
        assert _count(session_id) == 1
    assert (first, last) == (1, 1)
    assert elapsed < LOCK_TIMEOUT_SECONDS
    # The lock stayed busy, so the wake reached this process's readers directly.
    assert [json.loads(payload) for payload in delivered] == [{"session_id": session_id, "max_seq": 1}]


def test_wake_arrives_after_the_row_is_visible(connect, session_id):
    from app.db import session_events as se

    listener = _listener(connect)
    _, last = se.append_events(session_id, [NOTE])
    assert _next_wake(listener, session_id, 5) == {"session_id": session_id, "max_seq": last}
    assert _count(session_id) == 1


def test_caller_owned_wake_waits_for_the_callers_commit(connect, session_id):
    from app.db import base, session_events as se

    listener = _listener(connect)
    with base.get_db() as conn:
        _, last = se.append_events(session_id, [NOTE], _conn=conn)
        assert _next_wake(listener, session_id, 0.2) is None
    assert _next_wake(listener, session_id, 5) == {"session_id": session_id, "max_seq": last}


def test_caller_rollback_sends_no_wake_and_keeps_no_row(connect, session_id):
    from app.db import base, session_events as se

    listener = _listener(connect)
    with pytest.raises(RuntimeError, match="caller failed"):
        with base.get_db() as conn:
            se.append_events(session_id, [NOTE], _conn=conn)
            raise RuntimeError("caller failed")
    assert _next_wake(listener, session_id, 0.3) is None
    assert _count(session_id) == 0


def test_backfill_wakes_after_commit(connect, session_id):
    from app.db import session_events as se

    listener = _listener(connect)
    _, last = se.append_backfill_events(session_id, [NOTE])
    assert _next_wake(listener, session_id, 5) == {"session_id": session_id, "max_seq": last}


def test_wake_carries_the_log_recheck_flag(connect, session_id):
    from app.db import session_events as se

    listener = _listener(connect)
    se.send_wake(session_id, 0, recheck_log=True)
    assert _next_wake(listener, session_id, 5) == {
        "session_id": session_id, "max_seq": 0, "recheck_log": True,
    }


# ---------------------------------------------------------------------------
# The recorder against real lock timeouts on the counter row
# ---------------------------------------------------------------------------

@contextmanager
def held_counter_row(connect, session_id):
    """Keep every event writer of this session waiting on its counter row."""
    from app.db import session_events as se

    se.append_events(session_id, [NOTE])
    blocker = connect()
    blocker.cursor().execute(
        "SELECT 1 FROM session_event_counters WHERE session_id = %s FOR UPDATE", (session_id,),
    )
    try:
        yield blocker
    finally:
        blocker.rollback()


def _counted_appends(monkeypatch):
    from app.db import session_events as se

    real = se.append_events
    attempts = []

    def append(*args, **kwargs):
        attempts.append(time.monotonic())
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "append_events", append)
    return attempts


async def _wait_for(predicate, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


def _delta(text: str) -> dict:
    return {"type": "stream_delta", "delta_type": "text", "content": text}


async def test_recorder_resends_after_a_real_lock_timeout(connect, session_id, monkeypatch):
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events as se

    recorder = await TurnRecorder.maybe_create(session_id)
    await recorder.start("hello")
    with held_counter_row(connect, session_id) as blocker:
        attempts = _counted_appends(monkeypatch)
        first = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
        await _wait_for(lambda: len(attempts) >= 2)  # the first attempt timed out
        second = asyncio.create_task(recorder.on_event(_delta("B" * BIG)))
        await asyncio.sleep(0.05)
        assert not second.done()  # queued behind the batch being re-sent
        blocker.rollback()
        await asyncio.gather(first, second)

    assert not recorder.recording_failed
    rows = se.get_events(session_id)
    texts = [str(row["payload"]["text"])[0] for row in rows
             if row["event_type"] == "assistant.text_delta"]
    assert texts == ["A", "B"]
    seqs = [row["seq"] for row in rows]
    assert seqs == list(range(1, len(seqs) + 1))
    await recorder.close()


async def test_a_real_resend_is_refused_by_a_moved_fence(connect, session_id, monkeypatch):
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events as se

    recorder = await TurnRecorder.maybe_create(session_id)
    await recorder.start("hello")
    assert recorder.run_id is not None
    with held_counter_row(connect, session_id) as blocker:
        before = len(se.get_events(session_id))
        attempts = _counted_appends(monkeypatch)
        write = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
        await _wait_for(lambda: len(attempts) >= 2)
        # Another runner took over while this batch was waiting to be re-sent.
        admin = connect(autocommit=True)
        admin.cursor().execute("SET lock_timeout = 0")
        admin.cursor().execute("UPDATE runs SET fence_token = fence_token + 1 WHERE id = %s",
                               (recorder.run_id,))
        blocker.rollback()
        await write

    assert recorder._superseded
    assert not recorder.recording_failed
    assert len(se.get_events(session_id)) == before
    await recorder.close()


async def test_a_lost_connection_is_not_resent(connect, session_id, monkeypatch):
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events as se

    recorder = await TurnRecorder.maybe_create(session_id)
    await recorder.start("hello")
    failed = []
    recorder.on_recording_failed = lambda: failed.append(True)
    admin = connect(autocommit=True).cursor()
    with held_counter_row(connect, session_id):
        before = len(se.get_events(session_id))
        attempts = _counted_appends(monkeypatch)
        write = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
        pids = []

        def waiter_found():
            admin.execute("SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                          "AND query LIKE %s", ("%session_event_counters%",))
            pids.extend(row[0] for row in admin.fetchall())
            return bool(pids)

        await _wait_for(waiter_found)
        admin.execute("SELECT pg_terminate_backend(%s)", (pids[0],))
        await write

    assert len(attempts) == 1
    assert failed == [True]
    assert recorder.recording_failed
    assert len(se.get_events(session_id)) == before
    await recorder.close()
