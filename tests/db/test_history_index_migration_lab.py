"""Opt-in million-event online migration measurement on disposable PostgreSQL."""
from __future__ import annotations

import json
import os
import time

import pytest

from app.db import base, history_indexing as hi, session_recall
from tests.db.history_index_lab import LiveInserts
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401
from tests.db.test_history_indexing_postgres import finish_migration

pytestmark = requires_real_pg


@pytest.mark.skipif(os.environ.get("HISTORY_MILLION_LAB") != "1", reason="opt-in migration lab")
def test_million_event_migration(capsys: pytest.CaptureFixture[str]) -> None:
    count = 1_000_000
    with base.get_db() as conn:
        cur = conn.cursor()
        assert not hi.enabled(cur), "use a fresh disposable database"
        cur.execute("SELECT pg_get_indexdef(indexrelid) AS ddl FROM pg_index "
                    "WHERE indexrelid IN (to_regclass('idx_session_events_recall_trgm'), "
                    "to_regclass('idx_session_recall_text_trgm_e'))")
        definitions = [r["ddl"] for r in cur.fetchall()]
        cur.execute("DROP INDEX idx_session_events_recall_trgm")
        cur.execute("DROP INDEX idx_session_recall_text_trgm_e")
    # Seed without row triggers or GIN write amplification. These are 1M real
    # source rows and 1M projection rows; setup is outside the migration timing.
    for start in range(0, count, 10_000):
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) "
                        "SELECT 'million-event-'||i,'million-history-lab',i,'message.user', "
                        "jsonb_build_object('content', md5(i::text)||md5((i+1)::text)) "
                        "FROM generate_series(%s,%s) i", (start, start + 9999))
            cur.execute("INSERT INTO session_recall_text "
                        "(source,session_id,row_key,part,ts,body,seq,event_type,raw) "
                        "SELECT 'e',session_id,id,0,0,payload->>'content',seq,event_type, "
                        "payload->>'content' FROM session_events "
                        "WHERE session_id='million-history-lab' "
                        "AND seq >= %s AND seq < %s", (start, start + 10_000))
    with base.get_db() as conn:
        cur = conn.cursor()
        for ddl in definitions:
            cur.execute(ddl)
        cur.execute("ANALYZE session_events")
        cur.execute("ANALYZE session_recall_text")
        cur.execute("SELECT pg_current_wal_insert_lsn() AS lsn, "
                    "pg_total_relation_size('session_events') + "
                    "pg_total_relation_size('session_recall_text') AS bytes, "
                    "pg_relation_filenode('session_events') AS heap")
        before = cur.fetchone()
    session_recall.install_triggers()
    with base.get_db() as conn:
        cur = conn.cursor()
        for source in session_recall.SOURCES:
            cur.execute("INSERT INTO session_recall_backfill(source,done) VALUES (%s,true) "
                        "ON CONFLICT (source) DO UPDATE SET done=true", (source,))
        assert session_recall.ready(cur)
    sampler = LiveInserts()
    sampler.start()
    started = time.monotonic()
    try:
        finish_migration()
        elapsed = time.monotonic() - started
    finally:
        latency = sampler.finish()
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s) AS wal, "
                    "pg_total_relation_size('session_events') + "
                    "pg_total_relation_size('session_recall_text') AS bytes, "
                    "pg_relation_filenode('session_events') AS heap", (before["lsn"],))
        after = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM session_events "
                    "WHERE session_id='million-history-lab'")
        assert cur.fetchone()["n"] == count
        assert after["heap"] == before["heap"], "migration rewrote the event heap"
    with capsys.disabled():
        print("HISTORY_MIGRATION_LAB " + json.dumps({"events": count,
              "projection_rows": count, "content_chars": 64, "seconds": elapsed,
              "wal_bytes": int(after["wal"]), "before_bytes": before["bytes"],
              "after_bytes": after["bytes"], "live_latency_seconds": latency}))
