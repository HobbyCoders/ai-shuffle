"""Delete versus the real event recorder, interleaved on private PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.db import base, runs, session_events, sessions, turns
from tests.db.postgres_harness import requires_real_pg
from tests.db import schema_lifecycle_helpers as lifecycle

pytestmark = requires_real_pg


@pytest.fixture(autouse=True)
def multi_session_gate_is_first(monkeypatch):
    """Catch a gate acquired after ANY mutation/admission lock, not just R/C/S.

    The one documented exception is the deleted chat's own hand-off headers
    (``lock_handoff_history``): no gate holder ever waits for those (#1215).
    """
    from app.db import session_locks

    original = session_locks.lock_multi_session_mutation
    calls = []

    def checked(cursor):
        cursor.execute("SELECT mode FROM pg_locks WHERE pid=pg_backend_pid() AND granted "
                       "AND mode IN ('RowShareLock','RowExclusiveLock') "
                       "AND relation NOT IN (SELECT 'handoff_history_chunks'::regclass UNION ALL "
                       "SELECT indexrelid FROM pg_index WHERE indrelid='handoff_history_chunks'::regclass)")
        assert cursor.fetchall() == [], "multi-session gate must precede every row lock"
        calls.append(True)
        original(cursor)

    monkeypatch.setattr(session_locks, "lock_multi_session_mutation", checked)
    return calls


@pytest.fixture(scope="module", autouse=True)
def private_pool(tmp_path_factory):
    from app.core.config import settings
    from app.db.schema_postgres import create_postgres_schema

    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    with lifecycle.private_postgres(bindir, tmp_path_factory.mktemp("delete-lock-pg")) as d:
        kwargs = dict(host=d["host"], port=d["port"], user=d["user"],
                      password=d["password"], dbname=d["database"])
        original = settings.get_database_connection
        object.__setattr__(settings, "get_database_connection", lambda: dict(kwargs))
        base.init_pool(min_conn=1, max_conn=6)
        try:
            with base.get_db() as conn:
                create_postgres_schema(conn.cursor())
            from app.db import mobile_tool_identity
            mobile_tool_identity.init_schema()
            yield kwargs
        finally:
            base.close_pool()
            object.__setattr__(settings, "get_database_connection", original)


@pytest.mark.parametrize("fenced", [True, False], ids=["run-fence", "unfenced-counter"])
def test_delete_waits_before_removing_event_runtime(private_pool, monkeypatch, fenced):
    sid = "delete-lock-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    run = runs.claim_run(sid, "recorder", 60)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    # No filesystem or wakeup service is needed to reproduce the DB transaction.
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    appender_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if threading.current_thread().name.startswith("recorder"):
                fence_lock = "FROM runs WHERE id = %s FOR SHARE" in sql
                counter_lock = (sql.startswith("INSERT INTO session_event_counters")
                                and "RETURNING next_seq" in sql)
                if (fence_lock if fenced else counter_lock) and not locked.is_set():
                    appender_pid.append(self.connection.get_backend_pid())
                    locked.set()
                    assert resume.wait(10), "test never released the recorder"
            return result

    cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)

    def append():
        return session_events.append_events(
            sid, [{"type": "turn.completed", "payload": {}}],
            run_id=run["id"] if fenced else None,
            fence_token=run["fence_token"] if fenced else None,
        )

    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder") as recorder, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        recording = recorder.submit(append)
        deleting = None
        try:
            assert locked.wait(5), "recorder never acquired its first lock"
            deleting = deleter.submit(sessions.delete_session, sid)
            # Observe a real lock wait, not a sleep guessing which transaction won.
            deadline = time.monotonic() + 5
            with observer.cursor() as probe:
                while True:
                    probe.execute("SELECT pg_stat_clear_snapshot()")
                    probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                                  (appender_pid[0],))
                    if probe.fetchone():
                        break
                    if deleting.done():
                        deleting.result()  # Surface SQL setup failures, not a timing error.
                        pytest.fail("delete finished while the recorder held its lock")
                    assert time.monotonic() < deadline, "delete did not wait behind recorder"
                    time.sleep(0.01)
            assert not deleting.done()
        finally:
            resume.set()
        assert recording.result(timeout=5) == (2, 2)
        assert deleting.result(timeout=5)

    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        for table in sessions._SESSION_RUNTIME_TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0, f"delete left orphan rows in {table}"


def _wait_for_lock_wait(observer, blocker_pid, pending):
    deadline = time.monotonic() + 5
    with observer.cursor() as probe:
        while True:
            probe.execute("SELECT pg_stat_clear_snapshot()")
            probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                          (blocker_pid,))
            if probe.fetchone():
                return
            if pending.done():
                pending.result()
                pytest.fail("transaction finished without the required lock wait")
            assert time.monotonic() < deadline, "transaction never waited for its competitor"
            time.sleep(0.01)


def _assert_deleted(sid):
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        for table in sessions._SESSION_RUNTIME_TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0, f"delete left orphan rows in {table}"


@pytest.mark.parametrize("path,with_run", [("park-repair", True), ("park-repair", False), ("expired-settle", True)])
@pytest.mark.parametrize("first", ["settler", "delete"])
def test_delete_vs_settlement_both_lock_orders(private_pool, monkeypatch, path, with_run, first):
    from types import SimpleNamespace
    from app.core.session_settlement import _capture_and_dismiss

    sid = "delete-settle-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    run = runs.claim_run(sid, "settlement", 60) if with_run else None
    turn = turns.enqueue_turn(sid, "user", prompt="settlement interleaving")
    turns.start_turn(turn["id"], run_id=run["id"] if run else None)
    if path == "park-repair":
        parked = turns.park_turn(turn["id"], usage={"input_tokens": 1})
        # The real ProviderAgent repair arguments, including the admission guard.
        settle = lambda: turns.settle_parked_turns(
            sid, turn_ids=[turn["id"]], only_if_idle=True,
            observed_park=parked, can_settle=lambda: True,
        )
    else:
        with base.get_db() as conn:
            conn.cursor().execute("UPDATE runs SET lease_expires_at=NOW()-INTERVAL '1 minute' WHERE id=%s",
                                  (run["id"],))
        # An in-process owner wait makes the production capture reach the
        # expired-run fence, rather than its deliberately empty-snapshot branch.
        broker = SimpleNamespace(snapshot_pending=lambda _: ([SimpleNamespace(session_id=sid)], [], []))
        settle = lambda: _capture_and_dismiss(sid, broker, None)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    first_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            name = threading.current_thread().name
            if not name.startswith(first) or locked.is_set():
                return result
            run_lock = "FROM runs WHERE session_id" in sql and "FOR UPDATE" in sql and self.rowcount > 0
            counter_lock = sql.startswith("INSERT INTO session_event_counters")
            counter_row_lock = "FROM session_event_counters WHERE" in sql and "FOR UPDATE" in sql
            session_lock = "FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql
            if first == "delete":
                boundary = counter_lock if path == "park-repair" else run_lock
            else:
                # The old path starts with session/counter; the fixed path
                # starts with the shared run lock. Pause at either first lock.
                boundary = run_lock or counter_lock or (session_lock if path == "park-repair" else counter_row_lock)
            if boundary:
                first_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released the first transaction"
            return result

    original_cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="settler") as settler, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        leading = settler.submit(settle) if first == "settler" else deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "first transaction never reached its lock boundary"
            trailing = deleter.submit(sessions.delete_session, sid) if first == "settler" else settler.submit(settle)
            _wait_for_lock_wait(observer, first_pid[0], trailing)
        finally:
            resume.set()
        settle_result = (leading if first == "settler" else trailing).result(timeout=5)
        assert (trailing if first == "settler" else leading).result(timeout=5)
        if first == "settler":
            if path == "park-repair":
                assert settle_result == 1
            else:
                assert [row["id"] for row in settle_result["aborted"]] == [turn["id"]]
        elif path == "park-repair":
            assert settle_result == 0
        else:
            assert settle_result is None
    _assert_deleted(sid)


@pytest.mark.parametrize("backfill", [False, True], ids=["unfenced-append", "backfill"])
def test_delete_wins_late_event_write_cannot_resurrect_runtime(private_pool, monkeypatch, backfill):
    sid = "delete-late-event-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    delete_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if (threading.current_thread().name.startswith("delete")
                    and sql.startswith("INSERT INTO session_event_counters") and not locked.is_set()):
                delete_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released deletion"
            return result

    original_cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    append = session_events.append_backfill_events if backfill else session_events.append_events
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-event") as writer:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "delete never owned the counter lock"
            writing = writer.submit(append, sid, [{"type": "system.note", "payload": {}}])
            _wait_for_lock_wait(observer, delete_pid[0], writing)
        finally:
            resume.set()
        assert deleting.result(timeout=5)
        # Rejection unwinds the append's transaction, including its counter
        # upsert. Neither an event nor a counter may reappear after deletion.
        with pytest.raises(ValueError, match="session.*does not exist"):
            writing.result(timeout=5)
    _assert_deleted(sid)



def test_post_delete_stop_notice_delivery_drains_oldest_batch(private_pool, monkeypatch):
    from app.db import stop_notices

    sid = "deleted-stop-notices-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    notice_ids = ["stop-incomplete-" + uuid4().hex for _ in range(17)]
    for notice_id in notice_ids:
        stop_notices.save(notice_id, sid, {"content": "cleanup is incomplete"})
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    assert sessions.delete_session(sid)
    # Knowing an outbox id cannot grant an ordinary writer post-delete access.
    with pytest.raises(ValueError, match="session.*does not exist"):
        session_events.append_events(sid, [{"id": notice_ids[0], "type": "system.note",
                                           "payload": {"subtype": "stop_incomplete", "data": {"forged": True}}}])
    _assert_deleted(sid)
    first = stop_notices.pending()
    assert len(first) == 16
    assert all(stop_notices.deliver(notice_id) for notice_id in first)
    remaining = stop_notices.pending()
    assert len(remaining) == 1
    assert stop_notices.deliver(remaining[0])
    assert stop_notices.pending() == []
    assert all(stop_notices.deliver(notice_id) for notice_id in notice_ids)  # idempotent acknowledgements
    events = session_events.get_events(sid)
    assert {event["id"] for event in events} == set(notice_ids)
    assert all(event["payload"]["subtype"] == "stop_incomplete" for event in events)



def _delete_behind_writer(private_pool, monkeypatch, sid, write, old_boundary, *, lock_session=None,
                          mirror_sessions=None):
    locked, resume = threading.Event(), threading.Event()
    writer_pid = []
    counter_order = []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            name = threading.current_thread().name
            if name.startswith("writer") and mirror_sessions is not None:
                text = sql.decode() if isinstance(sql, bytes) else sql
                if text.startswith("INSERT INTO session_event_counters"):
                    if not counter_order:
                        # Prove the real state row, not merely its table lock,
                        # was acquired before the mirror's first C lock.
                        with psycopg2.connect(**private_pool) as probe:
                            with pytest.raises(psycopg2.errors.LockNotAvailable):
                                probe.cursor().execute("SELECT id FROM network_state WHERE id=1 FOR UPDATE NOWAIT")
                            probe.rollback()
                    counter_order.append(params[0])
                if text.startswith("INSERT INTO session_events"):
                    assert set(counter_order) == set(mirror_sessions)
            result = super().execute(sql, params)
            if name.startswith(("writer", "asyncio")) and not locked.is_set():
                text = sql.decode() if isinstance(sql, bytes) else sql
                run_lock = "FROM runs WHERE session_id" in text and "FOR UPDATE" in text and self.rowcount > 0
                if (run_lock or old_boundary(text)) and (lock_session is None or params == (lock_session,)):
                    writer_pid.append(self.connection.get_backend_pid())
                    locked.set()
                    assert resume.wait(10), "test never released writer"
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="writer") as writer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        writing = writer.submit(write)
        try:
            if not locked.wait(5):
                pytest.fail(f"writer never reached its mutation lock: {writing.result(timeout=1)!r}")
            deleting = deleter.submit(sessions.delete_session, sid)
            _wait_for_lock_wait(observer, writer_pid[0], deleting)
        finally:
            resume.set()
        writing.result(timeout=5)
        assert deleting.result(timeout=5)
    _assert_deleted(sid)
    if mirror_sessions is not None:
        assert list(dict.fromkeys(counter_order)) == sorted(mirror_sessions)


@pytest.mark.parametrize("path", ["deferred-report", "clear", "question-answer"])
def test_delete_vs_report_clear_and_answer(private_pool, monkeypatch, path):
    import asyncio
    from unittest.mock import AsyncMock
    from app.db import interactions
    from app.core import session_orchestration
    from app.services import notification_service

    sid = "delete-mutation-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    runs.claim_run(sid, "mutation", 60)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    if path == "deferred-report":
        monkeypatch.setattr(notification_service, "emit_notification", AsyncMock())
        write = lambda: asyncio.run(session_orchestration.defer_started_report(
            "missing-starter", {"task_id": sid, "agent_message_turn_id": "finished", "status": "completed"}))
        boundary = lambda sql: "FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql
    elif path == "clear":
        write = lambda: sessions.clear_session_conversation(sid)
        boundary = lambda sql: sql.startswith("DELETE FROM session_events WHERE")
    else:
        prompt = interactions.create_interaction(sid, "question", {}, None)
        write = lambda: interactions.answer_question_and_enqueue(prompt, {"answer": "yes"}, None, "yes", {})
        boundary = lambda sql: (sql.startswith("UPDATE pending_interactions SET state='answered'")
                                or ("FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql))
    _delete_behind_writer(private_pool, monkeypatch, sid, write, boundary)



def test_fork_cannot_message_parent_before_or_during_admission(private_pool, monkeypatch):
    from app.db import database
    from app.core import session_orchestration as orch

    token = uuid4().hex
    parent, child, profile = "z-parent-" + token, "a-child-" + token, "agent-" + token
    database.create_profile(profile, "Lock test", None, {"model": "sonnet"})
    for sid, owner in ((parent, None), (child, parent)):
        sessions.create_session(sid, profile, title=sid, is_active=True, model="sonnet",
                                permission_mode="default", parent_session_id=owner)
        turn = turns.enqueue_turn(sid, "user", prompt="work", profile_id=profile)
        turns.settle_turn(turn["id"], "completed")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    # Forks are not top-level peers: neither initial discovery nor the locked
    # admission recheck permits the alleged child -> parent MessageSession.
    with pytest.raises(ValueError, match="Top-level orchestration"):
        orch._queue(child, {"session_id": parent, "message": "Report to parent",
                            "request_id": "parent-delete-race"}, "message")
    before = sessions.get_session(child)
    snapshot = {**before, "parent_session_id": None}
    with base.get_db() as conn, pytest.raises(ValueError, match="scope or permissions changed"):
        orch._recheck(conn.cursor(), [snapshot], {child: None})
    assert sessions.get_session(child)["parent_session_id"] == parent



def test_copy_rejects_destination_deleted_while_waiting_for_counter(private_pool, monkeypatch):
    source, dest = "copy-source-" + uuid4().hex, "copy-dest-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().executemany("INSERT INTO sessions (id) VALUES (%s)", [(source,), (dest,)])
    session_events.append_events(source, [{"type": "system.note", "payload": {"content": "keep source"}}])
    deleting_locked, resume = threading.Event(), threading.Event()
    delete_pid = []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            if sql.startswith("DELETE FROM session_events WHERE") and params == (dest,):
                delete_pid.append(self.connection.get_backend_pid())
                deleting_locked.set()
                assert resume.wait(10)
            return super().execute(sql, params)

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, ThreadPoolExecutor(max_workers=2) as workers:
        observer.autocommit = True
        deleting = workers.submit(sessions.delete_session, dest)
        assert deleting_locked.wait(5)
        copying = workers.submit(session_events.copy_session_events, source, dest)
        try:
            _wait_for_lock_wait(observer, delete_pid[0], copying)
        finally:
            resume.set()
        assert deleting.result(timeout=5)
        with pytest.raises(ValueError, match="session.*does not exist"):
            copying.result(timeout=5)
    _assert_deleted(dest)
    assert len(session_events.get_events(source)) == 1



def test_delete_vs_child_first_shared_active_batch(private_pool, monkeypatch):
    from app.network import session_active

    token = uuid4().hex
    parent, child = "z-active-parent-" + token, "a-active-child-" + token
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (parent,))
        conn.cursor().execute("INSERT INTO sessions (id,parent_session_id) VALUES (%s,%s)", (child, parent))
    for sid in (parent, child):
        runs.claim_run(sid, "active-batch", 60)
    write = lambda: session_active.write_shared_many([child, parent], False)
    boundary = lambda sql: "FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql
    _delete_behind_writer(private_pool, monkeypatch, parent, write, boundary)
    assert sessions.get_session(child)["parent_session_id"] is None
    assert sessions.get_session(child)["is_active"] is False


def test_delete_vs_private_history_publication(private_pool, monkeypatch):
    from app.network import handoff

    sid, token = "delete-history-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
        # An empty portable snapshot deliberately prunes the destination's stale
        # event. It is the same durable header install_history stages before
        # publication; all reconciliation/counter/session writes remain real.
        conn.cursor().execute("INSERT INTO handoff_history_chunks "
                              "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                              "VALUES (%s,%s,NULL,'',0,'{}')", (token, sid))
    runs.claim_run(sid, "history-publisher", 60)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])

    def publish():
        with base.get_db() as conn:
            return handoff._publish_history(conn.cursor(), sid, token)

    boundary = lambda sql: (sql.startswith("DELETE FROM session_events WHERE")
                            or sql.startswith("INSERT INTO session_event_counters"))
    _delete_behind_writer(private_pool, monkeypatch, sid, publish, boundary)



@pytest.mark.parametrize("path", ["publish", "accept-owner", "finalize-wakes", "discard"])
@pytest.mark.parametrize("first", ["delete", "handoff"])
def test_delete_vs_handoff_history_both_lock_orders(private_pool, monkeypatch, path, first):
    """#1215: delete and every hand-off history writer share one H -> C -> S order.

    Before the fix delete took C and S first and only reached the private
    hand-off header H through its FK cascade, while the hand-off paths hold H
    and then need C (publication) or S (staging cleanup): DeadlockDetected in
    both start orders.
    """
    from types import SimpleNamespace

    from app.network import handoff

    sid, token = "handoff-delete-" + uuid4().hex, uuid4().hex
    source, target = "source-" + uuid4().hex, "target-" + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, source))
        # The same durable header install_history stages before publication.
        # An empty snapshot still prunes/reserves events through the real SQL.
        cur.execute("INSERT INTO handoff_history_chunks "
                    "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                    "VALUES (%s,%s,NULL,'',0,'{}')", (token, sid))
        cur.execute("INSERT INTO handoff_wakes (token,session_id,source_id,wakes) VALUES (%s,%s,%s,'[]')",
                    (token, sid, source))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(handoff.engine, "require_context", lambda: SimpleNamespace(device_id=target))

    def write():
        if path == "publish":
            with base.get_db() as conn:
                return handoff._publish_history(conn.cursor(), sid, token)
        if path == "accept-owner":
            return handoff._accept_owner(sid, target, None, token)
        if path == "finalize-wakes":
            return handoff._finalize_wakes(sid, source, {"token": token})
        return handoff._discard_history(sid, token)

    locked, resume = threading.Event(), threading.Event()
    first_pid = []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if threading.current_thread().name.startswith(first) and not locked.is_set():
                if first == "delete":
                    # Delete owns its whole lock prefix (old: C and S; new: H too).
                    boundary = sql.startswith("DELETE FROM session_events WHERE") and params == (sid,)
                else:
                    # The hand-off owns its first private header, nothing else yet.
                    boundary = "FROM handoff_history_chunks" in sql and "FOR UPDATE" in sql
                if boundary:
                    first_pid.append(self.connection.get_backend_pid())
                    locked.set()
                    assert resume.wait(10), "test never released the first transaction"
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff") as handing, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        if first == "delete":
            leading = deleting = deleter.submit(sessions.delete_session, sid)
        else:
            leading = writing = handing.submit(write)
        try:
            if not locked.wait(5):
                pytest.fail(f"first transaction never reached its boundary: {leading.result(timeout=1)!r}")
            if first == "delete":
                writing = handing.submit(write)
            else:
                deleting = deleter.submit(sessions.delete_session, sid)
            # A real PostgreSQL lock wait on the leader, never a sleep.
            _wait_for_lock_wait(observer, first_pid[0], writing if first == "delete" else deleting)
        finally:
            resume.set()
        assert deleting.result(timeout=10)
        if first == "handoff":
            # The hand-off committed whole before deletion could start.
            assert writing.result(timeout=10) == ({} if path == "publish" else [] if path != "discard" else None)
        elif path in ("accept-owner", "finalize-wakes"):
            with pytest.raises(ValueError, match="deleted"):
                writing.result(timeout=10)
        else:
            # Deletion's cascade removed the staging rows the writer waited for.
            assert writing.result(timeout=10) is None
    _assert_deleted(sid)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


def test_private_history_fence_has_only_two_counter_round_trips(private_pool, monkeypatch):
    from app.network import handoff

    sid, token = "history-budget-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
        conn.cursor().execute("INSERT INTO handoff_history_chunks "
                              "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                              "VALUES (%s,%s,NULL,'',0,'{}')", (token, sid))
    calls = []
    original_cursor = base._PgDictConnection.cursor

    class CountedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            if ("session_event_counters" in sql or sql.startswith("SELECT COALESCE(MAX(seq)")
                    or sql.startswith("SELECT id FROM sessions")):
                calls.append(sql)
            return super().execute(sql, params)

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = CountedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    with base.get_db() as conn:
        handoff._publish_history(conn.cursor(), sid, token)
    # Reserve C before reconciliation and advance its watermark afterwards.
    # Reuse publication's mandatory final session UPDATE as the fresh existence
    # check rather than adding round trips to the large-history ratchet.
    assert len(calls) == 2, calls


def test_child_inserted_after_delete_discovery_cannot_invert_batch(private_pool, monkeypatch):
    from app.network import session_active

    token = uuid4().hex
    parent, child = "z-dynamic-parent-" + token, "a-dynamic-child-" + token
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (parent,))
    runs.claim_run(parent, "dynamic-parent", 60)
    discovered, discovery_resume = threading.Event(), threading.Event()
    child_locked, child_resume = threading.Event(), threading.Event()
    writer_pid, delete_prefixes = [], []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            name = threading.current_thread().name
            if name.startswith("delete") and not discovered.is_set() and sql.startswith(
                    "SELECT id FROM sessions WHERE id=%s OR parent_session_id=%s"):
                discovered.set()  # The result set contains the parent only.
                assert discovery_resume.wait(10)
            if name.startswith("delete") and "FROM runs WHERE session_id" in sql and "FOR UPDATE" in sql:
                delete_prefixes.append(params[0])
            if name.startswith("writer") and not child_locked.is_set() and params == (child,) and (
                    "FROM runs WHERE session_id" in sql and "FOR UPDATE" in sql):
                writer_pid.append(self.connection.get_backend_pid())
                child_locked.set()
                assert child_resume.wait(10)
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="writer") as writer:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, parent)
        try:
            assert discovered.wait(5)
            with base.get_db() as conn:
                conn.cursor().execute("INSERT INTO sessions (id,parent_session_id) VALUES (%s,%s)",
                                      (child, parent))
            runs.claim_run(child, "dynamic-child", 60)
            writing = writer.submit(session_active.write_shared_many, [child, parent], False)
            assert child_locked.wait(5)
            discovery_resume.set()
            _wait_for_lock_wait(observer, writer_pid[0], deleting)
        finally:
            discovery_resume.set()
            child_resume.set()
        writing.result(timeout=5)
        assert deleting.result(timeout=5)
    _assert_deleted(parent)
    assert delete_prefixes == sorted([child, parent])  # includes the post-snapshot child
    assert sessions.get_session(child)["parent_session_id"] is None
    assert runs.get_active_run(child) is not None


def test_single_session_append_turn_and_park_repair_never_take_multi_gate(
        private_pool, multi_session_gate_is_first):
    sid = "single-no-gate-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    run = runs.claim_run(sid, "single-session", 60)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    turn = turns.enqueue_turn(sid, "user", prompt="one session only")
    turns.start_turn(turn["id"], run_id=run["id"])
    parked = turns.park_turn(turn["id"], usage={"input_tokens": 1})
    assert turns.settle_parked_turns(
        sid, turn_ids=[turn["id"]], only_if_idle=True, observed_park=parked,
        can_settle=lambda: True,
    ) == 1
    assert multi_session_gate_is_first == []


def test_creation_and_first_event_can_share_the_uncommitted_transaction(private_pool):
    from app.db import database

    sid, profile = "first-event-" + uuid4().hex, "agent-" + uuid4().hex
    database.create_profile(profile, "Creation audit", None, {})
    with base.transaction() as conn:
        sessions.create_session(sid, profile, _conn=conn)
        assert session_events.append_events(sid, [{"type": "system.note", "payload": {}}], _conn=conn) == (1, 1)
        with psycopg2.connect(**private_pool) as observer:
            with observer.cursor() as cursor:
                cursor.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
                assert cursor.fetchone() is None  # creation has not committed yet
    # Independent writers are started after creation returns/commits in the
    # audited API, remote-control and worker-dispatch paths.
    assert session_events.append_events(sid, [{"type": "system.note", "payload": {}}]) == (2, 2)
    assert len(session_events.get_events(sid)) == 2


@pytest.mark.parametrize("deleted", ["sender", "receiver"])
def test_delete_vs_reachable_top_level_message(private_pool, monkeypatch, deleted):
    from app.db import database
    from app.core import session_orchestration as orch

    token = uuid4().hex
    sender, receiver, profile = "a-peer-" + token, "z-peer-" + token, "agent-" + token
    database.create_profile(profile, "Peer lock test", None, {"model": "sonnet"})
    for sid in (sender, receiver):
        sessions.create_session(sid, profile, title=sid, is_active=True, model="sonnet",
                                permission_mode="default")
        turn = turns.enqueue_turn(sid, "user", prompt="peer work", profile_id=profile)
        turns.settle_turn(turn["id"], "completed")
        runs.claim_run(sid, "peer-writer", 60)
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    write = lambda: orch._queue(sender, {"session_id": receiver, "message": "Review progress",
                                        "request_id": "delete-peer-race"}, "message")
    boundary = lambda sql: "FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql
    sid = sender if deleted == "sender" else receiver
    _delete_behind_writer(private_pool, monkeypatch, sid, write, boundary, lock_session=sid)


@pytest.mark.parametrize(("path", "family"), [
    (path, False) for path in ("envelope", "append", "chunk", "pending", "pending-fallback",
                               "snapshot-rewrite", "snapshot-fresh")
] + [(path, True) for path in ("chunk", "pending", "pending-fallback", "snapshot-rewrite", "snapshot-fresh")])
def test_delete_vs_ordinary_mirror_ingestion(private_pool, monkeypatch, path, family):
    """Drive the signed wire and snapshot producers, including a committed-row UPSERT."""
    from app.db import network as store
    from app.network import crypto, engine, envelope, identity, membership, policy

    def device():
        return identity.DeviceIdentity(crypto.generate_signing_key(), crypto.generate_encryption_key(),
                                       1, "2026-01-01T00:00:00Z")

    local, remote = device(), device()
    member = membership.Member(remote.device_id, remote.signing_public, remote.encryption_public,
                               "Remote owner", "worker", 0)
    ctx = engine.NetworkContext(local, "mirror-lock-" + uuid4().hex, "Mirror locks", remote.device_id,
                                0, 1, {1: crypto.generate_network_key()}, {remote.device_id: member}, "ready")
    sid = "z-delete-mirror-" + uuid4().hex
    child = "a-delete-mirror-" + uuid4().hex
    # Wire/page order is deliberately the opposite of runtime-prefix order.
    session_ids = [sid, child] if family else [sid]
    coll = policy.collection("session_events")
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO UPDATE SET network_id=EXCLUDED.network_id",
                       (ctx.network_id, remote.device_id))
        cursor.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
        if family:
            cursor.execute("INSERT INTO sessions (id,owner_device_id,parent_session_id) VALUES (%s,%s,%s)",
                           (child, remote.device_id, sid))
    envs, items = [], []
    for index, session_id in enumerate(session_ids, 1):
        session_events.append_events(session_id, [{"type": "system.note", "payload": {"content": "committed"}}])
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM session_events WHERE session_id=%s", (session_id,))
            record = engine.serialize_row(coll, dict(cursor.fetchone()))
        record["payload"] = {"content": "remote reconciliation"}
        if path in ("append", "snapshot-fresh"):
            record.update(id="mirror-event-" + uuid4().hex, seq=2)
        rid = coll.record_id(record)
        envs.append(envelope.build(
            network_id=ctx.network_id, collection=coll.name, record_id=rid,
            op="upsert", clock=2, origin_device_id=remote.device_id,
            origin_incarnation=1, origin_seq=index, schema_version=1, key_version=1,
            network_key=ctx.network_key, signing_private=remote.signing_private,
            record=record, order_seq=record["seq"],
        ).to_dict())
        items.append({"record_id": rid, "record": record, "clock": 2, "origin_device_id": remote.device_id,
                      "deleted": False, "order_seq": record["seq"]})

    if path.startswith("pending"):
        with base.get_db() as conn:
            for env in envs:
                store.stash_pending(conn.cursor(), env)

    def ingest():
        result = engine.ApplyResult()
        if path == "chunk":
            assert engine._apply_chunk(envs, ctx, result)
            assert result.applied == len(session_ids) and result.retry == 0
            return
        if path.startswith("pending"):
            assert engine._apply_pending_chunk(remote.device_id, 1, ctx, result,
                                               _try_append=path != "pending-fallback")
            assert result.applied == len(session_ids) and result.retry == 0
            return
        with base.get_db() as conn:
            cursor = conn.cursor()
            if path.startswith("snapshot"):
                applied, _ = engine.apply_snapshot_items(cursor, coll, items, ctx=ctx)
                assert applied == len(session_ids)
            else:
                store.lock_state(cursor)
                if path == "append":
                    assert engine._apply_event_append(cursor, envs, ctx, engine.ApplyResult())
                else:
                    assert engine.apply_envelope(cursor, envs[0], ctx=ctx) == "applied"

    # Before the fix the writer pauses owning an event tuple; delete then
    # owns C and waits for that tuple, while the resumed mirror waits for C.
    # After the fix the same barrier pauses at its earlier C lock instead.
    boundary = lambda sql: (sql.startswith("INSERT INTO session_events")
                            or sql.startswith("INSERT INTO session_event_counters"))
    _delete_behind_writer(private_pool, monkeypatch, sid, ingest, boundary, mirror_sessions=session_ids)
    if family:
        assert sessions.get_session(child)["parent_session_id"] is None
        assert len(session_events.get_events(child)) == (2 if path == "snapshot-fresh" else 1)


@pytest.mark.parametrize("path", ["envelope", "snapshot", "chunk", "pending", "pending-fallback"])
def test_delete_first_mirror_tombstone_cannot_recreate_counter(private_pool, monkeypatch, path):
    """A reservation's INSERT snapshot can outlive the session it selected.

    The tombstone is consumed (the stream moves on) but applies nothing for
    a chat whose delete committed while it waited (#1222).
    """
    from app.db import network as store
    from app.network import crypto, engine, envelope, identity, membership, policy

    def device():
        return identity.DeviceIdentity(crypto.generate_signing_key(), crypto.generate_encryption_key(),
                                       1, "2026-01-01T00:00:00Z")

    local, remote = device(), device()
    member = membership.Member(remote.device_id, remote.signing_public, remote.encryption_public,
                               "Remote owner", "worker", 0)
    ctx = engine.NetworkContext(local, "delete-tombstone-" + uuid4().hex, "Tombstone race", remote.device_id,
                                0, 1, {1: crypto.generate_network_key()}, {remote.device_id: member}, "ready")
    sid = "delete-tombstone-" + uuid4().hex
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO UPDATE SET network_id=EXCLUDED.network_id",
                       (ctx.network_id, remote.device_id))
        cursor.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    rid = sid + "|1"
    env = envelope.build(
        network_id=ctx.network_id, collection="session_events", record_id=rid,
        op="delete", clock=2, origin_device_id=remote.device_id,
        origin_incarnation=1, origin_seq=1, schema_version=1, key_version=1,
        network_key=ctx.network_key, signing_private=remote.signing_private, order_seq=1,
    ).to_dict()
    if path.startswith("pending"):
        with base.get_db() as conn:
            store.stash_pending(conn.cursor(), env)

    def ingest():
        result = engine.ApplyResult()
        if path == "chunk":
            assert engine._apply_chunk([env], ctx, result)
            assert (result.applied, result.duplicate, result.retry) == (0, 1, 0)
        elif path.startswith("pending"):
            assert engine._apply_pending_chunk(remote.device_id, 1, ctx, result,
                                               _try_append=path != "pending-fallback")
            assert (result.applied, result.duplicate, result.retry) == (0, 1, 0)
        else:
            with base.get_db() as conn:
                cursor = conn.cursor()
                if path == "snapshot":
                    item = {"record_id": rid, "record": None, "clock": 2,
                            "origin_device_id": remote.device_id, "deleted": True, "order_seq": 1}
                    applied, _ = engine.apply_snapshot_items(cursor, policy.collection("session_events"),
                                                             [item], ctx=ctx)
                    assert applied == 0
                else:
                    store.lock_state(cursor)
                    assert engine.apply_envelope(cursor, env, ctx=ctx) == "duplicate"

    locked, resume = threading.Event(), threading.Event()
    delete_pid = []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if (threading.current_thread().name.startswith("delete") and not locked.is_set()
                    and sql.startswith("INSERT INTO session_event_counters")):
                delete_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released deletion"
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="tombstone") as mirror:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "delete never acquired the counter"
            applying = mirror.submit(ingest)
            # The mirror's INSERT SELECT has seen S and is actually waiting
            # on delete's C; release only after PostgreSQL confirms that wait.
            _wait_for_lock_wait(observer, delete_pid[0], applying)
        finally:
            resume.set()
        assert deleting.result(timeout=5)
        applying.result(timeout=5)
    _assert_deleted(sid)


@pytest.mark.parametrize("path", ["chunk", "append", "envelope", "pending", "pending-fallback", "snapshot"])
def test_delete_first_fresh_mirror_events_are_dropped(private_pool, monkeypatch, path):
    """#1222: fresh events waiting behind delete's counter must not land after it.

    Before the fix the waiting mirror inserted both events and
    after_event_mirrored recreated the counter at next_seq=3 for a chat
    that no longer existed, while reporting two applied events.
    """
    from app.db import network as store
    from app.network import crypto, engine, envelope, identity, membership, policy

    def device():
        return identity.DeviceIdentity(crypto.generate_signing_key(), crypto.generate_encryption_key(),
                                       1, "2026-01-01T00:00:00Z")

    local, remote = device(), device()
    member = membership.Member(remote.device_id, remote.signing_public, remote.encryption_public,
                               "Remote owner", "worker", 0)
    ctx = engine.NetworkContext(local, "delete-fresh-" + uuid4().hex, "Fresh mirror race", remote.device_id,
                                0, 1, {1: crypto.generate_network_key()}, {remote.device_id: member}, "ready")
    sid = "delete-fresh-" + uuid4().hex
    coll = policy.collection("session_events")
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO UPDATE SET network_id=EXCLUDED.network_id",
                       (ctx.network_id, remote.device_id))
        cursor.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
    session_events.append_events(sid, [{"type": "system.note", "payload": {"content": "local"}}])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s", (sid,))
        template = engine.serialize_row(coll, dict(cursor.fetchone()))
    envs, items = [], []
    for index, seq in enumerate((2, 3), 1):
        record = {**template, "id": "fresh-event-" + uuid4().hex, "seq": seq,
                  "payload": {"content": f"remote {seq}"}}
        rid = coll.record_id(record)
        envs.append(envelope.build(
            network_id=ctx.network_id, collection=coll.name, record_id=rid,
            op="upsert", clock=2 + index, origin_device_id=remote.device_id,
            origin_incarnation=1, origin_seq=index, schema_version=1, key_version=1,
            network_key=ctx.network_key, signing_private=remote.signing_private,
            record=record, order_seq=seq,
        ).to_dict())
        items.append({"record_id": rid, "record": record, "clock": 2 + index,
                      "origin_device_id": remote.device_id, "deleted": False, "order_seq": seq})
    if path.startswith("pending"):
        with base.get_db() as conn:
            for env in envs:
                store.stash_pending(conn.cursor(), env)

    def ingest():
        result = engine.ApplyResult()
        if path == "chunk":
            assert engine._apply_chunk(envs, ctx, result)
        elif path.startswith("pending"):
            assert engine._apply_pending_chunk(remote.device_id, 1, ctx, result,
                                               _try_append=path != "pending-fallback")
        else:
            with base.get_db() as conn:
                cursor = conn.cursor()
                if path == "snapshot":
                    assert engine.apply_snapshot_items(cursor, coll, items, ctx=ctx) == (0, [])
                    return
                store.lock_state(cursor)
                if path == "append":
                    assert engine._apply_event_append(cursor, envs, ctx, result)
                else:
                    assert [engine.apply_envelope(cursor, env, ctx=ctx, result=result)
                            for env in envs] == ["duplicate", "duplicate"]
                    return
        # Consumed (the stream moves on) but nothing applied for a deleted chat.
        assert (result.applied, result.duplicate, result.retry, result.rejected) == (0, 2, 0, 0)
        with base.get_db() as conn:
            assert store.get_watermark(conn.cursor(), remote.device_id, 1) == 2

    locked, resume = threading.Event(), threading.Event()
    delete_pid = []
    original_cursor = base._PgDictConnection.cursor

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if (threading.current_thread().name.startswith("delete") and not locked.is_set()
                    and sql.startswith("INSERT INTO session_event_counters")):
                delete_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released deletion"
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="mirror") as mirror:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "delete never acquired the counter"
            applying = mirror.submit(ingest)
            # The mirror has seen S and really waits on delete's C.
            _wait_for_lock_wait(observer, delete_pid[0], applying)
        finally:
            resume.set()
        assert deleting.result(timeout=5)
        applying.result(timeout=5)
    _assert_deleted(sid)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sync_records WHERE collection='session_events' "
                       "AND record_id LIKE %s", (sid + "|%",))
        assert cursor.fetchone()["n"] == 0
        cursor.execute("SELECT COUNT(*) AS n FROM sync_pending WHERE origin_device_id=%s",
                       (remote.device_id,))
        assert cursor.fetchone()["n"] == 0


def test_mirror_deleted_mark_holds_only_while_the_session_is_absent(private_pool, monkeypatch):
    """The window -> per-op memory never drops events for a chat that exists."""
    from app.db import network as store
    from app.network import engine

    gone, back = "mark-gone-" + uuid4().hex, "mark-back-" + uuid4().hex
    monkeypatch.setattr(engine, "_MIRROR_DELETED", {gone: None, back: None})
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO NOTHING", ("mark-test", "mark-device"))
        # A locally deleted mirror whose owner re-sent the catalog.
        cursor.execute("INSERT INTO sessions (id) VALUES (%s)", (back,))
        store.lock_state(cursor)
        assert engine._lock_mirror_event_counters(cursor, [gone + "|1", back + "|1"]) == {gone}
        cursor.execute("SELECT COUNT(*) AS n FROM session_event_counters WHERE session_id=%s", (gone,))
        assert cursor.fetchone()["n"] == 0
    assert engine._MIRROR_DELETED == {gone: None}


def test_mirror_reservation_preserves_durable_post_delete_stop_runtime(private_pool, monkeypatch):
    from app.db import network as store, stop_notices
    from app.network import engine

    sid, notice_id = "mirror-stop-" + uuid4().hex, "stop-warning-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    stop_notices.save(notice_id, sid, {"content": "cleanup is incomplete"})
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    assert sessions.delete_session(sid)
    assert stop_notices.deliver(notice_id)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO NOTHING", ("mirror-stop-test", "mirror-stop-device"))
        store.lock_state(cursor)
        engine._lock_mirror_event_counters(cursor, [sid + "|1"])
        cursor.execute("SELECT next_seq FROM session_event_counters WHERE session_id=%s", (sid,))
        assert cursor.fetchone()["next_seq"] == 1
    assert sessions.get_session(sid) is None
    assert [event["id"] for event in session_events.get_events(sid)] == [notice_id]
