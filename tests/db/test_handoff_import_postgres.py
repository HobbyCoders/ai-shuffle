"""Atomic hand-off import and opt-in 20k-row benchmark on disposable PostgreSQL."""
import json
import os
import random
import time
from unittest.mock import patch

import pytest

from app.db import base, database
from app.network import handoff
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_incremental_events_skip_unchanged_and_update_changed(chat):
    data = snapshot()
    first_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT seq,xmin::text AS version FROM session_events WHERE session_id=%s ORDER BY seq", (chat,))
        before = {r["seq"]: r["version"] for r in cur.fetchall()}
    data["history"]["session_events"][0]["payload"] = {"text": "changed"}
    second_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert first_sdk != second_sdk  # Never reuse a provider's warm native cache.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT seq,payload,xmin::text AS version FROM session_events WHERE session_id=%s ORDER BY seq", (chat,))
        rows = cur.fetchall()
        assert len(rows) == 10
        assert rows[0]["payload"] == {"text": "changed"}
        assert rows[0]["version"] != before[1]
        assert all(r["version"] == before[r["seq"]] for r in rows[1:])


def test_staged_chunks_leave_old_mirror_and_owner_until_final_commit(chat):
    old = snapshot()
    sdk = handoff.install_history(chat, old, "/tmp/handoff-lab")
    data = snapshot(events=15)
    data["token"] = "new-lease"
    data["_prepare_history"] = True
    new_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 10
    handoff._accept_owner(chat, "target", token="new-lease")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": new_sdk, "owner_device_id": "target"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 15


def test_aborted_chunk_import_keeps_previous_mirror(chat, monkeypatch):
    from app.db import network

    old = snapshot()
    sdk = handoff.install_history(chat, old, "/tmp/handoff-lab")
    data = snapshot(nodes=250)
    data["token"] = "aborted-lease"
    data["_prepare_history"] = True
    original = network._insert_rows

    def fail(cur, table, *args, **kwargs):
        if table == "fluid_session_events":
            raise RuntimeError("injected failure after committed node chunks")
        return original(cur, table, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 10
        cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE token='aborted-lease'")
        assert cur.fetchone()["n"] == 0


def snapshot(events=10, nodes=3, transcript=12, width=100):
    rng = random.Random(295)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 "
    text = lambda: "".join(rng.choices(alphabet, k=width))
    return {"token": "lab-token", "sdk_session_id": "ses_lab-source", "history": {
        "session_events": [{"id": f"lab-event-{i}", "seq": i + 1,
            "event_type": "message.assistant", "payload": {"text": text()},
            "created_at": "2026-10-19T00:00:00Z"} for i in range(events)],
        "fluid_sessions": [{"head_node_id": f"n{nodes-1}", "created_at": 1.0}],
        "fluid_session_nodes": [{"node_id": f"n{i}", "parent_id": f"n{i-1}" if i else None,
            "ts": float(i), "message": {"role": "assistant", "content": text()}} for i in range(nodes)],
        "fluid_session_events": [{"seq": i, "event": {"type": "text", "text": text()}}
                                 for i in range(transcript)],
    }}


@pytest.fixture
def chat():
    sid = "handoff-lab"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_events WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM session_event_counters WHERE session_id=%s", (sid,))
        cur.execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Import lab','source')", (sid,))
    yield sid
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id FROM sessions WHERE id=%s", (sid,))
        sdk = cur.fetchone()["sdk_session_id"]
        cur.execute("DELETE FROM sessions WHERE id=%s", (sid,))
        cur.execute("DELETE FROM session_events WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM session_event_counters WHERE session_id=%s", (sid,))
        for table in ("fluid_sessions", "fluid_session_nodes", "fluid_session_events", "session_store_entries"):
            cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s,%s)", (sdk, "ses_lab-source"))


@pytest.mark.skipif(os.environ.get("HANDOFF_LAB") != "1", reason="opt-in 20k-row measurement")
def test_import_lab(chat, capsys):
    from app.db import network, session_recall
    from app.db.schema_postgres import create_session_recall_text

    data = snapshot(events=7823, nodes=1741, transcript=10768, width=3100)
    with base.get_db() as conn:
        create_session_recall_text(conn.cursor())
        session_recall.install_objects(conn.cursor())
    session_recall.install_triggers()
    # Reproduce a receiver that already mirrors the owner's event stream.
    from psycopg2.extras import Json
    with base.get_db() as conn:
        columns = ("session_id", *handoff.TABLES["session_events"])
        rows = [(chat, *[Json(row.get(c)) if c in handoff.JSON_COLUMNS and row.get(c) is not None
                         else row.get(c) for c in handoff.TABLES["session_events"]])
                for row in data["history"]["session_events"]]
        network._insert_rows(conn.cursor(), "session_events", columns, rows,
                             "ON CONFLICT DO NOTHING", batch=200)
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            conn.cursor().execute("DROP INDEX idx_fluid_recall_text_trgm")
    measured = {}
    original = network._insert_rows

    def insert(cur, table, *args, **kwargs):
        start = time.monotonic()
        result = original(cur, table, *args, **kwargs)
        measured[table] = measured.get(table, 0) + time.monotonic() - start
        return result

    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_current_wal_insert_lsn() AS lsn")
        lsn = cur.fetchone()["lsn"]
    start = time.monotonic()
    with patch.object(network, "_insert_rows", insert):
        handoff.install_history(chat, data, "/tmp/handoff-lab")
    elapsed = time.monotonic() - start
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s) AS bytes", (lsn,))
        wal = int(cur.fetchone()["bytes"])
    with capsys.disabled():
        print("HANDOFF_LAB " + json.dumps({"rows": sum(len(v) for v in data["history"].values()),
            "bytes": len(json.dumps(data)), "seconds": elapsed, "wal_bytes": wal,
            "tables_seconds": measured, "import_metrics": data.get("_history_metrics"),
            "duplicate_node_index": os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") != "1"}, sort_keys=True))
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            create_session_recall_text(conn.cursor())
