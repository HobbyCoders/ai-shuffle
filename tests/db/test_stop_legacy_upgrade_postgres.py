"""Legacy task upgrade bounds are conservative, durable, and snapshot ordered."""
import threading
import time

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (
    cluster, connect, session_id,  # noqa: F401 - isolated PostgreSQL fixtures
)

pytestmark = requires_real_pg


def _legacy(session_id):
    from app.db import base, turns

    old = turns.enqueue_turn(session_id, "user", prompt="old", profile_id="default")
    with base.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO background_tasks (task_id,session_id,instance_id,kind,status,remote_device_id,started_at) "
            "VALUES ('legacy',%s,'old-install','agent','running','remote','2020-01-01T00:00:00Z')", (session_id,))
    return old


def _row(session_id):
    from app.db import background_tasks
    return background_tasks.get_background_task("legacy", session_id)


def test_upgrade_backfills_and_preserves_only_this_incarnations_bound(session_id):
    from app.core.task_registry import task_registry
    from app.db import background_tasks, base, database, turns

    old = _legacy(session_id)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE background_tasks DROP COLUMN turn_order_upper_bound")
        database._run_column_migrations(cursor)
    row = _row(session_id)
    assert row["turn_order"] is None
    assert row["turn_order_upper_bound"] == old["insert_order"]
    newer = turns.enqueue_turn(session_id, "user", prompt="new", profile_id="default")
    try:
        task_registry.hydrate_rows(session_id, [row])
        from app.core.query_session_lifecycle import capture_stop_scope
        scope = capture_stop_scope(session_id, before_turn=newer["insert_order"])
        assert [task["id"] for task in scope.active] == ["legacy"]
        assert scope.unattributed == ()
        # A queued pre-upgrade snapshot cannot erase the bound of the same run.
        stale = {**row, "turn_order_upper_bound": None}
        background_tasks.upsert_background_task(stale)
        assert _row(session_id)["turn_order_upper_bound"] == old["insert_order"]
        # A resume renews identity and exact order; it must not inherit the bound.
        background_tasks.upsert_background_task({**stale, "started_at": "2021-01-01T00:00:00Z",
                                                 "turn_order": newer["insert_order"]})
        resumed = _row(session_id)
        assert resumed["turn_order_upper_bound"] is None
        assert resumed["turn_order"] == newer["insert_order"]
    finally:
        task_registry.clear_session(session_id)


def test_successor_already_queued_at_upgrade_cannot_attribute_legacy_run(session_id):
    from app.core.query_session_lifecycle import capture_stop_scope
    from app.core.task_registry import task_registry
    from app.db import base, database, turns

    _legacy(session_id)
    newer = turns.enqueue_turn(session_id, "user", prompt="already queued", profile_id="default")
    with base.get_db() as conn:
        database._backfill_legacy_task_turn_bounds(conn.cursor())
    try:
        task_registry.hydrate_rows(session_id, [_row(session_id)])
        scope = capture_stop_scope(session_id, before_turn=newer["insert_order"])
        assert scope.active == []
        assert scope.unattributed == ("legacy",)
    finally:
        task_registry.clear_session(session_id)


def test_upgrade_snapshot_does_not_include_concurrently_admitted_successor(connect, session_id):
    from app.db import base, database, turns

    old = _legacy(session_id)
    holder = connect()
    holder.cursor().execute("UPDATE background_tasks SET title='held' WHERE session_id=%s", (session_id,))
    migrating = connect()
    outcome = {}

    def migrate():
        try:
            with migrating:
                database._backfill_legacy_task_turn_bounds(migrating.cursor())
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=migrate)
    thread.start()
    observer = connect(autocommit=True)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cursor = observer.cursor()
            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                           (migrating.get_backend_pid(),))
            if cursor.fetchone()[0] == "Lock":
                break
            time.sleep(0.01)
        else:
            pytest.fail("migration never reached the held legacy row")
        newer = turns.enqueue_turn(session_id, "user", prompt="during upgrade", profile_id="default")
        with base.get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO background_tasks (task_id,session_id,instance_id,kind,status,remote_device_id,turn_order) "
                "VALUES ('newer',%s,'new-install','agent','running','remote',%s)",
                (session_id, newer["insert_order"]))
    finally:
        holder.rollback()
        thread.join(5)
    assert not thread.is_alive()
    assert "error" not in outcome
    row = _row(session_id)
    assert row["turn_order_upper_bound"] == old["insert_order"] < newer["insert_order"]
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT turn_order,turn_order_upper_bound FROM background_tasks "
                       "WHERE task_id='newer' AND session_id=%s", (session_id,))
        assert dict(cursor.fetchone()) == {"turn_order": newer["insert_order"], "turn_order_upper_bound": None}
