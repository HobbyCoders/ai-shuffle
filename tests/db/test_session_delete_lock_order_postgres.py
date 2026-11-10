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

    No exception: the hand-off headers come after the gate too (#1215).
    """
    from app.db import session_locks

    original = session_locks.lock_multi_session_mutation
    calls = []

    def checked(cursor):
        cursor.execute("SELECT mode FROM pg_locks WHERE pid=pg_backend_pid() AND granted "
                       "AND mode IN ('RowShareLock','RowExclusiveLock')")
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
    from app.db import network as store, session_locks
    from app.network import engine

    gone, back = "mark-gone-" + uuid4().hex, "mark-back-" + uuid4().hex
    monkeypatch.setattr(session_locks, "_DELETED", {gone: None, back: None})
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
    assert session_locks._DELETED == {gone: None}


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


def _remote_network(prefix):
    """A ready network context whose single remote member signs the test ops."""
    from app.network import crypto, engine, identity, membership

    def device():
        return identity.DeviceIdentity(crypto.generate_signing_key(), crypto.generate_encryption_key(),
                                       1, "2026-01-01T00:00:00Z")

    local, remote = device(), device()
    member = membership.Member(remote.device_id, remote.signing_public, remote.encryption_public,
                               "Remote owner", "worker", 0)
    ctx = engine.NetworkContext(local, prefix + uuid4().hex, "Lock order", remote.device_id,
                                0, 1, {1: crypto.generate_network_key()}, {remote.device_id: member}, "ready")
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                              "ON CONFLICT(id) DO UPDATE SET network_id=EXCLUDED.network_id",
                              (ctx.network_id, remote.device_id))
    return ctx, remote


def _session_tombstone(ctx, remote, sid):
    from app.network import envelope

    return envelope.build(
        network_id=ctx.network_id, collection="sessions", record_id=sid,
        op="delete", clock=2, origin_device_id=remote.device_id,
        origin_incarnation=1, origin_seq=1, schema_version=1, key_version=1,
        network_key=ctx.network_key, signing_private=remote.signing_private,
    ).to_dict()


def _stage_handoff_header(cur, sid, token):
    # The same durable header install_history stages before publication.
    cur.execute("INSERT INTO handoff_history_chunks "
                "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                "VALUES (%s,%s,NULL,'',0,'{}')", (token, sid))


class _Barriers:
    """Pause named threads at chosen SQL statements and record their backend pids."""

    def __init__(self, monkeypatch, rules):
        # rules: thread-name prefix -> (predicate(sql, params), pause_before)
        self.rules, self.reached, self.pids = rules, {}, {}
        self.resume = {name: threading.Event() for name in rules}
        for name in rules:
            self.reached[name] = threading.Event()
        barriers, original_cursor = self, base._PgDictConnection.cursor

        class InterleavedCursor(RealDictCursor):
            def execute(self, sql, params=None):
                name = next((n for n in barriers.rules if threading.current_thread().name.startswith(n)), None)
                if name is not None:
                    barriers.pids.setdefault(name, self.connection.get_backend_pid())
                hit = name is not None and not barriers.reached[name].is_set() and barriers.rules[name][0](sql, params)
                if hit and barriers.rules[name][1]:
                    barriers._pause(name)
                result = super().execute(sql, params)
                if hit and not barriers.rules[name][1]:
                    barriers._pause(name)
                return result

        def instrumented(self, *args, **kwargs):
            kwargs["cursor_factory"] = InterleavedCursor
            return original_cursor(self, *args, **kwargs)

        monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
        monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid, row=None: None)
        monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)

    def _pause(self, name):
        self.reached[name].set()
        assert self.resume[name].wait(10), f"test never released {name}"

    def wait(self, name, future):
        if not self.reached[name].wait(5):
            pytest.fail(f"{name} never reached its barrier: {future.result(timeout=1)!r}")

    def release_all(self):
        for event in self.resume.values():
            event.set()


@pytest.mark.parametrize("marked", [True, False], ids=["same-process", "other-process"])
def test_delete_committed_just_before_append_reservation_drops_events(private_pool, monkeypatch, marked):
    """#1222 (review): fast append read the chat, then the delete committed.

    The reservation INSERT then finds no session row at all, so nothing
    "vanished" under it. Before the fix both events were stored for a chat
    that no longer existed. The caller's own read proves the chat existed
    (``other-process``: no in-memory delete mark can help).
    """
    from app.db import network as store, session_locks
    from app.network import engine, envelope, policy

    ctx, remote = _remote_network("delete-before-reserve-")
    sid = "delete-before-reserve-" + uuid4().hex
    coll = policy.collection("session_events")
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
    session_events.append_events(sid, [{"type": "system.note", "payload": {"content": "local"}}])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s", (sid,))
        template = engine.serialize_row(coll, dict(cursor.fetchone()))
    envs = []
    for index, seq in enumerate((2, 3), 1):
        record = {**template, "id": "fresh-event-" + uuid4().hex, "seq": seq,
                  "payload": {"content": f"remote {seq}"}}
        envs.append(envelope.build(
            network_id=ctx.network_id, collection=coll.name, record_id=coll.record_id(record),
            op="upsert", clock=2 + index, origin_device_id=remote.device_id,
            origin_incarnation=1, origin_seq=index, schema_version=1, key_version=1,
            network_key=ctx.network_key, signing_private=remote.signing_private,
            record=record, order_seq=seq,
        ).to_dict())

    def ingest():
        result = engine.ApplyResult()
        with base.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            assert engine._apply_event_append(cursor, envs, ctx, result)
        return result

    # Pause BEFORE the reservation: the owner read is done, no counter is held.
    barriers = _Barriers(monkeypatch, {"mirror": (
        lambda sql, params: sql.startswith("INSERT INTO session_event_counters"), True)})
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mirror") as mirror:
        applying = mirror.submit(ingest)
        try:
            barriers.wait("mirror", applying)
            # Nothing the paused mirror holds is on deletion's path: it commits whole.
            assert sessions.delete_session(sid)
            if not marked:
                monkeypatch.setattr(session_locks, "_DELETED", {})
        finally:
            barriers.release_all()
        result = applying.result(timeout=10)
    assert (result.applied, result.duplicate, result.retry, result.rejected) == (0, 2, 0, 0)
    _assert_deleted(sid)
    with base.get_db() as conn:
        cursor = conn.cursor()
        assert store.get_watermark(cursor, remote.device_id, 1) == 2
        cursor.execute("SELECT COUNT(*) AS n FROM sync_records WHERE collection='session_events' "
                       "AND record_id LIKE %s", (sid + "|%",))
        assert cursor.fetchone()["n"] == 0


def test_bulk_membership_incoming_tombstone_and_local_delete_share_one_order(private_pool, monkeypatch):
    """Review S2: gate -> network_state -> hand-off header, never back.

    Bulk membership holds the gate and waits for network_state; an incoming
    session tombstone holds network_state and needs the chat's hand-off
    header (its FK cascade deletes it); a local delete of the same chat
    waits for the gate. With delete taking the header BEFORE the gate this
    closed a three-transaction cycle (DeadlockDetected).
    """
    from app.db import network as store
    from app.network import engine, session_active

    ctx, remote = _remote_network("three-way-")
    sid, token = "three-way-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
        _stage_handoff_header(cur, sid, token)
    env = _session_tombstone(ctx, remote, sid)

    def incoming():
        with base.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            return engine.apply_envelope(cursor, env, ctx=ctx)

    barriers = _Barriers(monkeypatch, {"incoming": (
        lambda sql, params: "FROM network_state WHERE id = 1 FOR UPDATE" in sql, False)})
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="bulk") as bulk, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        applying = syncing.submit(incoming)
        try:
            barriers.wait("incoming", applying)
            # 1. Bulk membership owns the gate and really waits for network_state.
            settling = bulk.submit(session_active.write_shared_many, [], False)
            _wait_for_lock_wait(observer, barriers.pids["incoming"], settling)
            # 2. The local delete really waits behind the bulk writer's gate.
            deleting = deleter.submit(sessions.delete_session, sid)
            with observer.cursor() as probe:
                probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                              (barriers.pids["incoming"],))
                bulk_pid = probe.fetchone()[0]
            _wait_for_lock_wait(observer, bulk_pid, deleting)
        finally:
            barriers.release_all()
        assert applying.result(timeout=10) == "applied"
        assert settling.result(timeout=10) == []
        assert deleting.result(timeout=10) is False  # The tombstone already removed it.
    _assert_deleted(sid)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("path", ["envelope", "snapshot"])
def test_incoming_session_tombstone_vs_handoff_staging_cleanup(private_pool, monkeypatch, path):
    """Review S3: a tombstone takes the hand-off header before the session row.

    Before the fix the tombstone's DELETE locked the session and its FK
    cascade then waited for the header, while ``_discard_history`` held the
    header and next locked the session: DeadlockDetected.
    """
    from app.db import network as store
    from app.network import engine, handoff, policy

    ctx, remote = _remote_network("tombstone-discard-")
    sid, token = "tombstone-discard-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
        _stage_handoff_header(cur, sid, token)
    env = _session_tombstone(ctx, remote, sid)

    def incoming():
        with base.get_db() as conn:
            cursor = conn.cursor()
            if path == "snapshot":
                item = {"record_id": sid, "record": None, "clock": 2,
                        "origin_device_id": remote.device_id, "deleted": True, "order_seq": None}
                return engine.apply_snapshot_items(cursor, policy.collection("sessions"), [item], ctx=ctx)[0]
            store.lock_state(cursor)
            return engine.apply_envelope(cursor, env, ctx=ctx)

    barriers = _Barriers(monkeypatch, {"discard": (
        lambda sql, params: "FROM handoff_history_chunks" in sql and "FOR UPDATE" in sql, False)})
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="discard") as cleaning, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing:
        observer.autocommit = True
        discarding = cleaning.submit(handoff._discard_history, sid, token)
        try:
            barriers.wait("discard", discarding)
            applying = syncing.submit(incoming)
            _wait_for_lock_wait(observer, barriers.pids["discard"], applying)
        finally:
            barriers.release_all()
        assert discarding.result(timeout=10) is None
        assert applying.result(timeout=10) in ("applied", 1)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("path", ["accept-owner", "discard"])
@pytest.mark.parametrize("first", ["handoff", "delete"])
def test_handoff_hold_runs_are_locked_before_counter_and_session(private_pool, monkeypatch, path, first):
    """Review S4: hand-off hold runs belong to step H, before C and S.

    The hold run reserves the parent chat's run slot. Deleting the parent
    locks the family's runs, counters and sessions in session order (the
    parent sorts first). Before the fix acceptance/cleanup held the child's
    counter or session and only then released the parent's hold run, which
    the delete already held while waiting for the child: DeadlockDetected.
    """
    from types import SimpleNamespace

    from app.network import handoff

    parent, sid = "a-hold-parent-" + uuid4().hex, "z-hold-child-" + uuid4().hex
    token, source, target = uuid4().hex, "source-" + uuid4().hex, "target-" + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (parent, source))
        cur.execute("INSERT INTO sessions (id,owner_device_id,parent_session_id) VALUES (%s,%s,%s)",
                    (sid, source, parent))
        _stage_handoff_header(cur, sid, token)
        cur.execute("INSERT INTO handoff_wakes (token,session_id,source_id,wakes) VALUES (%s,%s,%s,'[]')",
                    (token, sid, source))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    hold = runs.claim_run(parent, handoff.HOLD + token, 60)
    assert hold is not None
    monkeypatch.setattr(handoff.engine, "require_context", lambda: SimpleNamespace(device_id=target))

    def write():
        if path == "accept-owner":
            return handoff._accept_owner(sid, target, None, token)
        return handoff._discard_history(sid, token)

    if first == "handoff":
        # Holding the child's session row (and, accepting, its counter).
        rules = {"handoff": (lambda sql, params: sql.startswith(
            "SELECT owner_device_id,placement FROM sessions WHERE id=%s FOR UPDATE"), False)}
    else:
        # Holding the whole family's runs, counters and sessions.
        rules = {"delete": (lambda sql, params: sql.startswith("DELETE FROM session_events WHERE")
                            and params == (parent,), False)}
    barriers = _Barriers(monkeypatch, rules)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff") as handing, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        if first == "handoff":
            writing = handing.submit(write)
            try:
                barriers.wait("handoff", writing)
                deleting = deleter.submit(sessions.delete_session, parent)
                _wait_for_lock_wait(observer, barriers.pids["handoff"], deleting)
            finally:
                barriers.release_all()
        else:
            deleting = deleter.submit(sessions.delete_session, parent)
            try:
                barriers.wait("delete", deleting)
                writing = handing.submit(write)
                _wait_for_lock_wait(observer, barriers.pids["delete"], writing)
            finally:
                barriers.release_all()
        assert deleting.result(timeout=10)
        assert writing.result(timeout=10) == ([] if path == "accept-owner" else None)
    _assert_deleted(parent)
    child = sessions.get_session(sid)
    assert child is not None and child["parent_session_id"] is None
    if path == "accept-owner":
        assert child["owner_device_id"] == target
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM runs WHERE runner_id=%s AND state='active'", (handoff.HOLD + token,))
        assert cur.fetchone()["n"] == 0


def _late_events_for_deleted_chat(prefix, *, published):
    """A deleted mirror chat and two valid late event envelopes for it.

    ``published``: the chat's sync record is a tombstone (a peer's delete, or
    a local delete the scan published); otherwise it is the live record of a
    local delete the scan has not published yet. Either survives a restart.
    """
    from app.db import network as store
    from app.network import engine, envelope, policy

    ctx, remote = _remote_network(prefix)
    sid = prefix + uuid4().hex
    coll = policy.collection("session_events")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
        store.put_record(cur, "sessions", sid, clock=1, origin_device_id=remote.device_id,
                         content_hash="synced", deleted=False)
    session_events.append_events(sid, [{"type": "system.note", "payload": {"content": "local"}}])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s", (sid,))
        template = engine.serialize_row(coll, dict(cursor.fetchone()))
    envs, items = [], []
    for index, seq in enumerate((2, 3), 1):
        record = {**template, "id": "late-event-" + uuid4().hex, "seq": seq,
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
    assert sessions.delete_session(sid)
    if published:
        with base.get_db() as conn:
            store.put_record(conn.cursor(), "sessions", sid, clock=2, origin_device_id=remote.device_id,
                             content_hash="", deleted=True)
    return ctx, remote, sid, envs, items


def _reserve_in_fresh_process(private_pool, sid):
    """Run the mirror reservation in a new interpreter: no in-memory marks."""
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json, sys\n"
        "from app.core.config import settings\n"
        "from app.db import base, network as store, session_locks\n"
        "from app.network import engine\n"
        "kwargs, sid = json.loads(sys.argv[1]), sys.argv[2]\n"
        "object.__setattr__(settings, 'get_database_connection', lambda: dict(kwargs))\n"
        "base.init_pool(min_conn=1, max_conn=1)\n"
        "assert session_locks._DELETED == {}\n"
        "with base.get_db() as conn:\n"
        "    cursor = conn.cursor()\n"
        "    store.lock_state(cursor)\n"
        "    print(json.dumps(sorted(engine._lock_mirror_event_counters(cursor, [sid + '|e']))))\n"
        "base.close_pool()\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    done = subprocess.run([sys.executable, "-c", script, json.dumps(private_pool), sid], cwd=root,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("path", ["envelope", "chunk", "pending", "snapshot"])
@pytest.mark.parametrize("cause", ["evicted", "restart"])
@pytest.mark.parametrize("published", [True, False], ids=["tombstone", "unpublished-local-delete"])
def test_deleted_chat_late_events_rely_on_the_durable_record(private_pool, monkeypatch, path, cause, published):
    """#1222 (review S5): the in-memory delete marks are only a cache.

    Before the fix, once 1,025 newer deletions evicted the chat's mark (or a
    restart emptied the cache), two valid late envelopes for the deleted chat
    were applied: 0 sessions, 2 events, counter 3, tombstone still present.
    sync_records knowing the chat while its row is gone is the durable answer.
    (Fast append declines a chat with no row; ``chunk`` covers that fallback.)
    """
    from app.db import network as store
    from app.db import session_locks
    from app.network import engine, policy

    monkeypatch.setattr(session_locks, "_DELETED", dict(session_locks._DELETED))
    ctx, remote, sid, envs, items = _late_events_for_deleted_chat("durable-delete-", published=published)
    assert sid in session_locks._DELETED  # delete_session marked it
    if cause == "evicted":
        session_locks.mark_sessions_deleted(f"evict-{index}-{uuid4().hex}"
                                            for index in range(session_locks._DELETED_MAX + 1))
    else:
        # A new process starts with no marks at all, and still finds the deletion.
        assert _reserve_in_fresh_process(private_pool, sid) == [sid]
        monkeypatch.setattr(session_locks, "_DELETED", {})
    assert sid not in session_locks._DELETED

    result = engine.ApplyResult()
    if path == "chunk":
        assert engine._apply_chunk(envs, ctx, result)
    elif path == "pending":
        with base.get_db() as conn:
            for env in envs:
                store.stash_pending(conn.cursor(), env)
        assert engine._apply_pending_chunk(remote.device_id, 1, ctx, result)
    else:
        with base.get_db() as conn:
            cursor = conn.cursor()
            if path == "snapshot":
                assert engine.apply_snapshot_items(cursor, policy.collection("session_events"),
                                                   items, ctx=ctx) == (0, [])
            else:
                store.lock_state(cursor)
                assert [engine.apply_envelope(cursor, env, ctx=ctx, result=result)
                        for env in envs] == ["duplicate", "duplicate"]
    if path in ("chunk", "pending"):
        assert (result.applied, result.duplicate, result.retry, result.rejected) == (0, 2, 0, 0)
    _assert_deleted(sid)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sync_records WHERE collection='session_events' "
                       "AND record_id LIKE %s", (sid + "|%",))
        assert cursor.fetchone()["n"] == 0
        if path != "snapshot":
            assert store.get_watermark(cursor, remote.device_id, 1) == 2
        cursor.execute("SELECT COUNT(*) AS n FROM sync_pending WHERE origin_device_id=%s", (remote.device_id,))
        assert cursor.fetchone()["n"] == 0


def test_absent_chat_without_a_sync_record_is_not_arrived_yet(private_pool, monkeypatch):
    """Review S5: the durable check never refuses a chat whose create is in flight."""
    from app.db import network as store
    from app.db import session_locks
    from app.network import engine

    monkeypatch.setattr(session_locks, "_DELETED", {})
    unknown, deleted = "not-arrived-" + uuid4().hex, "durably-deleted-" + uuid4().hex
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO network_state (id,network_id,founder_device_id) VALUES (1,%s,%s) "
                       "ON CONFLICT(id) DO NOTHING", ("not-arrived-test", "not-arrived-device"))
        store.put_record(cursor, "sessions", deleted, clock=2, origin_device_id="peer",
                         content_hash="", deleted=True)
        store.lock_state(cursor)
        assert engine._lock_mirror_event_counters(cursor, [unknown + "|1", deleted + "|1"]) == {deleted}
        cursor.execute("SELECT COUNT(*) AS n FROM session_event_counters WHERE session_id IN (%s,%s)",
                       (unknown, deleted))
        assert cursor.fetchone()["n"] == 0
    assert session_locks._DELETED == {deleted: None}


@pytest.mark.parametrize("first", ["cancel", "delete"])
def test_handoff_cancel_vs_deleting_the_moving_chats_parent(private_pool, monkeypatch, first):
    """Review S4: cancellation takes its lease and hold runs in one sorted lock.

    Child z is moving and its hold run reserves parent a's run slot. Before
    the fix cancellation locked z's lease first; deleting a then locked a's
    runs and waited for z's; cancellation released the hold on a and waited
    for the delete: DeadlockDetected.
    """
    from app.network import handoff

    parent, sid = "a-cancel-parent-" + uuid4().hex, "z-cancel-child-" + uuid4().hex
    target = "target-" + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id) VALUES (%s)", (parent,))
        cur.execute("INSERT INTO sessions (id,parent_session_id) VALUES (%s,%s)", (sid, parent))
    lease = runs.claim_run(sid, handoff.HANDOFF + target, 60)
    assert lease is not None
    token = lease["id"]
    assert runs.claim_run(parent, handoff.HOLD + token, 60) is not None

    if first == "cancel":
        # Holding its first run lock: z's lease before the fix, both runs after.
        rules = {"cancel": (lambda sql, params: "FROM runs WHERE" in sql and "FOR UPDATE" in sql, False)}
    else:
        # Holding the whole family's runs, counters and sessions.
        rules = {"delete": (lambda sql, params: sql.startswith("DELETE FROM session_events WHERE")
                            and params == (parent,), False)}
    barriers = _Barriers(monkeypatch, rules)

    def cancel():
        return handoff.finish(sid, target, {"phase": "cancel", "token": token})

    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="cancel") as cancelling, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        if first == "cancel":
            cancelled = cancelling.submit(cancel)
            try:
                barriers.wait("cancel", cancelled)
                deleting = deleter.submit(sessions.delete_session, parent)
                _wait_for_lock_wait(observer, barriers.pids["cancel"], deleting)
            finally:
                barriers.release_all()
        else:
            deleting = deleter.submit(sessions.delete_session, parent)
            try:
                barriers.wait("delete", deleting)
                cancelled = cancelling.submit(cancel)
                _wait_for_lock_wait(observer, barriers.pids["delete"], cancelled)
            finally:
                barriers.release_all()
        assert deleting.result(timeout=10)
        assert cancelled.result(timeout=10) == {"cancelled": True}
    _assert_deleted(parent)
    child = sessions.get_session(sid)
    assert child is not None and child["parent_session_id"] is None
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM runs WHERE state='active' AND (id=%s OR runner_id=%s)",
                    (token, handoff.HOLD + token))
        assert cur.fetchone()["n"] == 0


def _wait_blocked_or_done(observer, blocker_pid, pending):
    """Return "blocked" once ``pending`` waits on ``blocker_pid``, or "done"."""
    deadline = time.monotonic() + 5
    with observer.cursor() as probe:
        while True:
            probe.execute("SELECT pg_stat_clear_snapshot()")
            probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                          (blocker_pid,))
            if probe.fetchone():
                return "blocked"
            if pending.done():
                return "done"
            assert time.monotonic() < deadline, "transaction neither finished nor waited"
            time.sleep(0.01)


def test_header_created_after_delete_scan_cannot_deadlock_cleanup(private_pool, monkeypatch):
    """Review S6: header creation is serialized with delete's header scan (X).

    Before the fix the delete scanned no header, ``install_history`` then
    committed one, the delete took the chat's run/counter/session locks,
    ``_discard_history`` locked the new header and waited for the session,
    and the delete's FK cascade waited for that header: DeadlockDetected.
    Now the install waits for the delete (a real lock wait) and then fails on
    the session FK, so nothing is staged for a deleted chat.
    """
    from app.network import handoff

    sid, token = "s6-header-race-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)

    # The delete pauses twice: after its header scan (holding G and, fixed,
    # X), then once it holds the chat's runs, counter and session row.
    stages = [lambda sql, params: "FROM handoff_history_chunks" in sql and "FOR UPDATE" in sql,
              lambda sql, params: sql.startswith("DELETE FROM session_events WHERE") and params == (sid,)]
    reached = [threading.Event() for _ in stages]
    resume = [threading.Event() for _ in stages]
    pids = {}
    original_cursor = base._PgDictConnection.cursor

    class StagedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if threading.current_thread().name.startswith("delete"):
                pids.setdefault("delete", self.connection.get_backend_pid())
                stage = next((i for i, hit in enumerate(stages) if not reached[i].is_set()), None)
                if stage is not None and stages[stage](sql, params):
                    reached[stage].set()
                    assert resume[stage].wait(10), "test never released the delete"
            return result

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = StagedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    data = {"history": {}, "token": token, "sdk_session_id": None, "_prepare_history": True}
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="install") as installer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="discard") as cleaner:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            if not reached[0].wait(5):
                pytest.fail(f"delete never scanned headers: {deleting.result(timeout=1)!r}")
            installing = installer.submit(handoff.install_history, sid, data, "/tmp/handoff-lab")
            install_state = _wait_blocked_or_done(observer, pids["delete"], installing)
            resume[0].set()
            if not reached[1].wait(5):
                pytest.fail(f"delete never locked the chat: {deleting.result(timeout=1)!r}")
            discarding = cleaner.submit(handoff._discard_history, sid, token)
            _wait_blocked_or_done(observer, pids["delete"], discarding)
        finally:
            for event in resume:
                event.set()
        assert deleting.result(timeout=10) is True
        assert discarding.result(timeout=10) is None
        assert install_state == "blocked", "header creation must wait for the delete's scan"
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            installing.result(timeout=10)
    _assert_deleted(sid)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("path", ["envelope", "snapshot"])
def test_header_created_after_tombstone_scan_cannot_deadlock_cleanup(private_pool, monkeypatch, path):
    """Review S6, incoming sync: a session tombstone takes X before its header scan.

    Before the fix the tombstone scanned no header, ``install_history``
    committed one, ``_discard_history`` locked it, the tombstone's DELETE
    locked the session and its cascade waited for the header, and the
    cleanup then waited for the session: DeadlockDetected.
    """
    from app.db import network as store
    from app.network import engine, handoff, policy

    ctx, remote = _remote_network("s6-tombstone-")
    sid, token = "s6-tombstone-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, remote.device_id))
    env = _session_tombstone(ctx, remote, sid)

    def incoming():
        with base.get_db() as conn:
            cursor = conn.cursor()
            if path == "snapshot":
                item = {"record_id": sid, "record": None, "clock": 2,
                        "origin_device_id": remote.device_id, "deleted": True, "order_seq": None}
                return engine.apply_snapshot_items(cursor, policy.collection("sessions"), [item], ctx=ctx)[0]
            store.lock_state(cursor)
            return engine.apply_envelope(cursor, env, ctx=ctx)

    scan = (lambda sql, params: "FROM handoff_history_chunks" in sql and "FOR UPDATE" in sql, False)
    barriers = _Barriers(monkeypatch, {"incoming": scan, "discard": scan})
    data = {"history": {}, "token": token, "sdk_session_id": None, "_prepare_history": True}
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="install") as installer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="discard") as cleaner:
        observer.autocommit = True
        applying = syncing.submit(incoming)
        try:
            barriers.wait("incoming", applying)
            installing = installer.submit(handoff.install_history, sid, data, "/tmp/handoff-lab")
            install_state = _wait_blocked_or_done(observer, barriers.pids["incoming"], installing)
            discarding = cleaner.submit(handoff._discard_history, sid, token)
            barriers.wait("discard", discarding)
            barriers.resume["incoming"].set()
            _wait_blocked_or_done(observer, barriers.pids["discard"], applying)
        finally:
            barriers.release_all()
        assert applying.result(timeout=10) in ("applied", 1)
        assert discarding.result(timeout=10) is None
        assert install_state == "blocked", "header creation must wait for the tombstone's scan"
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            installing.result(timeout=10)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


def test_acceptance_retires_only_headers_from_its_own_scan(private_pool, monkeypatch):
    """Review S7: supersession never scans for headers after the session lock.

    Before the fix acceptance locked the existing headers, a newer import then
    committed its header and its cleanup locked it, and acceptance (holding
    the session) scanned for superseded headers again: it waited on cleanup's
    header while cleanup waited on the session row, DeadlockDetected.
    """
    from types import SimpleNamespace

    from app.network import handoff

    sid, old, new = "s7-supersede-" + uuid4().hex, uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,'source')", (sid,))
    handoff.install_history(sid, {"history": {}, "token": old, "_prepare_history": True}, "/tmp/handoff-lab")
    monkeypatch.setattr(handoff.engine, "require_context", lambda: SimpleNamespace(device_id="target"))
    barriers = _Barriers(monkeypatch, {
        # Acceptance: right after its header scan (before C/S).
        "accept": (lambda sql, params: sql.startswith("SELECT token FROM handoff_history_chunks")
                   and "FOR UPDATE" in sql, False),
        # Cleanup of the newer import: holding its header, before the session.
        "discard": (lambda sql, params: sql.startswith("SELECT sdk_session_id,created_at,rows FROM "
                                                       "handoff_history_chunks"), False),
    })
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="accept") as accepting, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="discard") as cleaning:
        observer.autocommit = True
        accepted = accepting.submit(handoff._accept_owner, sid, "target", None, old)
        try:
            barriers.wait("accept", accepted)
            handoff.install_history(sid, {"history": {}, "token": new, "_prepare_history": True},
                                    "/tmp/handoff-lab")
            discarding = cleaning.submit(handoff._discard_history, sid, new)
            barriers.wait("discard", discarding)
            barriers.resume["accept"].set()
            state = _wait_blocked_or_done(observer, barriers.pids["discard"], accepted)
        finally:
            barriers.release_all()
        assert accepted.result(timeout=10) == []
        assert discarding.result(timeout=10) is None
        assert state == "done", "acceptance must not wait on a header created after its scan"
    session = sessions.get_session(sid)
    assert session["owner_device_id"] == "target"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


def test_project_delete_takes_the_ordered_session_delete(private_pool, monkeypatch):
    """Review S8: deleting a project deletes its chats through ``delete_session``.

    Before the fix ``delete_project`` ran a raw ``DELETE FROM sessions``: it
    locked the chat row, its FK cascade then waited for a hand-off header
    that staging cleanup held, and cleanup next waited for the chat row:
    DeadlockDetected. Now the delete waits at the header scan instead.
    """
    from app.db import projects
    from app.network import handoff

    project, sid, token = "s8-project-" + uuid4().hex, "s8-chat-" + uuid4().hex, uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO projects (id,name,path) VALUES (%s,'Lock order','/tmp/s8')", (project,))
        cur.execute("INSERT INTO sessions (id,project_id) VALUES (%s,%s)", (sid, project))
        _stage_handoff_header(cur, sid, token)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    barriers = _Barriers(monkeypatch, {"discard": (
        lambda sql, params: "FROM handoff_history_chunks" in sql and "FOR UPDATE" in sql, False)})
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="discard") as cleaning, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="project") as deleter:
        observer.autocommit = True
        discarding = cleaning.submit(handoff._discard_history, sid, token)
        try:
            barriers.wait("discard", discarding)
            deleting = deleter.submit(projects.delete_project, project)
            _wait_for_lock_wait(observer, barriers.pids["discard"], deleting)
        finally:
            barriers.release_all()
        assert discarding.result(timeout=10) is None
        assert deleting.result(timeout=10) is True
    _assert_deleted(sid)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM projects WHERE id=%s", (project,))
        assert cur.fetchone() is None
        cur.execute("SELECT COUNT(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


def test_project_delete_keeps_a_chat_moved_to_another_project(private_pool, monkeypatch):
    """Review S10: project delete re-checks membership under the chat's locks.

    ``delete_project`` lists its chats, then deletes each in its own ordered
    transaction. A chat moved to another project in between (a cross-device
    resume or worktree reopen) is no longer this project's: before the fix
    the unconditional delete destroyed it and its transcript. A barrier holds
    the delete right after its project read while the move commits.
    """
    from app.db import projects

    old, new, sid = "s10-old-" + uuid4().hex, "s10-new-" + uuid4().hex, "s10-chat-" + uuid4().hex
    stayed = "s10-stay-" + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO projects (id,name,path) VALUES (%s,'Old','/tmp/s10-old'),"
                    "(%s,'New','/tmp/s10-new')", (old, new))
        cur.execute("INSERT INTO sessions (id,project_id) VALUES (%s,%s),(%s,%s)", (sid, old, stayed, old))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    # After the project's chat list is read, before any chat is deleted.
    barriers = _Barriers(monkeypatch, {"project": (
        lambda sql, params: sql.startswith("SELECT id FROM sessions WHERE project_id"), False)})
    cleanups = []
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda s, row=None: cleanups.append(("ref", s)))
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda s: cleanups.append(("wake", s)))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="project") as deleter:
        deleting = deleter.submit(projects.delete_project, old)
        try:
            barriers.wait("project", deleting)
            assert sessions.set_session_project(sid, new)["project_id"] == new
        finally:
            barriers.release_all()
        assert deleting.result(timeout=10) is True
    moved = sessions.get_session(sid)
    assert moved is not None and moved["project_id"] == new
    assert session_events.get_events(sid), "the moved chat's transcript must survive"
    assert ("ref", sid) not in cleanups and ("wake", sid) not in cleanups
    _assert_deleted(stayed)
    assert ("ref", stayed) in cleanups and ("wake", stayed) in cleanups
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM projects WHERE id IN (%s,%s) ORDER BY id", (old, new))
        assert [row["id"] for row in cur.fetchall()] == [new]


# The owner's deletion fact (#1308): the owner's delete writes
# network_session_deletions in its own transaction; incoming sync never
# recreates that chat and its tombstone outranks the record it refused.

def _owned_chat(ctx, remote, prefix, *, record_clock=5):
    """A chat this worker owns whose sync record the previous owner signed."""
    from app.db import network as store

    sid = prefix + uuid4().hex
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id,owner_device_id) VALUES (%s,%s)", (sid, ctx.device_id))
        if record_clock is not None:
            store.put_record(cur, "sessions", sid, clock=record_clock, origin_device_id=remote.device_id,
                             content_hash="previous-owner", deleted=False)
    return sid


def _session_upsert(ctx, remote, sid, *, clock, seq=1, owner=None):
    """The previous owner's record of the chat (its delayed hand-off commit)."""
    from app.network import engine, envelope, policy

    coll = policy.collection("sessions")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
        row = cur.fetchone()
    row = dict(row) if row else {"id": sid}
    row["owner_device_id"] = owner or ctx.device_id
    record = engine.serialize_row(coll, row)
    return envelope.build(
        network_id=ctx.network_id, collection="sessions", record_id=sid,
        op="upsert", clock=clock, origin_device_id=remote.device_id,
        origin_incarnation=1, origin_seq=seq, schema_version=1, key_version=1,
        network_key=ctx.network_key, signing_private=remote.signing_private,
        record=record,
    ).to_dict()


