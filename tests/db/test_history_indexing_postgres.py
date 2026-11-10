"""Deferred history indexes against disposable PostgreSQL, never an installed worker."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from psycopg2.extras import Json

from app.db import base, history_indexing as hi, session_recall
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg

# Captured at import, before the shared fixture swaps in its stand-in.
REAL_VOLUMES = hi._volumes


def hold_autovacuum(enabled: bool = False) -> None:
    """Keep autovacuum off the history tables while these tests drive migrate().

    The suite's tables collect rows from every module that shares the database,
    so autovacuum visits them mid-test. It holds SHARE UPDATE EXCLUSIVE and only
    yields to a waiter after deadlock_timeout (1 s), longer than the migration's
    250 ms metadata-lock check, so a migrate() that met it failed (#1434).
    Production's run_pass defers that pass and retries; these tests call
    migrate() directly and expect progress. Taking the ALTER's lock waits out a
    running autovacuum, and a new one re-reads the option before it starts.
    A worker that read the option just before the ALTER committed still runs
    with the old value, so the hold also waits until no autovacuum worker is
    on these tables.
    """
    oids = []
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SET LOCAL lock_timeout = '30s'")
        for table in hi.TABLES:
            cur.execute("SELECT to_regclass(%s)::oid AS oid", (table,))
            oid = cur.fetchone()["oid"]
            if oid:
                oids.append(oid)
                option = "RESET (autovacuum_enabled)" if enabled else "SET (autovacuum_enabled = false)"
                cur.execute(f"ALTER TABLE {table} {option}")
    if not enabled and oids:
        _wait_out_autovacuum(oids)


def _wait_out_autovacuum(oids: list[int]) -> None:
    """Return once no autovacuum worker has held, wanted or named these tables
    for 250 ms (one migration lock check), or fail after 30 s."""
    names = [t.rsplit(".", 1)[-1] for t in hi.TABLES]
    deadline = time.monotonic() + 30
    quiet_since = None
    while True:
        with base.get_db() as conn:
            cur = conn.cursor()
            # pg_locks covers a worker that holds or waits for a table lock;
            # the activity text covers one between choosing the table and
            # asking for the lock (readable when the role may see it).
            cur.execute(
                "SELECT a.pid, a.query FROM pg_stat_activity a "
                "WHERE a.backend_type = 'autovacuum worker' "
                "AND a.datname = current_database() AND ("
                " EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid"
                "         AND l.relation = ANY(%s::oid[]))"
                " OR a.query ~ ANY(SELECT '\\.' || n || '\\M' FROM unnest(%s::text[]) n))",
                (oids, names),
            )
            busy = cur.fetchall()
        now = time.monotonic()
        if busy:
            quiet_since = None
            if now > deadline:
                pytest.fail(f"autovacuum stayed on the history tables: {busy}")
        elif quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= 0.25:
            return
        time.sleep(0.05)


@pytest.fixture(scope="module", autouse=True)
def _restore_autovacuum(pg_schema):  # noqa: F811 - runs inside the module's schema
    yield
    hold_autovacuum(enabled=True)


@pytest.fixture(autouse=True)
def _no_autovacuum(_restore_autovacuum) -> None:
    hold_autovacuum()


def finish_migration() -> None:
    for _ in range(15):
        if hi.migrate():
            return
    pytest.fail("online index migration did not finish")


@pytest.fixture
def legacy_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows queued as state 1 by a build that promoted them one at a time."""
    monkeypatch.setattr(hi, "IMPORT_STATE", "1")


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
        assert f"gin_pending_list_limit={hi.HISTORY_PENDING_KB}" in options
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
        assert cur.fetchone()["n"] == 3
    assert hi.flush_pending(quiet=0)
    finish_migration()
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


def test_state_only_event_promotion_keeps_projection_in_its_own_queue(
    legacy_pending: None,
) -> None:
    session_recall.install_triggers()
    with base.get_db() as conn:
        assert session_recall.install_objects(conn.cursor())
    finish_migration()
    sid = "history-state-only"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_events"):
            cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) "
                        "VALUES (%s,%s,1,'message.user',%s)",
                        (sid, sid, Json({"content": "stateonlyneedle"})))
        cur.execute("SELECT ctid::text AS tid FROM session_recall_text WHERE session_id=%s",
                    (sid,))
        before = cur.fetchone()["tid"]
    try:
        assert hi.promote_batch(rows=1) == 1
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT ctid::text AS tid, history_index_state AS state "
                        "FROM session_recall_text WHERE session_id=%s", (sid,))
            assert cur.fetchone() == {"tid": before, "state": 1}
        # Simulate a restart between source and projection promotion. The source
        # is indexed, but the independently persisted text queue is still pending.
        base.close_pool()
        base.init_pool(min_conn=1, max_conn=6)
        assert hi.promote_batch() == 1
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT history_index_state AS state FROM session_recall_text "
                        "WHERE session_id=%s", (sid,))
            assert cur.fetchone()["state"] == 2
            # A live no-op edit must NOT take the state-only maintenance shortcut.
            with hi.importing(cur, "session_events"):
                cur.execute("UPDATE session_events SET payload=payload WHERE id=%s", (sid,))
            cur.execute("UPDATE session_events SET payload=payload WHERE id=%s", (sid,))
            cur.execute("SELECT history_index_state AS state FROM session_recall_text "
                        "WHERE session_id=%s", (sid,))
            assert cur.fetchone()["state"] == 0
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM session_events WHERE session_id=%s", (sid,))


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
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    from contextlib import contextmanager
    from collections.abc import Iterator
    from app.core.provider import fluid_pg_store
    from app.network import handoff
    from tests.db.test_handoff_import_postgres import snapshot

    finish_migration()
    data = snapshot(events=3, nodes=2, transcript=2, messages=2)
    for row in data["history"]["session_events"]:
        row["id"] = "history-producer-" + row["id"]
        row["payload"]["content"] = "handoffneedle punctuation.a_b% unicode café"
    for row in data["history"]["fluid_session_nodes"]:
        row["message"]["role"] = "user"
        row["message"]["content"] = [{"kind": "text", "text":
            "handoffneedle nativeonly café The owner is away. Do not ask questions"}]
    data["history"]["session_store_entries"] = [{
        "project_key": "history-producer", "subpath": None,
        "entry": {"type": "assistant", "message": {"content": "handoffneedle delegatedonly café"}},
        "created_at": "2026-10-19T00:00:00Z",
    }]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions(id,title,owner_device_id) "
                    "VALUES ('history-producer','History producer','test-remote')")
    sdk = handoff.install_history("history-producer", data, "/tmp/history-producer")

    def cleanup() -> None:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE id='history-producer'")
            for table in ("session_events", "session_recall_text", "fluid_session_nodes",
                          "fluid_session_events", "fluid_sessions", "session_store_entries"):
                cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s,%s)",
                            ("history-producer", sdk))

    request.addfinalizer(cleanup)
    from app.core import away_mode
    assert away_mode._last_delivered_away("history-producer")
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Test mirror")
    with base.get_db() as conn:
        cur = conn.cursor()
        for source in session_recall.SOURCES:
            while not session_recall.backfill_step(cur, source):
                pass
        # Moved rows go straight to the history indexes; nothing is left queued.
        for table in hi.TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE history_index_state=1")
            assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM session_recall_text "
                    "WHERE session_id=%s AND history_index_state=2", (sdk,))
        assert cur.fetchone()["n"] > 0
    # Searchable straight away, from the pending list, then again once merged.
    assert fluid_pg_store._search_sync(["handoffneedle"], 10, None)
    assert hi.flush_pending(quiet=0)
    # Drop every connection/cursor, then read only what the database persisted.
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    finish_migration()
    assert fluid_pg_store._search_sync(["handoffneedle", "café"], 10, None)
    # Another worker's mirror recalls its events; its store copy stays hidden
    # until this worker adopts the session, then native/delegated rows recall.
    assert not fluid_pg_store._search_sync(["nativeonly"], 10, None)
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id='test-local' "
                              "WHERE id='history-producer'")
    assert fluid_pg_store._search_sync(["nativeonly"], 10, None)
    assert fluid_pg_store._search_sync(["delegatedonly"], 10, None)
    from app.db import sessions
    assert sessions.search_sessions("handoffneedle")
    assert sessions.search_sessions_paginated("handoffneedle")
    with base.get_db() as conn:
        cur = conn.cursor()
        for table in hi.TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE history_index_state=1")
            assert cur.fetchone()["n"] == 0
        # A mirror without a local SDK pointer is eligible for event recall;
        # adopted local sessions deliberately recall their native/delegated rows.
        cur.execute("UPDATE sessions SET sdk_session_id=NULL, owner_device_id='test-remote' "
                    "WHERE id='history-producer'")
    assert fluid_pg_store._search_sync(["punctuation.a_b%"], 10, None)


