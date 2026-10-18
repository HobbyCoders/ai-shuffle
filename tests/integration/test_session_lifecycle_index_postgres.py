"""Keep question projection lifecycle lookups bounded on long running turns."""
import json
import time
import uuid

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = [pytest.mark.integration, requires_real_pg]


def test_lifecycle_lookup_uses_partial_index_on_long_turn():
    from app.db import base, database, session_questions

    sid = str(uuid.uuid4())
    index = "idx_session_events_lifecycle"
    captured = {}

    class ObservedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            start = time.perf_counter()
            result = self.cursor.execute(sql, params)
            if sql.startswith("SELECT seq, event_type, payload"):
                captured["query"] = (sql, params)
            if sql.startswith(f"CREATE INDEX IF NOT EXISTS {index} "):
                captured["index_build_started"] = start
                captured["index_build_ms"] = (time.perf_counter() - start) * 1000
            return result

        def __getattr__(self, name):
            return getattr(self.cursor, name)

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
        assert cursor.fetchone(), "fresh schema must install the lifecycle index"
        # 25k events in one session, ending in a 5k-event running turn. The
        # other 1.475M events across many sessions also measure the one-time
        # migration's write-lock duration on a realistically large event table.
        other_events = 1_475_000
        cursor.execute(
            "INSERT INTO session_events(id, session_id, seq, event_type, payload) "
            "SELECT %s || '-' || i, %s, i, "
            "CASE WHEN i IN (1, 20001) THEN 'turn.started' "
            "WHEN i = 20000 THEN 'turn.completed' ELSE 'assistant.text_delta' END, "
            "jsonb_build_object('text', 'synthetic lifecycle fixture') "
            "FROM generate_series(1, 25000) AS i", (sid, sid),
        )
        cursor.execute(
            "INSERT INTO session_events(id, session_id, seq, event_type, payload) "
            "SELECT %s || '-other-' || i, %s || '-other-' || ((i-1)/2000), "
            "(i-1) %% 2000 + 1, "
            "CASE WHEN (i-1) %% 100 = 0 THEN 'turn.started' "
            "WHEN (i-1) %% 100 = 99 THEN 'turn.completed' ELSE 'assistant.text_delta' END, "
            "jsonb_build_object('text', 'synthetic unrelated fixture') "
            "FROM generate_series(1, %s) AS i", (sid, sid, other_events),
        )
        cursor.execute("ANALYZE session_events")
        # Capture the exact production SELECT, not a copy that can drift away
        # from the append path. Read only its newest surviving boundary.
        boundary = next(session_questions._tail(ObservedCursor(cursor), sid, lifecycle_only=True))
        assert boundary[0:2] == (20001, "turn.started")
        cursor.execute(f"DROP INDEX {index}")
        before = explain(cursor)
        # The same fingerprint-gated migration used on existing installs must
        # restore it; fresh-schema presence alone is insufficient coverage.
        migration_start = time.perf_counter()
        database._run_column_migrations(ObservedCursor(cursor))
        # A normal CREATE INDEX retains its write-blocking table lock until the
        # migration commits. Measure that whole window, not just its DDL time.
        conn.commit()
        committed = time.perf_counter()
        migration_ms = (committed - migration_start) * 1000
        write_lock_window_ms = (committed - captured["index_build_started"]) * 1000
        after = explain(cursor)
        scans = [node for node in nodes(after["Plan"]) if node.get("Index Name") == index]
        assert scans, after
        assert all(node["Actual Rows"] <= 1 for node in scans), after
        assert all(node.get("Rows Removed by Filter", 0) == 0 for node in scans), after
        assert not any(node.get("Relation Name") == "session_events"
                       and node["Node Type"] in ("Seq Scan", "Bitmap Heap Scan")
                       for node in nodes(after["Plan"])), after
        print(json.dumps({
            "kind": "lifecycle_plan", "session_events": 25000, "other_events": other_events,
            "total_events": 25000 + other_events, "sessions": (other_events + 1999) // 2000 + 1,
            "running_turn_events": 5000, "before_ms": before["Execution Time"],
            "after_ms": after["Execution Time"],
            "index_build_ms": captured["index_build_ms"],
            "migration_through_commit_ms": migration_ms,
            "index_start_through_commit_ms": write_lock_window_ms,
            "before_plan": before["Plan"], "after_plan": after["Plan"],
        }))