def _session_record(sid):
    from app.db import network as store

    with base.get_db() as conn:
        return store.get_record(conn.cursor(), "sessions", sid)


def _session_row(sid):
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        return cur.fetchone()


def _waiting_query(observer, blocker_pid):
    with observer.cursor() as probe:
        probe.execute("SELECT pg_stat_clear_snapshot()")
        probe.execute("SELECT query FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))", (blocker_pid,))
        row = probe.fetchone()
    return row[0] if row else ""


def test_apply_arriving_while_the_owners_delete_is_open_cannot_recreate_the_chat(private_pool, monkeypatch):
    """#1308: the source's delayed commit record reaches the receiver while its delete is open.

    The apply holds network_state (N) and waits on the session row the open
    delete holds; once the delete commits it reads the owner's deletion fact,
    keeps the row deleted, and the tombstone outranks the incoming clock.
    Main recreated the chat here (its upsert ran after the delete committed).
    """
    from app.network import engine

    ctx, remote = _remote_network("open-delete-")
    monkeypatch.setattr(engine, "_context", ctx)
    sid = _owned_chat(ctx, remote, "open-delete-")
    env = _session_upsert(ctx, remote, sid, clock=50)
    barriers = _Barriers(monkeypatch, {"delete": (
        lambda sql, params: sql.startswith("DELETE FROM sessions WHERE id ="), False)})
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            barriers.wait("delete", deleting)  # the row is deleted, nothing committed
            applying = syncing.submit(engine.apply_batch, [env], ctx=ctx)
            _wait_for_lock_wait(observer, barriers.pids["delete"], applying)
            waited_on = _waiting_query(observer, barriers.pids["delete"])
        finally:
            barriers.release_all()
        assert deleting.result(timeout=10) is True
        applying.result(timeout=10)
    assert _session_row(sid) is None, "the delayed commit record recreated the deleted chat"
    record = _session_record(sid)
    assert record["deleted"] and record["origin_device_id"] == ctx.device_id
    assert record["clock"] > 50, "the owner's tombstone must outrank the record it refused"
    with base.get_db() as conn:
        assert not engine.store.session_deletion(conn.cursor(), sid)["pending"]
    assert waited_on.startswith("SELECT id FROM sessions WHERE id ="), waited_on  # the refusal's row lock