def test_interrupted_legacy_promotion_rolls_back_with_its_index_changes(
    legacy_pending: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    finish_migration()
    sid = "history-legacy-rollback"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "fluid_session_nodes"):
            cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                        "VALUES (%s,'n',0,%s)", (sid, Json({"role": "user", "content": []})))

    def state() -> int:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT history_index_state AS s FROM fluid_session_nodes "
                        "WHERE session_id=%s", (sid,))
            return cur.fetchone()["s"]

    @contextmanager
    def fail_commit() -> Iterator[Any]:
        with base.get_db() as conn:
            yield conn
            raise RuntimeError("interrupted promotion")

    try:
        assert state() == 1
        with monkeypatch.context() as patcher:
            patcher.setattr(hi, "get_db", fail_commit)
            with pytest.raises(RuntimeError, match="interrupted promotion"):
                hi.promote_batch(rows=1)
        assert state() == 1
        assert hi.promote_batch(rows=1) == 1
        assert state() == 2
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM fluid_session_nodes WHERE session_id=%s", (sid,))


def test_merge_stops_for_a_foreground_turn_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_migration()
    sid = "history-merge-yield"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_recall_text"):
            cur.execute("INSERT INTO session_recall_text(source,session_id,row_key,part,ts,body) "
                        "VALUES ('n',%s,'r',0,0,'mergeyieldneedle')", (sid,))
    # Hold the merge at the index lock, so the statement is certainly running
    # when the turn arrives. An ordinary merge can finish before the first poll.
    blocker = base.get_db()
    conn = blocker.__enter__()
    try:
        cur = conn.cursor()
        cur.execute("DROP INDEX idx_session_recall_text_trgm_n_h2")
        calls = 0

        def can_work() -> bool:
            nonlocal calls
            calls += 1
            return calls <= 2  # Pass gate and pre-statement gate, then a turn starts.

        started = hi.time.monotonic()
        assert not hi.flush_pending(quiet=0, can_work=can_work, poll=0.05)
        assert hi.time.monotonic() - started < 30
        assert calls == 3
        assert hi._unmerged_at is not None, "a cancelled merge must be retried"
    finally:
        conn.rollback()
        blocker.__exit__(None, None, None)
    try:
        assert hi.flush_pending(quiet=0)
        assert hi._unmerged_at is None
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT gin_clean_pending_list(%s::regclass) AS pages",
                        ("idx_session_recall_text_trgm_n_h2",))
            assert cur.fetchone()["pages"] == 0
            source = hi.search_table(cur, "session_recall_text")
            cur.execute(f"SELECT count(*) AS n FROM {source} "
                        "WHERE body LIKE '%mergeyieldneedle%'")
            assert cur.fetchone()["n"] == 1
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM session_recall_text WHERE session_id=%s", (sid,))


