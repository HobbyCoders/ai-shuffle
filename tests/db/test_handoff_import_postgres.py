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




def test_receipt_capable_staging_survives_lease_then_requires_explicit_receipt(chat, monkeypatch):
    from types import SimpleNamespace
    from psycopg2.extras import Json

    ctx = SimpleNamespace(device_id="target")
    monkeypatch.setattr(handoff.engine, "context", lambda: ctx)
    monkeypatch.setattr(handoff.engine, "require_context", lambda: ctx)
    data = snapshot()
    data["_prepare_history"] = True
    sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source")
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE handoff_history_chunks SET created_at=%s WHERE session_id=%s",
                              ("2000-01-01T00:00:00+00:00", chat))
    assert handoff.recover_history_staging() == 0
    assert handoff.admission_held(chat)
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id='target',placement=%s WHERE id=%s",
                              (Json({"where": "runner:target"}), chat))
    assert handoff.recover_history_staging() == 0  # Missing token is not a receipt.
    assert database.get_session(chat)["sdk_session_id"] is None
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET placement=%s WHERE id=%s",
                              (Json({"where": "runner:target", "handoff": {"token": data["token"]}}), chat))
    assert handoff.recover_history_staging() == 1
    assert database.get_session(chat)["sdk_session_id"] == sdk
    assert not handoff.admission_held(chat)


def test_legacy_receipt_recovers_latest_context_and_retires_older_staging(chat, monkeypatch):
    from types import SimpleNamespace
    from psycopg2.extras import Json

    ctx = SimpleNamespace(device_id="target")
    monkeypatch.setattr(handoff.engine, "context", lambda: ctx)
    monkeypatch.setattr(handoff.engine, "require_context", lambda: ctx)
    data = snapshot()
    data.update(token="older-legacy", _prepare_history=True)
    old_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=False)
    data["token"] = "newer-legacy"
    sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=False)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET owner_device_id='target',placement=%s WHERE id=%s",
                    (Json({"where": "runner:target"}), chat))
    assert handoff.recover_history_staging() == 1
    assert database.get_session(chat)["sdk_session_id"] == sdk
    assert not handoff.admission_held(chat)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (old_sdk,))
        assert cur.fetchone()["n"] == 0


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


