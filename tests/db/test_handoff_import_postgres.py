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




def test_divergent_compaction_prunes_stale_ids_before_upsert(chat):
    data = snapshot()
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    survivor = dict(data["history"]["session_events"][-1], id=data["history"]["session_events"][0]["id"])
    data["history"]["session_events"] = [survivor]
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,seq FROM session_events WHERE session_id=%s", (chat,))
        assert [dict(row) for row in cur.fetchall()] == [{"id": survivor["id"], "seq": survivor["seq"]}]


def test_legacy_message_reconciliation_is_batched_and_retains_unchanged_versions(chat, monkeypatch):
    from app.db import network

    data = snapshot(messages=601)
    batches = []
    insert = network._insert_rows

    def record(cur, table, columns, rows, *args, **kwargs):
        if table == "session_messages":
            batches.append(len(rows))
        return insert(cur, table, columns, rows, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", record)
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert batches == [601]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,xmin::text AS version,jsonl_uuid FROM session_messages WHERE session_id=%s", (chat,))
        before = {r["jsonl_uuid"]: (r["id"], r["version"]) for r in cur.fetchall()}
    data["history"]["session_messages"][0]["content"] = "edited message"
    data["history"]["session_messages"].pop()
    batches.clear()
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert batches == [1]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,xmin::text AS version,jsonl_uuid,content FROM session_messages WHERE session_id=%s", (chat,))
        rows = cur.fetchall()
        assert len(rows) == 600
        assert next(r for r in rows if r["jsonl_uuid"] == "lab-message-0")["content"] == "edited message"
        assert all((r["id"], r["version"]) == before[r["jsonl_uuid"]] for r in rows if r["jsonl_uuid"] != "lab-message-0")




def test_receipt_rewind_removes_only_portable_rows(chat):
    with base.get_db() as conn:
        cur = conn.cursor()
        for receipt in ("retained", "rewound"):
            cur.execute("INSERT INTO pending_interactions (id,session_id,kind,state) "
                        "VALUES (%s,%s,'model_switch_approval','answered')", (receipt, chat))
        cur.execute("INSERT INTO pending_interactions (id,session_id,kind,state) "
                    "VALUES ('local-permission',%s,'permission','pending')", (chat,))
    data = {"sdk_session_id": None, "history": handoff._history(database.get_session(chat))}
    data["history"]["pending_interactions"] = [r for r in data["history"]["pending_interactions"] if r["id"] == "retained"]
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM pending_interactions WHERE session_id=%s ORDER BY id", (chat,))
        assert [r["id"] for r in cur.fetchall()] == ["local-permission", "retained"]
        cur.execute("DELETE FROM pending_interactions WHERE session_id=%s", (chat,))


def snapshot(events=10, nodes=3, transcript=12, messages=0, width=100):
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
        "session_messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": text(),
                              "metadata": {"lab": i}, "jsonl_uuid": f"lab-message-{i}",
                              "created_at": "2026-10-19T00:00:00Z"} for i in range(messages)],
    }}


@pytest.fixture(scope="module", autouse=True)
def upgraded_schema(pg_schema):
    # Reused disposable databases exercise the same additive column upgrades
    # as an installed worker, not just CREATE TABLE IF NOT EXISTS.
    database.init_database()


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

    data = snapshot(events=7823, nodes=1741, transcript=10768, messages=4000, width=3100)
    payload_bytes = len(json.dumps(data))
    timing = handoff._Timing("lab", chat)
    data["_timing"] = timing
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
        if os.environ.get("HANDOFF_LAB_COLD") != "1":
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
            "bytes": payload_bytes, "seconds": elapsed, "wal_bytes": wal,
            "cold_receiver": os.environ.get("HANDOFF_LAB_COLD") == "1",
            "tables_seconds": measured, "publish_metrics": timing.steps,
            "import_metrics": data.get("_history_metrics"),
            "duplicate_node_index": os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") != "1"}, sort_keys=True))
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            create_session_recall_text(conn.cursor())