def test_restart_merges_what_the_previous_process_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_migration()
    sid = "history-merge-restart"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_recall_text"):
            cur.execute("INSERT INTO session_recall_text(source,session_id,row_key,part,ts,body) "
                        "VALUES ('n',%s,'r',0,0,'restartmergeneedle')", (sid,))
    # A new process knows nothing about earlier imports but starts dirty.
    monkeypatch.setattr(hi, "_unmerged_at", 0.0)
    try:
        assert hi.flush_pending()
        assert hi._unmerged_at is None
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT gin_clean_pending_list(%s::regclass) AS pages",
                        ("idx_session_recall_text_trgm_n_h2",))
            assert cur.fetchone()["pages"] == 0
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM session_recall_text WHERE session_id=%s", (sid,))


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

    def promote(**kwargs: Any) -> int:
        calls.append(1)
        return 1

    monkeypatch.setattr(hi, "migrate", lambda: True)
    monkeypatch.setattr(hi, "promote_batch", promote)
    assert not hi.run_pass(can_work=lambda: False)
    assert calls == []
    idle = iter((True, True, False))
    assert not hi.run_pass(pause=0, can_work=lambda: next(idle))
    assert calls == [1]


@pytest.mark.parametrize("opted_in", [False, True])
def test_projection_rebuild_preserves_opt_in_and_resumes_pending_imports(
    opted_in: bool, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import sessions
    from app.network import handoff
    from tests.db.test_handoff_import_postgres import snapshot

    finish_migration()
    monkeypatch.setattr(hi, "FLUSH_QUIET_SECONDS", 0.0)
    sid = f"history-repair-{opted_in}"
    needle = f"repairneedle{opted_in}"
    with base.get_db() as conn:
        cur = conn.cursor()
        if not opted_in:
            cur.execute("DELETE FROM system_settings WHERE key=%s", (hi.KEY,))
        cur.execute("INSERT INTO sessions(id,title) VALUES (%s,'Projection repair')", (sid,))
    data = snapshot(events=1, nodes=1, transcript=1, messages=1)
    for row in data["history"]["session_events"]:
        row["id"] = sid + row["id"]
    for row in data["history"]["fluid_session_nodes"]:
        row["message"] = {"role": "user", "content": [{"kind": "text", "text": needle}]}
    sdk = handoff.install_history(sid, data, "/tmp/history-repair")

    def cleanup() -> None:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE id=%s", (sid,))
            for table in ("session_events", "session_event_counters", "session_recall_text",
                          "fluid_session_nodes", "fluid_session_events", "fluid_sessions",
                          "session_store_entries"):
                cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s,%s)", (sid, sdk))

    request.addfinalizer(cleanup)
    assert sessions.search_sessions(needle)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT history_index_state AS state FROM fluid_session_nodes "
                    "WHERE session_id=%s", (sdk,))
        assert cur.fetchone()["state"] == (2 if opted_in else 0)
        cur.execute("DROP TABLE session_recall_text")

    # Match startup order: repair/backfill recall BEFORE the history worker.
    for _ in range(20):
        if session_recall.run_backfill(max_seconds=1, pause_seconds=0):
            break
    else:
        pytest.fail("projection repair did not finish")
    hold_autovacuum()  # The repair recreated session_recall_text.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (hi.KEY,))
        marker = cur.fetchone()
        assert marker == ({"value": "building"} if opted_in else None)

    # Opt-in survives the rebuild; without it the worker must start nothing.
    for _ in range(20):
        if hi.run_pass(seconds=1, pause=0):
            break
    else:
        pytest.fail("history catch-up did not finish after projection repair")
    assert any(row["id"] == sid for row in sessions.search_sessions(needle))
    paginated, total = sessions.search_sessions_paginated(needle)
    assert any(row["id"] == sid for row in paginated)
    assert total == 1
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT history_index_state AS state FROM fluid_session_nodes "
                    "WHERE session_id=%s", (sdk,))
        assert cur.fetchone()["state"] == (2 if opted_in else 0)
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (hi.KEY,))
        assert cur.fetchone() == ({"value": "ready"} if opted_in else None)


