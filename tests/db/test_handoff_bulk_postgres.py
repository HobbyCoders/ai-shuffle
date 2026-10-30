"""Issue #295: real-PostgreSQL bulk, equivalence and atomicity ratchets.

Counts, not a wall-clock assertion: the 65 MB measurement is reproducible without
making busy CI runners fail a timing threshold. The large fixture explicitly
prepares the experimental deferred-index schema; it does not activate it on a
worker or claim that the ordinary GIN path is fast. Never uses an installed DB.
"""
import json
import logging
from collections import Counter
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from psycopg2.extras import RealDictCursor

from app.db import base, database, history_indexing, network, session_recall
from app.network import handoff
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401
from tests.db.test_handoff_import_postgres import snapshot

pytestmark = requires_real_pg


@pytest.fixture(scope="module", autouse=True)
def indexed_schema(request):
    request.getfixturevalue("pg_schema")
    database.init_database()
    session_recall.install_triggers()
    for _ in range(20):
        if history_indexing.migrate():
            break
    else:
        pytest.fail("history-index migration did not finish")


@pytest.fixture
def destination(tmp_path):
    sid, cwd = "bulk-ratchet", str(tmp_path)
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Bulk lab','source')", (sid,))
    yield sid, cwd
    with base.get_db() as conn:
        cur = conn.cursor()
        # Includes superseded native trees, not just the current SDK pointer.
        cur.execute("SELECT session_id FROM fluid_sessions WHERE project_root=%s", (cwd,))
        sdks = [row["session_id"] for row in cur.fetchall()]
        for sdk in sdks:
            for table in ("fluid_session_events", "fluid_session_nodes", "session_store_entries", "fluid_sessions"):
                cur.execute(f"DELETE FROM {table} WHERE session_id=%s", (sdk,))
        for table in handoff.APP_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM session_event_counters WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM sessions WHERE id=%s", (sid,))


@contextmanager
def statements():
    """Count actual DB calls, including staging, publication and deferral SQL."""
    calls = []

    class Cursor(RealDictCursor):
        def execute(self, sql, params=None):
            calls.append(sql)
            return super().execute(sql, params)

    original = base._PgDictConnection.cursor

    def cursor(conn, *args, **kwargs):
        return original(conn, *args, cursor_factory=Cursor, **kwargs)

    with patch.object(base._PgDictConnection, "cursor", cursor):
        yield calls


@pytest.mark.parametrize("mirrored", [False, True])
def test_issue_295_bundle_has_bounded_round_trips(destination, mirrored):
    sid, cwd = destination
    data = snapshot(events=7823, nodes=1741, transcript=10768, messages=41, width=3100)
    assert sum(map(len, data["history"].values())) == 20374
    assert len(json.dumps(data).encode()) >= 64_000_000
    if mirrored:
        handoff.install_history(sid, data, cwd)
    with statements() as calls:
        sdk = handoff.install_history(sid, data, cwd)
    # The ceiling covers the WHOLE producing path, not just its 200-row loop.
    # Ratchet down if SQL is removed; do not raise it to accommodate per-row work.
    assert len(calls) <= 189
    inserts = Counter(sql.split()[2] for sql in calls if sql.startswith("INSERT INTO "))
    assert inserts["session_events"] == 40
    assert inserts["fluid_session_events"] == 54
    assert inserts["fluid_session_nodes"] == 9
    assert inserts["session_messages"] <= 1
    with base.get_db() as conn:
        cur = conn.cursor()
        for table, count, key in (("session_events", 7823, sid), ("fluid_session_events", 10768, sdk),
                                  ("fluid_session_nodes", 1741, sdk), ("session_messages", 41, sid)):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE session_id=%s", (key,))
            assert cur.fetchone()["n"] == count


def portable_bundle():
    data = snapshot(events=401, nodes=201, transcript=401, messages=5, width=40)
    data["history"]["session_events"][0]["id"] = "replacement-event"
    data["history"]["session_events"][-1]["id"] = "lab-event-0"
    data["history"]["session_events"][1]["payload"] = '{"text":"café — \u2603", "nested":[null, true, 1.25]}'
    data["history"]["session_messages"][0].update(
        content="quotes \" / backslash \\ / newline\n雪", tool_input={"nested": [None, False]}, metadata=None)
    data["history"]["fluid_session_events"][0]["event"] = '{"type":"text", "text":"🌱"}'
    data["history"]["session_store_entries"] = [{
        "project_key": "source-project", "subpath": "history.jsonl",
        "entry": '{"sessionId":"source-sdk", "cwd":"/source", "data":[null, false, "雪"]}',
        "created_at": "2026-10-19T01:00:00+01:00",
    }]
    data["history"]["pending_interactions"] = [{
        "id": "portable-receipt", "kind": "model_switch_named", "state": "answered",
        "payload": {"model": "test-model"}, "answer": {"approved": True},
        "created_at": "2026-10-19T00:00:00Z", "answered_at": "2026-10-19T00:00:01Z",
    }, {
        "id": "not-portable", "kind": "permission", "state": "pending", "payload": {},
    }]
    data["history"]["turns"] = [{
        "id": "portable-launch", "source": "agent_message", "prompt": "start 雪",
        "payload": {"orchestration_sender": "origin"}, "command_id": "orchestration:start:lab",
        "position": 1, "state": "queued", "created_at": "2026-10-19T00:00:00Z",
    }]
    return data


