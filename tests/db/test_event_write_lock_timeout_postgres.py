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
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
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


async def _start_interruptible_turn(session_id: str):
    from app.core.session_runtime import SessionRuntime
    from app.db import turns as turns_db

    turn = turns_db.enqueue_turn(session_id, "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn(session_id, "stop")
    return turn, SessionRuntime(turn), turns_db


async def test_stop_signals_the_provider_while_the_counter_row_is_held(connect, session_id):
    """The review's scenario: every event write waits, and Stop must not."""
    from app.core import turn_recorder as tr
    from app.db import session_events as se

    turn, runtime, turns_db = await _start_interruptible_turn(session_id)
    signalled = []
    began = time.monotonic()

    async def signal(_sid):
        signalled.append(time.monotonic() - began)
        turns_db.settle_turn(turn["id"], "aborted", "racer")

    with held_counter_row(connect, session_id) as blocker, \
            patch("app.core.session_runtime._should_evict_after_escalation", return_value=False), \
            patch("app.core.query_session_lifecycle.interrupt_session", new=signal):
        await runtime.interrupt(escalation_timeout=2)
        assert len(signalled) == 1
        assert signalled[0] < 1, "Stop waited on the event log"
        types = [row["event_type"] for row in se.get_events(session_id)]
        assert "turn.interrupting" not in types  # still queued on the held row
        blocker.rollback()
        await tr.drain_event_writes(15)

    assert "turn.interrupting" in [row["event_type"] for row in se.get_events(session_id)]


async def test_a_cancelled_close_writes_its_final_batch_once_the_row_frees(connect, session_id):
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events as se

    recorder = await TurnRecorder.maybe_create(session_id)
    await recorder.start("hello")
    await recorder.on_event(_delta("A"))
    with held_counter_row(connect, session_id) as blocker:
        closing = asyncio.create_task(recorder.close())
        await asyncio.sleep(0.05)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        blocker.rollback()
        await recorder.writes_settled()

    rows = se.get_events(session_id)
    types = [row["event_type"] for row in rows]
    assert not recorder.recording_failed
    assert types.index("assistant.text_delta") < types.index("turn.aborted")
    seqs = [row["seq"] for row in rows]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_a_lease_renewal_lands_between_retries(connect, session_id, monkeypatch):
    """The fenced writer holds its run row while it waits, but only during attempts."""
    from app.core import turn_recorder as tr
    from app.core.turn_recorder import TurnRecorder
    from app.db import runs as runs_db

    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (1.0, 1.0, 1.0, 1.0))
    recorder = await TurnRecorder.maybe_create(session_id)
    await recorder.start("hello")
    outcomes = []

    with held_counter_row(connect, session_id) as blocker:
        writing = asyncio.create_task(recorder.on_event(_delta("A" * 4096)))
        for _ in range(16):
            try:
                outcomes.append(await asyncio.to_thread(
                    runs_db.renew_run, recorder.run_id, recorder.runner_id,
                    recorder.fence_token, 60))
            except LockNotAvailable:
                outcomes.append("refused")
            await asyncio.sleep(0.25)
        blocker.rollback()
        await writing

    assert "refused" in outcomes  # the writer really was holding the run row
    assert True in outcomes
    await recorder.close()
    assert not recorder.recording_failed


def _parked_turn(session_id: str) -> str:
    from app.db import turns as turns_db

    turn = turns_db.enqueue_turn(session_id, "user", prompt="park", profile_id="default")
    turns_db.start_turn(turn["id"], None, "runner")
    assert turns_db.park_turn(turn["id"], ["task"])
    return turn["id"]


async def test_a_stop_settles_the_park_it_read(session_id):
    """The observed interval's timestamp has to match on a real timestamp column."""
    from app.core.query_session_lifecycle import enter_user_stop
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    parked = _parked_turn(session_id)
    try:
        assert (await enter_user_stop(session_id))["parked_settled"] == 1
    finally:
        task_registry.clear_user_stop(session_id)

    assert turns_db.get_turn(parked)["state"] == "completed"