def test_promotion_visits_bounded_window_in_heap_order(legacy_pending: None) -> None:
    finish_migration()
    sid = "history-heap-window"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "fluid_session_nodes"):
            for key in ("z", "a", "m"):
                cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                            "VALUES (%s,%s,0,%s)",
                            (sid, key, Json({"role": "user", "content": []})))
    calls = 0

    def can_work() -> bool:
        nonlocal calls
        calls += 1
        return calls <= 2  # Table gate, first row, then foreground work arrives.

    try:
        assert hi.promote_batch(rows=3, can_work=can_work) == 1
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT node_id, history_index_state AS state FROM fluid_session_nodes "
                        "WHERE session_id=%s ORDER BY node_id", (sid,))
            assert [(r["node_id"], r["state"]) for r in cur.fetchall()] == [
                ("a", 1), ("m", 1), ("z", 2),
            ]
            cur.execute("SELECT xmin::text AS version FROM fluid_session_nodes "
                        "WHERE session_id=%s AND node_id='z'", (sid,))
            version = cur.fetchone()["version"]
        # A fresh invocation resumes the persistent queue, never rewriting z.
        assert hi.promote_batch(rows=3) == 2
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT xmin::text AS version FROM fluid_session_nodes "
                        "WHERE session_id=%s AND node_id='z'", (sid,))
            assert cur.fetchone()["version"] == version
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("DELETE FROM fluid_session_nodes WHERE session_id=%s", (sid,))


def test_history_budget_upgrade_never_changes_live_index_options() -> None:
    finish_migration()
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT relname, reloptions FROM pg_class WHERE relname = ANY(%s)",
                    (list(hi.INDEXES),))
        before = cur.fetchall()
        cur.execute("ALTER INDEX idx_session_recall_text_trgm_e_h2 "
                    "SET (gin_pending_list_limit=1024)")
    # A receiver already marked ready by the former version is upgraded in place.
    assert hi.migrate()
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT reloptions FROM pg_class "
                    "WHERE relname='idx_session_recall_text_trgm_e_h2'")
        assert f"gin_pending_list_limit={hi.HISTORY_PENDING_KB}" in cur.fetchone()["reloptions"]
        cur.execute("SELECT relname, reloptions FROM pg_class WHERE relname = ANY(%s)",
                    (list(hi.INDEXES),))
        assert sorted(cur.fetchall(), key=lambda row: row["relname"]) == sorted(
            before, key=lambda row: row["relname"],
        )


def _insert_probe(cur: Any, sid: str, needle: str) -> None:
    cur.execute("INSERT INTO session_recall_text(source,session_id,row_key,part,ts,body) "
                "VALUES ('n',%s,'r',0,0,%s)", (sid, needle))


def _delete_probe(sid: str) -> None:
    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM session_recall_text WHERE session_id=%s", (sid,))


def _invalid_indexes() -> list[str]:
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE NOT i.indisvalid AND c.relnamespace=current_schema()::regnamespace")
        return [row["relname"] for row in cur.fetchall()]


def _migrate_in_thread() -> tuple[Any, list[BaseException]]:
    errors: list[BaseException] = []

    def body() -> None:
        try:
            finish_migration()
        except BaseException as exc:  # noqa: BLE001 — reported by the test thread
            errors.append(exc)

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    return worker, errors


@pytest.mark.parametrize("holder", ["writer", "old_snapshot"])
def test_concurrent_build_outlasts_writers_and_old_snapshots(holder: str) -> None:
    finish_migration()
    with base.get_db() as conn:
        conn.cursor().execute("DROP INDEX idx_session_recall_text_trgm_n_h2")
    sid = f"history-build-{holder}"
    blocker = base.get_db()
    conn = blocker.__enter__()
    try:
        cur = conn.cursor()
        if holder == "writer":
            _insert_probe(cur, sid, "buildwriterneedle")
        else:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("SELECT count(*) AS n FROM sessions")
            cur.fetchone()
        worker, errors = _migrate_in_thread()
        # Far beyond the short limit that applies to metadata locks.
        worker.join(1.5)
        assert worker.is_alive(), f"the build gave up behind {holder}: {errors}"
    finally:
        conn.rollback()
        blocker.__exit__(None, None, None)
    worker.join(60)
    assert not worker.is_alive()
    assert errors == []
    assert _invalid_indexes() == []


def test_interrupted_concurrent_build_is_rebuilt_on_the_next_pass() -> None:
    finish_migration()
    with base.get_db() as conn:
        conn.cursor().execute("DROP INDEX idx_session_recall_text_trgm_n_h2")
    sid = "history-build-interrupted"
    blocker = base.get_db()
    conn = blocker.__enter__()
    try:
        cur = conn.cursor()
        _insert_probe(cur, sid, "buildinterruptedneedle")
        worker, errors = _migrate_in_thread()
        deadline = time.monotonic() + 20
        cancelled = False
        while time.monotonic() < deadline and not cancelled:
            with base.get_db() as admin:
                probe = admin.cursor()
                probe.execute("SELECT pg_cancel_backend(pid) AS ok FROM pg_stat_activity "
                              "WHERE query LIKE 'CREATE INDEX CONCURRENTLY%%' "
                              "AND pid <> pg_backend_pid()")
                cancelled = bool(probe.fetchall())
            time.sleep(0.05)
        assert cancelled, "the build never reached its concurrent phase"
        worker.join(20)
        assert errors and isinstance(errors[0], psycopg2.Error)
        assert _invalid_indexes(), "a cancelled concurrent build should leave an INVALID index"
    finally:
        conn.rollback()
        blocker.__exit__(None, None, None)
    finish_migration()
    assert _invalid_indexes() == []


def test_database_error_during_migration_retries_at_normal_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> bool:
        raise psycopg2.errors.LockNotAvailable("busy")

    monkeypatch.setattr(hi, "migrate", fail)
    # Escaping would send startup's loop into its ten-minute error back-off.
    assert hi.run_pass() is False


