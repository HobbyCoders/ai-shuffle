"""Real row-lock serialization, idempotent races and atomic event rollback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
from uuid import uuid4

import pytest
from app.db import base, runs, session_plans, session_events
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_concurrent_retry_rollback_and_handoff_fence(monkeypatch):
    sid = "plan-pg-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Plan concurrency','source')", (sid,))
    run = runs.claim_run(sid, "runner", 120)
    proof = (sid, run["id"], run["fence_token"])
    args = {"action": "update", "request_id": "retry", "expected_revision": 0,
            "goal": "Atomic", "done_when": "One committed revision", "tasks": [
                {"id": "one", "title": "Write", "status": "in_progress"}]}
    def execute(value):
        return session_plans.execute(sid, value, device_id="source", runner_id="runner", proof=proof)
    gate = Barrier(2)
    def retry():
        gate.wait(timeout=5)
        return execute(args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(retry), pool.submit(retry)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]
    assert sorted(x.get("replayed", False) for x in outcomes) == [False, True]
    assert len([e for e in session_events.get_events(sid) if e["event_type"] == "plan.updated"]) == 1
    def fail(*a, **k):
        raise ValueError("injected event rollback")
    with monkeypatch.context() as m:
        m.setattr(session_events, "append_events", fail)
        with pytest.raises(ValueError, match="injected event rollback"):
            execute({**args, "request_id": "failed", "expected_revision": 1})
    assert session_plans.read(sid)["plan"]["revision"] == 1
    # Ownership publication holds the same row lock the writer needs. Even a
    # still-active old run is rejected after the new owner commits.
    started = Event()
    def stale():
        started.set()
        return execute({**args, "request_id": "stale", "expected_revision": 1})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (sid,))
            pending = pool.submit(stale)
            assert started.wait(5)
            cur.execute("UPDATE sessions SET owner_device_id='target' WHERE id=%s", (sid,))
        with pytest.raises(ValueError, match="another worker"):
            pending.result(timeout=10)
    assert session_plans.read(sid)["plan"]["revision"] == 1


def test_plan_and_normal_event_writer_use_compatible_lock_order(monkeypatch):
    sid = "plan-event-race-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Event concurrency','source')", (sid,))
    run = runs.claim_run(sid, "runner", 120)
    proof = (sid, run["id"], run["fence_token"])
    # Create the counter through the real event producer first. The race is
    # an ordinary counter UPDATE versus a plan mutation, not FK insertion.
    session_events.append_events(sid, [{"type": "system.note", "payload": {"subtype": "before-race"}}])
    counter_held, plan_counter_attempt = Event(), Event()

    class Cursor:
        def __init__(self, cursor, planner):
            self.cursor, self.planner = cursor, planner
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, sql, args=None):
            counter = sql.startswith("INSERT INTO session_event_counters")
            if counter and self.planner:
                plan_counter_attempt.set()
            result = self.cursor.execute(sql, args)
            if counter and not self.planner:
                counter_held.set()
                assert plan_counter_attempt.wait(5), "Plan never attempted its event counter"
            return result

    class Connection:
        def __init__(self, connection, planner):
            self._connection, self.planner = connection, planner
        def __getattr__(self, name):
            return getattr(self._connection, name)
        def cursor(self):
            return Cursor(self._connection.cursor(), self.planner)

    @contextmanager
    def planner_db():
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL lock_timeout='4s'")
            conn.cursor().execute("SET LOCAL statement_timeout='6s'")
            yield Connection(conn, True)
    monkeypatch.setattr(session_plans, "get_db", planner_db)

    def event_writer():
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL lock_timeout='4s'")
            conn.cursor().execute("SET LOCAL statement_timeout='6s'")
            session_events.append_events(sid, [{"type": "system.note", "payload": {"subtype": "concurrent-event"}}],
                                         run_id=proof[1], fence_token=proof[2], _conn=Connection(conn, False))
    args = {"action": "update", "request_id": "concurrent-plan", "expected_revision": 0,
            "goal": "No writer deadlock", "done_when": "Both producers commit", "tasks": [
                {"id": "one", "title": "Write", "status": "in_progress"}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        event = pool.submit(event_writer)
        assert counter_held.wait(5)
        plan = pool.submit(session_plans.execute, sid, args, device_id="source", runner_id="runner", proof=proof)
        event.result(timeout=10)
        assert plan.result(timeout=10)["revision"] == 1
    assert session_plans.read(sid)["plan"]["revision"] == 1
    types = [event["event_type"] for event in session_events.get_events(sid)]
    assert types == ["system.note", "system.note", "plan.updated"]
