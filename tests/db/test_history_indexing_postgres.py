"""Deferred history indexes against disposable PostgreSQL, never an installed worker."""
from __future__ import annotations

from typing import Any

import pytest
from psycopg2.extras import Json

from app.db import base, history_indexing as hi, session_recall
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def finish_migration() -> None:
    for _ in range(15):
        if hi.migrate():
            return
    pytest.fail("online index migration did not finish")


def test_online_migration_and_restart_safe_import() -> None:
    session_recall.install_triggers()
    # Simulate a restart after each completed index, not one giant transaction.
    finish_migration()
    assert hi.migrate()
    with base.get_db() as conn:
        cur = conn.cursor()
        assert hi.enabled(cur)
        cur.execute("SELECT reloptions FROM pg_class WHERE relname=%s",
                    ("idx_session_recall_text_trgm_e_h2",))
        options = cur.fetchone()["reloptions"]
        assert "fastupdate=on" in options
        assert "gin_pending_list_limit=1024" in options
        cur.execute("DELETE FROM session_events WHERE session_id='history-index-test'")
        cur.execute("DELETE FROM sessions WHERE id='history-index-test'")
        cur.execute("INSERT INTO sessions(id,title) VALUES ('history-index-test','History test')")
        with hi.importing(cur, "session_events"):
            for i in range(3):
                cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) "
                            "VALUES (%s,'history-index-test',%s,'message.user',%s)",
                            (f"history-event-{i}", i, Json({"content": "deferredneedle"})))
        cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) "
                    "VALUES ('history-live-event','history-index-test',9,'message.user',%s)",
                    (Json({"content": "liveneedle"}),))
        cur.execute("SELECT history_index_state AS s FROM session_events "
                    "WHERE id='history-live-event'")
        assert cur.fetchone()["s"] == 0
        source = hi.search_table(cur, "session_recall_text")
        cur.execute(f"SELECT count(*) AS n FROM {source} WHERE body LIKE '%deferredneedle%'")
        assert cur.fetchone()["n"] == 0
    # Promotion is durable; a new invocation resumes without an in-memory cursor.
    assert hi.promote_batch(rows=1) > 0
    finish_migration()
    for _ in range(8):
        hi.promote_batch(rows=1)
    with base.get_db() as conn:
        cur = conn.cursor()
        source = hi.search_table(cur, "session_recall_text")
        cur.execute(f"SELECT count(*) AS n FROM {source} WHERE body LIKE '%deferredneedle%'")
        assert cur.fetchone()["n"] == 3
        # Editing imported history must never enter the history GIN pending list.
        cur.execute("UPDATE session_events SET payload=%s WHERE id='history-event-0'",
                    (Json({"content": "edited-live-needle"}),))
        cur.execute("SELECT history_index_state AS s FROM session_events "
                    "WHERE id='history-event-0'")
        assert cur.fetchone()["s"] == 0
        cur.execute("SELECT history_index_state AS s FROM session_recall_text "
                    "WHERE source='e' AND row_key='history-event-0'")
        assert cur.fetchone()["s"] == 0
        cur.execute("INSERT INTO session_recall_text(source,session_id,row_key,part,ts,body) "
                    "SELECT 'e','history-plan-probe',i::text,0,0,md5(i::text) "
                    "FROM generate_series(1,5000) i")
        cur.execute("ANALYZE session_recall_text")
        cur.execute("SET LOCAL enable_seqscan=off")
        cur.execute("SET LOCAL enable_indexscan=off")
        source = hi.search_table(cur, "session_recall_text")
        cur.execute(f"EXPLAIN SELECT * FROM {source} WHERE source='e' "
                    "AND body LIKE '%deferredneedle%'")
        plan = str(cur.fetchall())
        assert "idx_session_recall_text_trgm_e" in plan
        assert "idx_session_recall_text_trgm_e_h2" in plan
        cur.execute("DELETE FROM session_recall_text WHERE session_id='history-plan-probe'")
        cur.execute("DELETE FROM session_events WHERE session_id='history-index-test'")
        cur.execute("DELETE FROM sessions WHERE id='history-index-test'")