@pytest.mark.parametrize(("free", "index_bytes"), [(1024 ** 3, 4 * 1024 ** 3)])
def test_automatic_build_defers_when_the_disk_is_short(
    free: int, index_bytes: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    finish_migration()
    with base.get_db() as conn:
        conn.cursor().execute("DROP INDEX idx_session_recall_text_trgm_n_h2")
    monkeypatch.setattr(hi, "_retry_build_at", 0.0)
    monkeypatch.setattr(hi, "_volumes", lambda cur, size: [("data", 1, free, index_bytes)])
    with caplog.at_level(logging.WARNING, logger=hi.__name__):
        assert hi.migrate() is False
    assert "free" in caplog.text
    with base.get_db() as conn:
        cur = conn.cursor()
        assert hi._valid(cur, "idx_session_recall_text_trgm_n"), "the original must stay usable"
        assert not hi._valid(cur, "idx_session_recall_text_trgm_n_h0")
    assert _invalid_indexes() == []
    # Waits out the retry interval without touching the database again.
    monkeypatch.setattr(hi, "_online_cursor", lambda: pytest.fail("retried too soon"))
    assert hi.migrate() is False
    monkeypatch.undo()
    finish_migration()


TEST_TABLESPACE = "history_index_test"
REBUILT = "idx_session_recall_text_trgm_n"
LEFT_ALONE_LOG = "stays where it is"
RECLAIMED_LOG = "Removed unfinished history index"
PLENTY = 100 * 1024 ** 3


def _index_tablespace(cur: Any, name: str) -> str | None:
    cur.execute("SELECT t.spcname FROM pg_class c LEFT JOIN pg_tablespace t "
                "ON t.oid = c.reltablespace WHERE c.oid = to_regclass(%s)", (name,))
    return cur.fetchone()["spcname"]


def _index_identity(cur: Any, name: str) -> tuple[int, str | None]:
    """Changes if the index is rebuilt, replaced or moved."""
    cur.execute("SELECT to_regclass(%s)::oid AS oid", (name,))
    return cur.fetchone()["oid"], _index_tablespace(cur, name)


def _make_rebuild_pending(cur: Any, tablespace: str | None) -> None:
    """Put back the single index a worker has before the upgrade."""
    cur.execute("SELECT tail FROM history_index_migrations WHERE name = %s", (REBUILT,))
    expression, _, predicate = cur.fetchone()["tail"].partition(" WHERE ")
    cur.execute(f"DROP INDEX IF EXISTS {REBUILT}, {REBUILT}_h0, {REBUILT}_h2")
    place = f" TABLESPACE {tablespace}" if tablespace else ""
    where = f" WHERE {predicate}" if predicate else ""
    cur.execute(f"CREATE INDEX {REBUILT} ON session_recall_text USING gin "
                f"{expression}{place}{where}")


@pytest.fixture
def tablespace() -> Iterator[str]:
    """A real non-default tablespace. CI's unprivileged role is given one."""
    finish_migration()
    with hi._online_cursor() as cur:
        cur.execute("SELECT 1 FROM pg_tablespace WHERE spcname = %s", (TEST_TABLESPACE,))
        if not cur.fetchone():
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()["rolsuper"]:
                pytest.skip(f"needs the {TEST_TABLESPACE} tablespace or a superuser")
            cur.execute("SET allow_in_place_tablespaces = true")
            cur.execute(f"CREATE TABLESPACE {TEST_TABLESPACE} LOCATION ''")
    yield TEST_TABLESPACE
    with hi._online_cursor() as cur:
        for name in (REBUILT, f"{REBUILT}_h2"):
            cur.execute(f"ALTER INDEX IF EXISTS {name} SET TABLESPACE pg_default")
    finish_migration()


@dataclass
class Disks:
    """Made-up free bytes per place, and each place the accounting looked at."""
    free: dict[str, int]
    seen: list[str]


@pytest.fixture
def disks(tablespace: str, tmp_path: Path) -> Iterator[Disks]:
    """Runs the real accounting over made-up free space."""
    from app.maintenance import session_recall_cache

    result = Disks({"data": PLENTY, "wal": PLENTY}, [])
    labels = {str(tmp_path): "data", str(tmp_path / "pg_wal"): "wal"}

    def disk(path: Any) -> tuple[int, int]:
        label = labels[str(path)]
        result.seen.append(label)
        return list(result.free).index(label), result.free[label]

    # Undone here, so that the tablespace fixture's cleanup sees real settings.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_recall_cache, "managed_data_directory", lambda cur, _: tmp_path)
        patch.setattr(hi, "_volumes", REAL_VOLUMES)
        patch.setattr(hi, "_disk", disk)
        patch.setattr(hi, "_retry_build_at", 0.0)
        patch.setattr(hi, "_left_alone", set())
        yield result


def test_default_tablespace_setting_cannot_redirect_a_rebuild(
    tablespace: str, disks: Disks, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild stays with the database, as measured."""
    borrow = hi._online_cursor

    @contextmanager
    def redirected() -> Iterator[Any]:
        with borrow() as cur:
            if cur is not None:  # As a role or database setting would.
                cur.execute(f"SET default_tablespace = {tablespace}")
            yield cur

    with borrow() as cur:
        _make_rebuild_pending(cur, None)
    monkeypatch.setattr(hi, "_online_cursor", redirected)
    assert hi.migrate() is False
    monkeypatch.setattr(hi, "_online_cursor", borrow)
    assert disks.seen == ["data", "wal"]
    with hi._online_cursor() as cur:
        assert hi._valid(cur, REBUILT) and hi._valid(cur, f"{REBUILT}_h2")
        assert _index_tablespace(cur, REBUILT) is None
        assert _index_tablespace(cur, f"{REBUILT}_h2") is None


def test_index_on_its_own_tablespace_is_left_alone_without_holding_the_upgrade_up(
    tablespace: str, disks: Disks,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, tablespace)
        before = _index_identity(cur, REBUILT)
    monkeypatch.setattr(hi, "_unmerged_at", None)
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        assert hi.migrate() is True, "a skipped index must not leave the pass unfinished"
        assert hi.migrate() is True
        assert hi.run_pass() is True
    assert caplog.text.count(LEFT_ALONE_LOG) == 1, "one line, not one per pass"
    assert hi._retry_build_at == 0.0, "nothing to retry"
    assert disks.seen == []
    with hi._online_cursor() as cur:
        assert hi.enabled(cur)
        assert _index_identity(cur, REBUILT) == before
        assert hi._valid(cur, REBUILT)
        assert not hi._valid(cur, f"{REBUILT}_h0") and not hi._valid(cur, f"{REBUILT}_h2")
        cur.execute("SELECT to_regclass(%s) AS h0, to_regclass(%s) AS h2",
                    (f"{REBUILT}_h0", f"{REBUILT}_h2"))
        assert cur.fetchone() == {"h0": None, "h2": None}
    assert _invalid_indexes() == []


def test_already_converted_index_on_its_own_tablespace_is_unchanged(
    tablespace: str, disks: Disks, caplog: pytest.LogCaptureFixture,
) -> None:
    with hi._online_cursor() as cur:
        for name in (REBUILT, f"{REBUILT}_h2"):
            cur.execute(f"ALTER INDEX {name} SET TABLESPACE {tablespace}")
        before = [_index_identity(cur, REBUILT), _index_identity(cur, f"{REBUILT}_h2")]
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        assert hi.migrate() is True
    assert LEFT_ALONE_LOG not in caplog.text
    assert disks.seen == []
    with hi._online_cursor() as cur:
        assert [_index_identity(cur, REBUILT), _index_identity(cur, f"{REBUILT}_h2")] == before


def test_search_and_moves_work_with_one_index_left_alone(tablespace: str, disks: Disks) -> None:
    sid = "history-mixed-index"
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, tablespace)
    finish_migration()
    try:
        with base.get_db() as conn:
            cur = conn.cursor()
            assert hi.enabled(cur)
            with hi.importing(cur, "session_recall_text"):
                _insert_probe(cur, sid, "mixedimportneedle")
            cur.execute("INSERT INTO session_recall_text(source,session_id,row_key,part,ts,body) "
                        "VALUES ('n',%s,'live',0,0,'mixedliveneedle')", (sid,))
        for _ in range(2):  # Before and after the merge.
            with base.get_db() as conn:
                cur = conn.cursor()
                source = hi.search_table(cur, "session_recall_text")
                for needle in ("mixedimportneedle", "mixedliveneedle"):
                    cur.execute(f"SELECT count(*) AS n FROM {source} WHERE body LIKE %s",
                                (f"%{needle}%",))
                    assert cur.fetchone()["n"] == 1, needle
            assert hi.flush_pending(quiet=0) is True
    finally:
        _delete_probe(sid)


def _leftover(cur: Any, name: str, kind: str, place: str = "") -> None:
    """A real index in the given state, as an interrupted concurrent build leaves one."""
    if kind == "valid":
        cur.execute(f"CREATE INDEX {name} ON session_recall_text "
                    f"USING gin (body gin_trgm_ops){place}")
    elif kind == "invalid":
        # Two rows break the uniqueness, which fails the build after its catalog
        # entry is committed. It needs no privilege to mark the index invalid.
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {name} "
                        f"ON session_recall_text ((1)){place}")


def _exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS found", (name,))
    return cur.fetchone()["found"] is not None


@pytest.fixture
def two_rows() -> Iterator[None]:
    sid = "history-interrupted-leftovers"
    with base.get_db() as conn:
        cur = conn.cursor()
        _insert_probe(cur, sid, "leftoverone")
        _insert_probe(cur, sid + "-2", "leftovertwo")
    yield
    _delete_probe(sid)
    _delete_probe(sid + "-2")


LEFTOVER_KINDS = ("absent", "valid", "invalid")


@pytest.mark.parametrize("h2", LEFTOVER_KINDS)
@pytest.mark.parametrize("h0", LEFTOVER_KINDS)
def test_interrupted_replacements_of_a_left_alone_index_are_reclaimed_once(
    h0: str, h2: str, tablespace: str, disks: Disks, two_rows: None,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, tablespace)
        _leftover(cur, f"{REBUILT}_h0", h0)
        _leftover(cur, f"{REBUILT}_h2", h2)
        before = _index_identity(cur, REBUILT)
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        assert hi.migrate() is True
    assert hi._retry_build_at == 0.0
    assert disks.seen == []
    with hi._online_cursor() as cur:
        assert not _exists(cur, f"{REBUILT}_h0") and not _exists(cur, f"{REBUILT}_h2")
        assert _index_identity(cur, REBUILT) == before
        assert hi._valid(cur, REBUILT)
    assert _invalid_indexes() == []
    assert caplog.text.count(RECLAIMED_LOG) == 2 - [h0, h2].count("absent")

    # Once clean, a pass takes no lock and logs nothing more.
    caplog.clear()
    ddl: list[str] = []
    real = hi._concurrent_ddl
    monkeypatch.setattr(hi, "_concurrent_ddl", lambda c, t, s: (ddl.append(s), real(c, t, s)))
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        assert hi.migrate() is True
    assert ddl == []
    assert caplog.text == ""


@pytest.mark.parametrize("original", ["partial", "invalid"])
def test_replacements_are_kept_unless_a_valid_full_original_remains(
    original: str, tablespace: str, disks: Disks, two_rows: None,
) -> None:
    with hi._online_cursor() as cur:
        if original == "partial":
            cur.execute(f"DROP INDEX {REBUILT}_h2")
            cur.execute(f"ALTER INDEX {REBUILT} SET TABLESPACE {tablespace}")
        else:
            cur.execute(f"DROP INDEX {REBUILT}, {REBUILT}_h2")
            _leftover(cur, REBUILT, "invalid", f" TABLESPACE {tablespace}")
        _leftover(cur, f"{REBUILT}_h0", "valid")
        _leftover(cur, f"{REBUILT}_h2", "invalid")
    try:
        assert hi.migrate() is True
        with hi._online_cursor() as cur:
            assert _exists(cur, REBUILT)
            assert _exists(cur, f"{REBUILT}_h0") and _exists(cur, f"{REBUILT}_h2")
    finally:
        with hi._online_cursor() as cur:
            _make_rebuild_pending(cur, None)


def test_cancelled_reclaim_is_retried_at_the_normal_cadence(
    tablespace: str, disks: Disks, two_rows: None,
) -> None:
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, tablespace)
        _leftover(cur, f"{REBUILT}_h0", "valid")
        _leftover(cur, f"{REBUILT}_h2", "invalid")
        before = _index_identity(cur, REBUILT)
    sid = "history-reclaim-cancelled"
    results: list[bool] = []
    blocker = base.get_db()
    conn = blocker.__enter__()
    try:
        _insert_probe(conn.cursor(), sid, "reclaimcancelledneedle")  # DROP waits for this.
        worker = threading.Thread(target=lambda: results.append(hi.run_pass()))
        worker.start()
        deadline = time.monotonic() + 20
        cancelled = False
        while time.monotonic() < deadline and not cancelled:
            with base.get_db() as admin:
                probe = admin.cursor()
                probe.execute("SELECT pg_cancel_backend(pid) AS ok FROM pg_stat_activity "
                              "WHERE query LIKE 'DROP INDEX CONCURRENTLY%%' "
                              "AND pid <> pg_backend_pid()")
                cancelled = bool(probe.fetchall())
            time.sleep(0.05)
        assert cancelled, "the reclaim never reached its concurrent phase"
        worker.join(20)
        assert results == [False], "cancelled work is a retry, not a startup back-off"
        assert hi._retry_build_at == 0.0
    finally:
        conn.rollback()
        blocker.__exit__(None, None, None)
    with hi._online_cursor() as cur:
        assert _exists(cur, f"{REBUILT}_h0"), "the cancelled drop leaves its index behind"
        assert _exists(cur, f"{REBUILT}_h2")
        assert _index_identity(cur, REBUILT) == before and hi._valid(cur, REBUILT)
    assert hi.run_pass() is True
    with hi._online_cursor() as cur:
        assert not _exists(cur, f"{REBUILT}_h0") and not _exists(cur, f"{REBUILT}_h2")
        assert _index_identity(cur, REBUILT) == before
    assert _invalid_indexes() == []


def _after_tablespace_read(monkeypatch: pytest.MonkeyPatch, *statements: str) -> None:
    """Runs these on another connection right after the upgrade reads an index's place."""
    borrow = hi._online_cursor
    pending = list(statements)

    class Cursor:
        def __init__(self, cur: Any) -> None:
            self._cur = cur

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cur, name)

        def execute(self, statement: str, *args: Any) -> None:
            self._cur.execute(statement, *args)
            if "reltablespace" in statement and pending:
                other = psycopg2.connect(os.environ["TEST_POSTGRES_DSN"])
                try:
                    other.autocommit = True
                    while pending:
                        other.cursor().execute(pending.pop(0))
                finally:
                    other.close()

    @contextmanager
    def wrapped() -> Iterator[Any]:
        with borrow() as cur:
            yield None if cur is None else Cursor(cur)

    monkeypatch.setattr(hi, "_online_cursor", wrapped)


@pytest.mark.parametrize("default_has_room", [True, False])
def test_index_moved_away_after_the_read_is_rebuilt_only_where_measured(
    default_has_room: bool, tablespace: str, disks: Disks,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    disks.free["data"] = PLENTY if default_has_room else 1
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, None)
    _after_tablespace_read(monkeypatch, f"ALTER INDEX {REBUILT} SET TABLESPACE {tablespace}")
    with caplog.at_level(logging.WARNING, logger=hi.__name__):
        assert hi.migrate() is False
    monkeypatch.undo()
    assert disks.seen == ["data", "wal"]
    with hi._online_cursor() as cur:
        assert hi._valid(cur, REBUILT), "the original must stay usable"
        if default_has_room:
            assert _index_tablespace(cur, REBUILT) is None
            assert _index_tablespace(cur, f"{REBUILT}_h2") is None
        else:
            assert "free some disk space" in caplog.text
            assert _index_tablespace(cur, REBUILT) == tablespace
            assert not hi._valid(cur, f"{REBUILT}_h0")
    assert _invalid_indexes() == []


def test_index_moved_back_after_the_read_is_upgraded_on_the_next_pass(
    tablespace: str, disks: Disks, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hi._online_cursor() as cur:
        _make_rebuild_pending(cur, tablespace)
    _after_tablespace_read(monkeypatch, f"ALTER INDEX {REBUILT} SET TABLESPACE pg_default")
    assert hi.migrate() is True, "this pass read it on its own tablespace"
    monkeypatch.undo()
    assert disks.seen == []
    with hi._online_cursor() as cur:
        assert not hi._valid(cur, f"{REBUILT}_h2")
    finish_migration()
    assert disks.seen == ["data", "wal"]
    with hi._online_cursor() as cur:
        assert hi._valid(cur, REBUILT) and hi._valid(cur, f"{REBUILT}_h2")
        assert _index_tablespace(cur, REBUILT) is None
    assert _invalid_indexes() == []


def test_merge_waits_for_an_import_scope_held_past_the_quiet_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_migration()
    monkeypatch.setattr(hi, "FLUSH_QUIET_SECONDS", 0.2)
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_recall_text"):
            time.sleep(0.5)  # Longer than the quiet time, like a big table's publish.
            monkeypatch.setattr(hi, "_online_cursor", lambda: pytest.fail("merged mid-move"))
            assert hi.flush_pending() is False
    # The quiet time counts from the end of the scope, not its start.
    assert hi.flush_pending() is False
    monkeypatch.undo()
    monkeypatch.setattr(hi, "FLUSH_QUIET_SECONDS", 0.2)
    time.sleep(0.3)
    assert hi.flush_pending() is True


def test_merge_is_cancelled_by_an_import_that_starts_after_it() -> None:
    finish_migration()
    sid = "history-merge-import"
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_recall_text"):
            _insert_probe(cur, sid, "mergeimportneedle")
    blocker = base.get_db()
    conn = blocker.__enter__()
    result: list[bool] = []
    try:
        # Holds the merge at the index lock so its statement is certainly running.
        conn.cursor().execute("DROP INDEX idx_session_recall_text_trgm_n_h2")
        worker = threading.Thread(
            target=lambda: result.append(hi.flush_pending(quiet=0, poll=0.05)), daemon=True)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()
        with base.get_db() as other:
            with hi.importing(other.cursor(), "session_recall_text"):
                worker.join(10)
                assert not worker.is_alive(), "the import did not cancel the merge"
    finally:
        conn.rollback()
        blocker.__exit__(None, None, None)
    assert result == [False]
    assert hi._unmerged_at is not None, "a cancelled merge must be retried"
    assert hi.flush_pending(quiet=0)
    _delete_probe(sid)


@pytest.fixture
def imported_overflow(request: pytest.FixtureRequest) -> list[str]:
    """Two imported events too long to index inline, leaving overflow markers."""
    session_recall.install_triggers()
    with base.get_db() as conn:
        assert session_recall.install_objects(conn.cursor())
    finish_migration()
    ids = ["history-overflow-a", "history-overflow-b"]
    with base.get_db() as conn:
        cur = conn.cursor()
        with hi.importing(cur, "session_events"):
            for seq, sid in enumerate(ids):
                cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) "
                            "VALUES (%s,%s,%s,'message.user',%s)",
                            (sid, sid, seq, Json({"content": "overflowneedle " * 20_000})))
        cur.execute("SELECT count(*) AS n FROM session_recall_text "
                    "WHERE session_id = ANY(%s) AND part = -1", (ids,))
        assert cur.fetchone()["n"] == 2

    def cleanup() -> None:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM session_events WHERE session_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM session_recall_text WHERE session_id = ANY(%s)", (ids,))

    request.addfinalizer(cleanup)
    return ids


