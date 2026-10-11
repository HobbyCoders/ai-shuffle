"""Real PostgreSQL lock-order regression for task replay versus handoff.

Run explicitly with ALLOW_REAL_PG=1 and a disposable loopback TEST_POSTGRES_DSN.
The integration marker excludes this from shim-only runs; there are no skips.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import threading
import uuid

import pytest

from tests.db.postgres_harness import pg_schema  # noqa: F401

pytestmark = pytest.mark.integration


def test_repair_and_handoff_share_lock_order(monkeypatch):
    assert os.environ.get("ALLOW_REAL_PG") == "1"
    from app.db import background_tasks, base, runs, session_events, task_replay
    from app.network.handoff import _lease

    sid = str(uuid.uuid4())
    with base.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO sessions(id,instance_id,owner_device_id) VALUES (%s,%s,%s)",
            (sid, "local-install", "local-worker"),
        )
    start = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    session_events.append_events(sid, [{"type": "task.updated", "payload": {
        "task_id": "child", "state": "running", "started_at": start,
    }}])
    background_tasks.upsert_background_task({
        "session_id": sid, "task_id": "child", "kind": "agent", "status": "killed",
        "started_at": start, "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    run = runs.claim_run(sid, "handoff:other-worker", 120)
    assert run is not None
    reading_run = threading.Event()

    class ObservedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if "FROM runs" in sql and "FOR SHARE" in sql:
                reading_run.set()
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class ObservedConnection:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self):
            return ObservedCursor(self.conn.cursor())

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def repair_connection():
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL lock_timeout = '5s'")
            yield ObservedConnection(conn)

    monkeypatch.setattr(task_replay, "get_db", repair_connection)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SET LOCAL lock_timeout = '5s'")
            # The actual handoff lease holds the run row before updating the
            # session (app.network.handoff.commit's transaction order).
            _lease(cur, sid, "other-worker", run["id"])
            repair = executor.submit(
                task_replay.repair_task_projection, sid, "child",
                instance_id="local-install", device_id="local-worker",
            )
            assert reading_run.wait(timeout=5), "repair never reached the run lock"
            # Old repair held the session row while requesting the run lock:
            # this UPDATE and the repair SELECT deadlocked on real PostgreSQL.
            cur.execute(
                "UPDATE sessions SET owner_device_id=%s, instance_id=%s WHERE id=%s",
                ("other-worker", "other-install", sid),
            )
        assert repair.result(timeout=10) is False
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT owner_device_id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone()["owner_device_id"] == "other-worker"
    assert session_events.get_max_seq(sid) == 1
    assert session_events.get_latest_task_updates(sid)[0]["state"] == "running"
