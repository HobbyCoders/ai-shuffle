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
def test_projection_rebuild_rebuilds_indexes_and_starts_workers_without_a_marker(
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
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (hi.KEY,))
        marker = cur.fetchone()
        assert marker == ({"value": "building"} if opted_in else None)

    # No direct migrator call may mask a worker that never starts by itself.
    for _ in range(20):
        if hi.run_pass(seconds=1, pause=0):
            break
    else:
        pytest.fail("automatic history catch-up did not finish after projection repair")
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
        assert cur.fetchone() == {"value": "ready"}


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