def test_import_rollback_and_live_setting_isolation() -> None:
    finish_migration()
    with pytest.raises(RuntimeError, match="rollback"):
        with base.get_db() as conn:
            with hi.importing(conn.cursor(), "session_events"):
                raise RuntimeError("rollback")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_setting(%s,true) AS value", (hi.SETTING,))
        assert cur.fetchone()["value"] in (None, "", "0")


def test_handoff_producer_searches_after_durable_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from collections.abc import Iterator
    from app.core.provider import fluid_pg_store
    from app.network import handoff
    from tests.db.test_handoff_import_postgres import snapshot

    finish_migration()
    data = snapshot(events=3, nodes=2, transcript=2, messages=2)
    for row in data["history"]["session_events"]:
        row["payload"]["content"] = "handoffneedle punctuation.a_b% unicode café"
    for row in data["history"]["fluid_session_nodes"]:
        row["message"]["role"] = "user"
        row["message"]["content"] = [{"kind": "text", "text":
            "handoffneedle café The owner is away. Do not ask questions"}]
    data["history"]["session_store_entries"] = [{
        "project_key": "history-producer", "subpath": None,
        "entry": {"type": "assistant", "message": {"content": "handoffneedle café"}},
        "created_at": "2026-10-19T00:00:00Z",
    }]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions(id,title,owner_device_id) "
                    "VALUES ('history-producer','History producer','test-remote')")
    sdk = handoff.install_history("history-producer", data, "/tmp/history-producer")
    from app.core import away_mode
    assert away_mode._last_delivered_away("history-producer")
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Test mirror")
    with base.get_db() as conn:
        cur = conn.cursor()
        for source in session_recall.SOURCES:
            while not session_recall.backfill_step(cur, source):
                pass
        for table in hi.TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE history_index_state=1")
            assert cur.fetchone()["n"] > 0
    assert not fluid_pg_store._search_sync(["handoffneedle"], 10, None)

    @contextmanager
    def fail_commit() -> Iterator[Any]:
        with base.get_db() as conn:
            yield conn
            raise RuntimeError("interrupted promotion")

    # The source row's state and its GIN/projection changes roll back together.
    with monkeypatch.context() as patcher:
        patcher.setattr(hi, "get_db", fail_commit)
        with pytest.raises(RuntimeError, match="interrupted promotion"):
            hi.promote_batch(rows=1)
    hi.promote_batch(rows=1)
    # Drop every connection/cursor, then resume exclusively from persisted state.
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    finish_migration()
    for _ in range(20):
        if not hi.promote_batch(rows=2):
            break
    assert fluid_pg_store._search_sync(["handoffneedle", "café"], 10, None)
    assert fluid_pg_store._search_sync(["punctuation.a_b%"], 10, None)
    from app.db import sessions
    assert sessions.search_sessions("handoffneedle")
    assert sessions.search_sessions_paginated("handoffneedle")
    with base.get_db() as conn:
        cur = conn.cursor()
        for table in hi.TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE history_index_state=1")
            assert cur.fetchone()["n"] == 0
        cur.execute("DELETE FROM sessions WHERE id='history-producer'")
        for table in ("session_events", "session_recall_text", "fluid_session_nodes",
                      "fluid_session_events", "fluid_sessions", "session_store_entries"):
            cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s,%s)",
                        ("history-producer", sdk))


def test_migration_retries_a_busy_table_without_long_lock() -> None:
    import time
    import psycopg2.errors

    finish_migration()
    with base.get_db() as conn:
        conn.cursor().execute("DROP INDEX idx_session_recall_text_trgm_e_h2")
    with base.get_db() as blocker:
        blocker.cursor().execute("LOCK session_recall_text IN SHARE MODE")
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            hi.migrate()
        assert time.monotonic() - started < 2.0
    finish_migration()


def test_catchup_yields_before_migration_and_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def promote() -> int:
        calls.append(1)
        return 1

    monkeypatch.setattr(hi, "migrate", lambda: True)
    monkeypatch.setattr(hi, "promote_batch", promote)
    assert not hi.run_pass(can_work=lambda: False)
    assert calls == []
    idle = iter((True, True, False))
    assert not hi.run_pass(pause=0, can_work=lambda: next(idle))
    assert calls == [1]