def test_apply_committed_first_then_the_owners_delete_outranks_it(private_pool, monkeypatch):
    """#1308: the commit record is applied first (holding the row); the delete waits, then wins."""
    from app.network import engine

    ctx, remote = _remote_network("apply-first-")
    monkeypatch.setattr(engine, "_context", ctx)
    sid = _owned_chat(ctx, remote, "apply-first-")
    env = _session_upsert(ctx, remote, sid, clock=50)
    barriers = _Barriers(monkeypatch, {"incoming": (
        lambda sql, params: sql.startswith("INSERT INTO sessions ("), False)})
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        applying = syncing.submit(engine.apply_batch, [env], ctx=ctx)
        try:
            barriers.wait("incoming", applying)  # the upsert holds the row, uncommitted
            deleting = deleter.submit(sessions.delete_session, sid)
            _wait_for_lock_wait(observer, barriers.pids["incoming"], deleting)
        finally:
            barriers.release_all()
        applying.result(timeout=10)
        assert deleting.result(timeout=10) is True
    assert _session_row(sid) is None
    record = _session_record(sid)
    assert record["deleted"] and record["origin_device_id"] == ctx.device_id
    assert record["clock"] > 50


def test_a_delete_waiting_on_its_chat_holds_no_network_state_lock(private_pool, monkeypatch):
    """#1308 review S6: while the owner's delete waits, N is free and other chats still apply."""
    from app.network import engine

    ctx, remote = _remote_network("waiting-delete-")
    monkeypatch.setattr(engine, "_context", ctx)
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid, row=None: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    sid = _owned_chat(ctx, remote, "waiting-delete-")
    other = "unrelated-" + uuid4().hex
    env = _session_upsert(ctx, remote, other, clock=60, owner=remote.device_id)
    with psycopg2.connect(**private_pool) as holder, psycopg2.connect(**private_pool) as observer, \
            psycopg2.connect(**private_pool) as probe, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming") as syncing:
        observer.autocommit = True
        held = holder.cursor()
        held.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (sid,))
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            _wait_for_lock_wait(observer, holder.get_backend_pid(), deleting)
            with probe.cursor() as cur:
                cur.execute("SET lock_timeout = '2s'")
                cur.execute("SELECT clock FROM network_state WHERE id = 1 FOR UPDATE")
                probe.rollback()
            applying = syncing.submit(engine.apply_batch, [env], ctx=ctx)
            applying.result(timeout=5)
            assert _session_row(other) is not None, "an unrelated chat must apply while the delete waits"
            assert not deleting.done()
        finally:
            holder.rollback()
        assert deleting.result(timeout=10) is True
    assert _session_row(sid) is None
