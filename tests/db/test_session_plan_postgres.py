"""Real row-lock serialization, idempotent races and atomic event rollback."""
from concurrent.futures import ThreadPoolExecutor
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