def installed_bytes(sid, sdk):
    """Canonical PG row bytes, excluding generated/local-only identities.

    JSONB already discards input whitespace/key order on BOTH paths. Compare
    its PostgreSQL text bytes, not a fixture's re-serialization of the input.
    Only the deliberately fresh SDK id inside a portable entry is normalized.
    """
    result = {}
    with base.get_db() as conn:
        cur = conn.cursor()
        for table, columns in handoff.TABLES.items():
            key = sid if table in handoff.APP_TABLES else sdk
            cur.execute(f"SELECT jsonb_build_array({', '.join(columns)})::text AS bytes "
                        f"FROM {table} WHERE session_id=%s", (key,))
            result[table] = sorted(row["bytes"].replace(sdk, "<native-sdk>").encode()
                                   for row in cur.fetchall())
    return result


@pytest.mark.parametrize("deferred", [False, True])
def test_bulk_rows_are_byte_identical_to_rowwise_with_rewinds_and_retries(destination, monkeypatch, deferred):
    sid, cwd = destination
    if not deferred:
        monkeypatch.setattr(history_indexing, "enabled", lambda cur: False)

    def rowwise(cur, table, columns, values, conflict="", *, batch=0):
        # Independent reference: one bound INSERT per row, never _insert_rows.
        sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES (" +
               ", ".join(["%s"] * len(columns)) + ") " + conflict)
        for row in values:
            cur.execute(sql, row)

    outputs = []
    for reference in (True, False):
        with monkeypatch.context() as scoped:
            if reference:
                scoped.setattr(network, "_insert_rows", rowwise)
            handoff.install_history(sid, snapshot(events=402, messages=6), cwd)
            sdk = handoff.install_history(sid, portable_bundle(), cwd)
            first = installed_bytes(sid, sdk)
            sdk = handoff.install_history(sid, portable_bundle(), cwd)
            assert installed_bytes(sid, sdk) == first  # Idempotent data, fresh SDK.
            outputs.append(first)
        with base.get_db() as conn:
            cur = conn.cursor()
            for table in handoff.APP_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE session_id=%s", (sid,))
    assert outputs[0] == outputs[1]
    assert len(outputs[1]["session_events"]) == 401
    assert len(outputs[1]["pending_interactions"]) == 1
    assert b'"completed"' in outputs[1]["turns"][0]


@pytest.mark.parametrize("fail_table", ["fluid_session_events", "session_events"])
def test_failure_rolls_back_publication_and_removes_committed_staging(destination, monkeypatch, fail_table):
    sid, cwd = destination
    sdk = handoff.install_history(sid, snapshot(), cwd)
    before = installed_bytes(sid, sdk)
    original = network._insert_rows

    def fail(cur, table, *args, **kwargs):
        result = original(cur, table, *args, **kwargs)
        if table == fail_table:
            raise RuntimeError("failure after actual INSERT")
        return result

    monkeypatch.setattr(network, "_insert_rows", fail)
    with pytest.raises(RuntimeError, match="after actual INSERT"):
        handoff.install_history(sid, portable_bundle(), cwd)
    assert installed_bytes(sid, sdk) == before
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (sid,))
        assert dict(cur.fetchone()) == {"sdk_session_id": sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM fluid_sessions WHERE project_root=%s", (cwd,))
        assert cur.fetchone()["n"] == 1


def test_publication_reports_real_statement_costs_without_payload(destination, caplog):
    sid, cwd = destination
    data = snapshot(events=201, nodes=0, transcript=0)
    data["history"]["session_events"][0]["payload"] = {"text": "private-transcript-canary"}
    with caplog.at_level(logging.INFO, logger=handoff.__name__):
        handoff.install_history(sid, data, cwd)
    messages = [r.getMessage() for r in caplog.records if "publish_history.session_events:" in r.getMessage()]
    assert len(messages) == 1
    assert "rows=201 candidates=201 statements=6" in messages[0]
    assert "execute=" in messages[0] and "max_statement=" in messages[0]
    assert "private-transcript-canary" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=handoff.__name__):
        handoff.install_history(sid, snapshot(events=0, nodes=0, transcript=0), cwd)
    messages = [r.getMessage() for r in caplog.records if "publish_history.session_events:" in r.getMessage()]
    assert len(messages) == 1
    # Zero incoming rows still required a scan and two bounded stale deletes.
    assert "rows=0 candidates=0 statements=6" in messages[0]
