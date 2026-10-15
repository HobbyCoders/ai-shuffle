"""Plan guard for the task-card fence, on disposable real PostgreSQL only."""
import json
import time
import uuid

import pytest

from tests.db.postgres_harness import pg_schema  # noqa: F401

pytestmark = pytest.mark.integration


def test_task_incarnation_lookup_is_index_backed():
    from app.db import base, database, session_events

    sid = str(uuid.uuid4())
    index = "idx_session_events_task_incarnation"
    captured = {}

    class ObservedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            start = time.perf_counter()
            result = self.cursor.execute(sql, params)
            if sql.startswith("SELECT MAX(payload->>'task_incarnation')"):
                captured["query"] = (sql, params)
            if sql.startswith(f"CREATE INDEX IF NOT EXISTS {index} "):
                captured["index_build_ms"] = (time.perf_counter() - start) * 1000
            return result

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class ObservedConnection:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self):
            return ObservedCursor(self.conn.cursor())

    def explain(cursor):
        sql, params = captured["query"]
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        return cursor.fetchone()["QUERY PLAN"][0]

    def nodes(plan):
        yield plan
        for child in plan.get("Plans", []):
            yield from nodes(child)

    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT indexname FROM pg_indexes WHERE indexname=%s", (index,))
        assert cursor.fetchone(), "bootstrap must install the index"
        # A long session: 77,616 events, 1,340 task updates, three for task-1.
        # Synthetic payloads, never a copy of a user's session.
        cursor.execute(
            "INSERT INTO session_events(id, session_id, seq, event_type, payload) "
            "SELECT %s || '-' || i, %s, i, "
            "CASE WHEN i <= 1340 THEN 'task.updated' ELSE 'assistant.text_delta' END, "
            "CASE WHEN i <= 1340 THEN jsonb_build_object("
            "'task_id', 'task-' || (i %% 447), 'state', 'running', "
            "'task_incarnation', '2026-10-15T00:00:00.' || lpad(i::text, 6, '0') || '+00:00') "
            "ELSE jsonb_build_object('text', repeat('synthetic stream payload ', 40)) END "
            "FROM generate_series(1, 77616) AS i",
            (sid, sid),
        )
        cursor.execute("INSERT INTO session_event_counters VALUES (%s, 77616)", (sid,))
        # Capture the actual production SELECT, so a changed fence expression
        # cannot leave this plan guard testing a stale copy of the query.
        session_events.append_events(sid, [{"type": "task.updated", "payload": {
            "task_id": "task-1", "state": "completed",
            "task_incarnation": "2026-10-14T00:00:00.000001+00:00",
        }}], _conn=ObservedConnection(conn))
        cursor.execute("ANALYZE session_events")
        cursor.execute(f"DROP INDEX {index}")
        before = explain(cursor)
        # Exercise the real existing-database migration, timing just its index
        # DDL (one full scan on upgrade, not the unrelated migration work).
        database._run_column_migrations(ObservedCursor(cursor))
        after = explain(cursor)
        scans = [node for node in nodes(after["Plan"]) if node.get("Index Name") == index]
        assert scans, after
        assert all(node["Actual Rows"] <= 1 for node in scans), after
        assert not any(node.get("Relation Name") == "session_events"
                       and node["Node Type"] in ("Seq Scan", "Bitmap Heap Scan")
                       for node in nodes(after["Plan"])), after
        cursor.execute("SELECT pg_relation_size(%s) AS bytes", (index,))
        print(json.dumps({
            "events": 77616, "task_updates": 1340,
            "before_ms": before["Execution Time"],
            "after_ms": after["Execution Time"],
            "index_build_ms": captured["index_build_ms"],
            "index_bytes": cursor.fetchone()["bytes"],
            "after_plan": after["Plan"],
        }))