async def test_a_stop_behind_the_admission_lock_leaves_a_park_that_moved(connect, session_id):
    from app.core.query_session_lifecycle import enter_user_stop
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    parked = _parked_turn(session_id)
    admission = connect()
    cursor = admission.cursor()
    cursor.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
    stop = asyncio.create_task(enter_user_stop(session_id))
    await asyncio.sleep(0.5)
    assert not stop.done()  # its park write is waiting for the lock
    cursor.execute("UPDATE turns SET ended_at = NOW() WHERE id = %s", (parked,))  # a new interval
    admission.commit()

    try:
        assert (await stop)["parked_settled"] == 0
    finally:
        task_registry.clear_user_stop(session_id)
    assert turns_db.get_turn(parked)["state"] == "parked"


_PEER_INSTALL = """
import json, sys
from app.core.config import settings
from app.db import base
connection = json.loads(sys.argv[1])
object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
base.init_pool(min_conn=1, max_conn=2)
from app.core import turn_manager
print(turn_manager._submit_blocking(sys.argv[2], "user", prompt="newer", profile_id="default"))
base.close_pool()
"""


def _peer_install_sends(connection: dict, session_id: str) -> str:
    """A second Python process, whose stop latch and registry are its own."""
    done = subprocess.run([sys.executable, "-c", _PEER_INSTALL, json.dumps(connection), session_id],
                          cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                          timeout=90)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip().splitlines()[-1]


async def _interrupted_with_older_queued(session_id: str):
    """A running turn with a message behind it, and a Stop admitted on it."""
    from app.core.session_runtime import SessionRuntime
    from app.db import turns as turns_db

    turn = turns_db.enqueue_turn(session_id, "user", prompt="hi", profile_id="default")
    turns_db.start_turn(turn["id"], None, "runner")
    older = turns_db.enqueue_turn(session_id, "user", prompt="older", profile_id="default")["id"]
    assert turns_db.interrupt_turn(session_id, "stop", stop_background=True, source="user_stop")

    async def provider_stops(_sid):
        turns_db.settle_turn(turn["id"], "aborted", "interrupted")

    return SessionRuntime(turn), older, provider_stops


async def _finish_interrupt(runtime, provider_stops) -> None:
    """Includes the teardown, which outlives the interrupt call by design."""
    from app.core import turn_recorder as tr

    with patch("app.core.query_session_lifecycle.interrupt_session", new=provider_stops):
        await runtime.interrupt("stop", escalation_timeout=1)
        steps = [task for task in asyncio.all_tasks()
                 if "_run_interrupt_step" in task.get_coro().__qualname__]
        if steps:
            await asyncio.wait(steps, timeout=20)
    await tr.drain_event_writes(5)


@pytest.fixture
def stop_cleared(session_id):
    from app.core.task_registry import task_registry

    yield
    task_registry.clear_user_stop(session_id)


async def test_a_peer_installs_message_outlives_a_stop_admitted_before_it(cluster, session_id, stop_cleared):
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    runtime, older, provider_stops = await _interrupted_with_older_queued(session_id)
    newer = await asyncio.to_thread(_peer_install_sends, cluster, session_id)
    await _finish_interrupt(runtime, provider_stops)

    assert turns_db.get_turn(older)["state"] == "aborted"
    assert turns_db.get_turn(newer)["state"] == "queued"
    assert not task_registry.is_user_stopped(session_id)