@pytest.mark.parametrize("publisher", ["accept", "finalize"])
@pytest.mark.parametrize("legacy_source", [False, True])
@pytest.mark.parametrize("next_owner", ["target", "newer-owner"])
def test_slow_publish_does_not_hold_session_row_and_revalidates_owner(chat, monkeypatch, next_owner, publisher, legacy_source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.db import network

    previous_sdk = handoff.install_history(chat, snapshot(), "/tmp/handoff-lab")
    data = snapshot(events=15)
    data.update(token="slow-publication", _prepare_history=True)
    new_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=not legacy_source)
    started, release = Event(), Event()
    insert = network._insert_rows

    def pause(cur, table, *args, **kwargs):
        if table == "session_events":
            started.set()
            assert release.wait(10), "publication was not released"
        return insert(cur, table, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", pause)
    if publisher == "finalize":
        from types import SimpleNamespace
        monkeypatch.setattr(handoff.engine, "require_context", lambda: SimpleNamespace(device_id="target"))
        handoff._stage_wakes(chat, "source", data, None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        if publisher == "accept":
            future = executor.submit(handoff._accept_owner, chat, "target", token=data["token"])
        else:
            future = executor.submit(handoff._finalize_wakes, chat, "source", {"token": data["token"]})
        try:
            assert started.wait(10), "publication did not start"
            with base.get_db() as conn:
                cur = conn.cursor()
                cur.execute("SET LOCAL lock_timeout = '250ms'")
                # A real ownership sync can arrive while the cold delta is written.
                cur.execute("UPDATE sessions SET owner_device_id=%s,placement=%s,title='Concurrent write' WHERE id=%s",
                            (next_owner, json.dumps({} if legacy_source else {"handoff": {"token": data["token"]}}), chat))
        finally:
            release.set()
        if next_owner == "target":
            future.result(timeout=10)
        else:
            with pytest.raises(ValueError, match="ownership changed"):
                future.result(timeout=10)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id,title FROM sessions WHERE id=%s", (chat,))
        row = cur.fetchone()
        assert row["owner_device_id"] == next_owner
        assert row["title"] == "Concurrent write"
        assert row["sdk_session_id"] == (new_sdk if next_owner == "target" else previous_sdk)
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == (15 if next_owner == "target" else 10)


def snapshot(events=10, nodes=3, transcript=12, messages=0, width=100):
    from fluid.messages import Message, Text

    rng = random.Random(295)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 "
    text = lambda: "".join(rng.choices(alphabet, k=width))
    return {"token": "lab-token", "sdk_session_id": "ses_lab-source", "history": {
        "session_events": [{"id": f"lab-event-{i}", "seq": i + 1,
            "event_type": "message.assistant", "payload": {"content": text(), "kind": "text"},
            "created_at": "2026-10-19T00:00:00Z"} for i in range(events)],
        "fluid_sessions": [{"head_node_id": f"n{nodes-1}", "created_at": 1.0}],
        "fluid_session_nodes": [{"node_id": f"n{i}", "parent_id": f"n{i-1}" if i else None,
            "ts": float(i), "message": Message("assistant", [Text(text())]).to_dict()} for i in range(nodes)],
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


@pytest.fixture
def recall_projection():
    from app.db import session_recall

    with base.get_db() as conn:
        session_recall.install_objects(conn.cursor())
    assert session_recall.install_triggers()
    # Establish readiness BEFORE producing the import, not by backfilling its
    # rows afterwards. Both legacy and projection-based SessionSearch are tested.
    for source in session_recall.SOURCES:
        while True:
            with base.get_db() as conn:
                if session_recall.backfill_step(conn.cursor(), source):
                    break


@pytest.mark.parametrize("cached", [False, True])
def test_import_drains_every_gin_and_searches_immediately_after_accept(chat, cached, monkeypatch, recall_projection):
    from fluid.messages import Message, Text
    from app.core.provider import fluid_pg_store
    from app.db import network, session_recall, sessions
    from app.db.schema_postgres import create_session_recall_text

    with base.get_db() as conn:
        cur = conn.cursor()
        if cached:
            create_session_recall_text(cur)
        cur.execute("SELECT gin_clean_pending_list(c.oid) FROM pg_class c "
                    "JOIN pg_am am ON am.oid=c.relam WHERE am.amname='gin'")
        cur.execute("SELECT current_setting('gin_pending_list_limit') AS value")
        default = cur.fetchone()["value"]
    observed = set()
    original = network._insert_rows

    def insert(cur, table, *args, **kwargs):
        if table in handoff.TABLES:
            cur.execute("SELECT pg_size_bytes(current_setting('gin_pending_list_limit')) AS bytes")
            # Tables without GIN indexes need no tuning.
            if table in {"fluid_session_nodes", "session_store_entries", "session_events", "session_messages"}:
                assert cur.fetchone()["bytes"] == handoff._HISTORY_GIN_LIMIT_KB * 1024
                observed.add(table)
        return original(cur, table, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", insert)
    data = snapshot(nodes=201, messages=3)
    needles = ("nativehandoffneedle", "delegatedhandoffneedle", "eventhandoffneedle", "legacyhandoffneedle")
    data["history"]["fluid_session_nodes"][0]["message"] = Message("user", [Text(needles[0])]).to_dict()
    data["history"]["session_store_entries"] = [{"project_key": "lab", "subpath": None,
        "entry": {"type": "user", "message": {"content": needles[1]}},
        "created_at": "2026-10-19T00:00:00Z"}]
    data["history"]["session_events"][0]["payload"] = {"content": needles[2], "kind": "text"}
    data["history"]["session_messages"][0]["content"] = needles[3]
    data["_prepare_history"] = True
    sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        # Native chunks already committed, while their SDK is still private.
        assert not any(gin_pending_pages(conn.cursor()).values())
        assert database.get_session(chat)["sdk_session_id"] is None
    handoff._accept_owner(chat, "target", token=data["token"])
    with base.get_db() as conn:
        assert not any(gin_pending_pages(conn.cursor()).values())
    assert observed == {"fluid_session_nodes", "session_store_entries", "session_events", "session_messages"}
    # A NEW connection (not just another lease from the pool) sees the default.
    import psycopg2
    with psycopg2.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW gin_pending_list_limit")
            assert cur.fetchone()[0] == default
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SHOW gin_pending_list_limit")
        assert cur.fetchone()["gin_pending_list_limit"] == default
    # UI search reads native/delegated/legacy text; mirrored application events
    # are instead searched by SessionSearch's remote-session arm.
    for needle in (needles[0], needles[1], needles[3]):
        assert chat in {row["id"] for row in sessions.search_sessions(needle)}, needle
        rows, total = sessions.search_sessions_paginated(needle)
        assert total >= 1 and chat in {row["id"] for row in rows}
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "search-reader")
    with base.get_db() as conn:
        assert session_recall.ready(conn.cursor())
    for indexed in (False, True):
        with patch.object(session_recall, "ready", return_value=indexed):
            # A locally accepted SDK is searched through its native/delegated
            # transcript, not through the remote-only mirrored event arm.
            for needle in needles[:2]:
                hits = fluid_pg_store._search_sync([needle], 5, None)
                assert any(hit.session_id in {chat, sdk} for hit in hits), (needle, indexed, hits)


def test_event_only_import_is_immediately_searchable_by_session_search(chat, monkeypatch, recall_projection):
    from app.core.provider import fluid_pg_store
    from app.db import session_recall

    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "search-reader")
    needle = "mirroredhandoffneedle"
    data = {"token": "event-only", "sdk_session_id": None, "_prepare_history": True,
            "history": {"session_events": [{"id": "event-only", "seq": 1,
                "event_type": "message.user", "payload": {"content": needle},
                "created_at": "2026-10-19T00:00:00Z"}]}}
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._accept_owner(chat, "target", token=data["token"])
    with base.get_db() as conn:
        assert session_recall.ready(conn.cursor())
        assert not any(gin_pending_pages(conn.cursor()).values())
    for indexed in (False, True):
        with patch.object(session_recall, "ready", return_value=indexed):
            assert chat in {hit.session_id for hit in fluid_pg_store._search_sync([needle], 5, None)}


def test_cleanup_failure_rolls_back_publication_and_can_retry(chat, monkeypatch):
    import psycopg2
    from psycopg2.extras import RealDictCursor

    old_sdk = handoff.install_history(chat, snapshot(), "/tmp/handoff-lab")
    data = snapshot(events=15)
    data.update(token="cleanup-retry", _prepare_history=True)
    new_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    original = handoff._history_gin

    class FailedCleanupCursor(RealDictCursor):
        def execute(self, sql, params=None):
            if sql.startswith("SELECT gin_clean_pending_list("):
                return super().execute("SELECT 1/0")
            return super().execute(sql, params)

    def failed_cleanup(cur, tables):
        return original(cur.connection.cursor(cursor_factory=FailedCleanupCursor), tables)

    with monkeypatch.context() as injected:
        injected.setattr(handoff, "_history_gin", failed_cleanup)
        with pytest.raises(psycopg2.errors.DivisionByZero):
            handoff._accept_owner(chat, "target", token=data["token"])
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": old_sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 10
    handoff._accept_owner(chat, "target", token=data["token"])
    assert database.get_session(chat)["sdk_session_id"] == new_sdk
    with base.get_db() as conn:
        assert not any(gin_pending_pages(conn.cursor()).values())


def test_gin_cleanup_exempts_only_the_drain_from_statement_timeout():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    class SlowCleanupCursor(RealDictCursor):
        def execute(self, sql, params=None):
            if sql.startswith("SELECT gin_clean_pending_list("):
                super().execute("SELECT pg_sleep(0.05)")
            return super().execute(sql, params)

    with base.get_db() as conn:
        cur = conn.cursor(cursor_factory=SlowCleanupCursor)
        cur.execute("SET LOCAL statement_timeout = '20ms'")
        cur.execute("SHOW gin_pending_list_limit")
        default = cur.fetchone()["gin_pending_list_limit"]
        with handoff._history_gin(cur, ("session_messages",)):
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()["statement_timeout"] == "20ms"
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()["statement_timeout"] == "20ms"
        cur.execute("SHOW gin_pending_list_limit")
        assert cur.fetchone()["gin_pending_list_limit"] == default
        with pytest.raises(psycopg2.errors.QueryCanceled):
            cur.execute("SELECT pg_sleep(0.05)")
        conn.rollback()


def gin_pending_pages(cur):
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
    cur.execute("SELECT c.relname, (pgstatginindex(c.oid)).pending_pages AS pages "
                "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_am am ON am.oid=c.relam WHERE am.amname='gin' ORDER BY c.relname")
    return {row["relname"]: row["pages"] for row in cur.fetchall()}


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
    limit = int(os.environ.get("HANDOFF_LAB_GIN_KB", handoff._HISTORY_GIN_LIMIT_KB))
    with patch.object(network, "_insert_rows", insert), patch.object(handoff, "_HISTORY_GIN_LIMIT_KB", limit):
        handoff.install_history(chat, data, "/tmp/handoff-lab")
    elapsed = time.monotonic() - start
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s) AS bytes", (lsn,))
        wal = int(cur.fetchone()["bytes"])
        pending = gin_pending_pages(cur)
    with capsys.disabled():
        print("HANDOFF_LAB " + json.dumps({"rows": sum(len(v) for v in data["history"].values()),
            "bytes": payload_bytes, "seconds": elapsed, "wal_bytes": wal,
            "pending_pages": pending, "gin_pending_list_limit_kb": limit,
            "cold_receiver": os.environ.get("HANDOFF_LAB_COLD") == "1",
            "tables_seconds": measured, "publish_metrics": timing.steps,
            "import_metrics": data.get("_history_metrics"),
            "duplicate_node_index": os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") != "1"}, sort_keys=True))
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            create_session_recall_text(conn.cursor())