def test_delayed_overflow_promotion_after_a_merge_is_merged_too(
    imported_overflow: list[str],
) -> None:
    assert hi.flush_pending(quiet=0)
    assert hi._unmerged_at is None
    while session_recall.promote_overflow(pause_seconds=0):
        pass
    assert hi._unmerged_at is not None, "history pieces written after the merge were forgotten"
    assert hi.flush_pending(quiet=0)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT gin_clean_pending_list(%s::regclass) AS pages",
                    ("idx_session_recall_text_trgm_e_h2",))
        assert cur.fetchone()["pages"] == 0
        source = hi.search_table(cur, "session_recall_text")
        cur.execute(f"SELECT count(DISTINCT session_id) AS n FROM {source} "
                    "WHERE source='e' AND part >= 0 AND body LIKE '%%overflowneedle%%'")
        assert cur.fetchone()["n"] == 2


def test_overflow_promotion_yields_to_a_foreground_turn(imported_overflow: list[str]) -> None:
    # One row per pass still goes through, so unbroken activity cannot hide a
    # long message from search for ever.
    assert session_recall.promote_overflow(pause_seconds=0, can_work=lambda: False) == 1
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM session_recall_text "
                    "WHERE session_id = ANY(%s) AND part = -1", (imported_overflow,))
        assert cur.fetchone()["n"] == 1


def test_import_scope_allows_an_inline_merge_and_restores_the_limit() -> None:
    finish_migration()

    def limits(cur: Any) -> tuple[str, str]:
        cur.execute("SELECT current_setting('statement_timeout') AS timeout, "
                    "current_setting('work_mem') AS memory")
        row = cur.fetchone()
        return row["timeout"], row["memory"]

    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '30s'")
        cur.execute("SET LOCAL work_mem = '5MB'")
        cur.execute("SET LOCAL maintenance_work_mem = '70MB'")
        with hi.importing(cur, "session_events"):
            assert limits(cur) == (hi.IMPORT_STATEMENT_TIMEOUT, "70MB")
        assert limits(cur) == ("30s", "5MB")