async def test_a_peer_installs_message_clears_the_stop_held_here_when_it_runs(
    cluster, session_id, stop_cleared,
):
    """Its send cannot reach this process's latch, so its start has to."""
    from app.core import start_admission, turn_scheduler
    from app.core.session_runtime import SessionRuntime
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    runtime, _older, provider_stops = await _interrupted_with_older_queued(session_id)
    await _finish_interrupt(runtime, provider_stops)
    assert task_registry.is_user_stopped(session_id)
    newer = await asyncio.to_thread(_peer_install_sends, cluster, session_id)

    async def deferred(*_args, **_kwargs):
        return "deferred"

    with patch.object(start_admission.admission, "wait", new=deferred), \
            patch.object(turn_scheduler, "_kick_at_reset"):
        assert await SessionRuntime(turns_db.get_turn(newer)).execute() is None

    assert turns_db.get_turn(newer)["state"] == "queued"
    assert not task_registry.is_user_stopped(session_id)


async def test_a_message_during_the_first_read_outlives_a_stop_with_no_earlier_latch(session_id, stop_cleared):
    from app.core import turn_manager
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    runtime, older, provider_stops = await _interrupted_with_older_queued(session_id)
    assert not task_registry.is_user_stopped(session_id)
    reached, resume = threading.Event(), threading.Event()
    real = turns_db.get_turn

    def paused(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread() and not reached.is_set():
            reached.set()
            resume.wait(10)
        return real(*args, **kwargs)

    with patch.object(turns_db, "get_turn", new=paused):
        interrupt = asyncio.create_task(_finish_interrupt(runtime, provider_stops))
        await asyncio.to_thread(reached.wait, 5)
        newer = await asyncio.to_thread(
            turn_manager._submit_blocking, session_id, "user", prompt="newer", profile_id="default",
        )
        resume.set()
        await interrupt

    assert turns_db.get_turn(older)["state"] == "aborted"
    assert turns_db.get_turn(newer)["state"] == "queued"
    assert not task_registry.is_user_stopped(session_id)


def _pending_wake(session_id: str, wake_id: str) -> dict:
    from app.db import wakeups as wakeups_db

    wakeups_db.create_wakeup(id=wake_id, session_id=session_id, prompt="the old instruction", profile_id="default",
                             fire_at=time.time() - 1)
    return next(row for row in wakeups_db.get_session_wakeups(session_id) if row["id"] == wake_id)


async def _fire(wake: dict) -> None:
    """The real firing path; this database has no agents to look the wake's up in."""
    from app.core import wakeup_service

    async def resolved(_row):
        return "default"

    with patch.object(wakeup_service, "_resolve_profile_id", new=resolved):
        await wakeup_service._fire(wake)


def _sources(session_id: str) -> list[str]:
    from app.db import base

    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT source FROM turns WHERE session_id = %s ORDER BY insert_order", (session_id,))
        return [row["source"] for row in cursor.fetchall()]


async def test_a_wake_both_cancels_timed_out_on_does_not_fire_after_the_newer_message(
    connect, session_id, stop_cleared,
):
    from app.core import turn_manager
    from app.core.query_session_lifecycle import stop_all_background_tasks
    from app.db import wakeups as wakeups_db

    wake_id = "wake-" + session_id
    wake = _pending_wake(session_id, wake_id)
    holder = connect()
    holder.cursor().execute("SELECT 1 FROM pending_wakeups WHERE id = %s FOR UPDATE", (wake_id,))
    await stop_all_background_tasks(session_id, user_stop=True)
    await asyncio.to_thread(
        turn_manager._submit_blocking, session_id, "user", prompt="newer", profile_id="default",
    )
    holder.rollback()
    assert wakeups_db.get_session_wakeups(session_id), "both cancels should have timed out"

    await _fire(wake)

    assert _sources(session_id) == ["user"]
    assert not wakeups_db.get_session_wakeups(session_id)


async def test_a_wake_armed_after_the_message_still_fires(session_id, stop_cleared):
    """Control: only a wake older than the person's message is superseded."""
    from app.core import turn_manager

    await asyncio.to_thread(
        turn_manager._submit_blocking, session_id, "user", prompt="newer", profile_id="default",
    )
    await _fire(_pending_wake(session_id, "wake-" + session_id))

    assert _sources(session_id) == ["user", "wakeup"]
