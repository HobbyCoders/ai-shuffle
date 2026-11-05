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
        from app.db import background_tasks
        background_tasks.upsert_background_task({"task_id": "newer", "session_id": session_id,
            "kind": "agent", "status": "running", "remote_device_id": "remote",
            "started_at": "2026-11-05T00:00:00Z", "turn_order": newer["insert_order"]})
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


@pytest.mark.parametrize("exact_order", [False, True])
def test_previous_version_writer_cannot_transfer_bound_to_new_incarnation(session_id, exact_order):
    from app.core.query_session_lifecycle import capture_stop_scope
    from app.core.task_registry import task_registry
    from app.db import base, database, turns

    _legacy(session_id)
    with base.get_db() as conn:
        database._backfill_legacy_task_turn_bounds(conn.cursor())
    if exact_order:
        from app.db import background_tasks
        snapshot = _row(session_id)
        snapshot.update(turn_order=snapshot["turn_order_upper_bound"], turn_order_upper_bound=None)
        background_tasks.upsert_background_task(snapshot)
    newer = turns.enqueue_turn(session_id, "user", prompt="newer input", profile_id="default")
    with base.get_db() as conn:
        # Pre-column writers update started_at but omit every new order column.
        # This is their ON CONFLICT SET behavior on the existing legacy row.
        conn.cursor().execute("UPDATE background_tasks SET started_at='2021-01-01T00:00:00Z' "
                              "WHERE task_id='legacy' AND session_id=%s", (session_id,))
    try:
        task_registry.hydrate_rows(session_id, [_row(session_id)])
        scope = capture_stop_scope(session_id, before_turn=newer["insert_order"])
        assert scope.active == []
        assert scope.unattributed == ("legacy",)
    finally:
        task_registry.clear_session(session_id)


CLOCK = "2026-11-05T00:00:00+00:00"


def _legacy_write(cursor, sid, task_id, operation):
    # Pre-nonce INSERT/ON CONFLICT writes omit ALL provenance metadata. An
    # unchanged timestamp must not make that omission look like the same run.
    if operation == "update":
        cursor.execute("UPDATE background_tasks SET started_at=%s, status='running' "
                       "WHERE task_id=%s AND session_id=%s", (CLOCK, task_id, sid))
    else:
        cursor.execute("INSERT INTO background_tasks "
            "(task_id,session_id,instance_id,kind,status,started_at,remote_device_id) "
            "VALUES (%s,%s,'old-install','agent','running',%s,'remote') "
            "ON CONFLICT (task_id,session_id) DO UPDATE SET "
            "instance_id=EXCLUDED.instance_id,kind=EXCLUDED.kind,status=EXCLUDED.status,"
            "started_at=EXCLUDED.started_at,remote_device_id=EXCLUDED.remote_device_id",
            (task_id, sid, CLOCK))


def _saved_task(sid, task_id):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM background_tasks WHERE task_id=%s AND session_id=%s", (task_id, sid))
        return dict(cursor.fetchone())


@pytest.mark.parametrize("operation", ["insert", "upsert", "update"])
def test_same_clock_old_sql_and_cached_new_writer_cannot_restore_old_scope(session_id, operation):
    from app.core.task_registry import task_registry
    from app.db import background_tasks, base, database, turns

    old = turns.enqueue_turn(session_id, "user", prompt="old", profile_id="default")
    cached = {"task_id": "same-clock", "session_id": session_id, "kind": "agent",
              "status": "running", "started_at": CLOCK, "remote_device_id": "remote",
              "turn_order": old["insert_order"]}
    if operation != "insert":
        background_tasks.upsert_background_task(cached)
        assert _saved_task(session_id, "same-clock")["turn_order"] == old["insert_order"]
    newer = turns.enqueue_turn(session_id, "user", prompt="new", profile_id="default")
    with base.get_db() as conn:
        _legacy_write(conn.cursor(), session_id, "same-clock", operation)
    saved = _saved_task(session_id, "same-clock")
    assert saved["turn_order"] is saved["turn_order_upper_bound"] is saved["turn_order_incarnation"] is None
    assert saved["turn_order_invalid_through"] >= newer["insert_order"]
    # A coalesced snapshot from before the old writer ran gets a fresh nonce
    # too. The durable invalidation boundary, not that nonce, rejects its scope.
    background_tasks.upsert_background_task(cached)
    assert _saved_task(session_id, "same-clock")["turn_order"] is None
    with base.get_db() as conn:
        database._backfill_legacy_task_turn_bounds(conn.cursor())
    rebound = _saved_task(session_id, "same-clock")
    assert rebound["turn_order_upper_bound"] == newer["insert_order"]
    # Simulated restart gives only a conservative CURRENT snapshot bound.
    task_registry.hydrate_rows(session_id, [rebound])
    assert task_registry.task_turn_order(session_id, "same-clock") is None
    with base.get_db() as conn:
        _legacy_write(conn.cursor(), session_id, "same-clock", "update")
    background_tasks.upsert_background_task(rebound)
    assert _saved_task(session_id, "same-clock")["turn_order_upper_bound"] is None
    newest = turns.enqueue_turn(session_id, "user", prompt="fresh known admission", profile_id="default")
    background_tasks.upsert_background_task({**cached, "turn_order": newest["insert_order"]})
    task_registry.clear_session(session_id)
    try:
        task_registry.hydrate_rows(session_id, [background_tasks.get_background_task("same-clock", session_id)])
        assert task_registry.task_turn_order(session_id, "same-clock") == newest["insert_order"]
    finally:
        task_registry.clear_session(session_id)


@pytest.mark.asyncio
async def test_actual_stop_revalidates_cached_order_after_same_clock_old_write(session_id, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core import query_session_lifecycle as qsl
    from app.core.task_registry import task_registry
    from app.db import background_tasks, base, turns

    old = turns.enqueue_turn(session_id, "user", prompt="old", profile_id="default")
    background_tasks.upsert_background_task({"task_id": "cached", "session_id": session_id,
        "kind": "agent", "status": "running", "started_at": CLOCK,
        "remote_device_id": "remote", "turn_order": old["insert_order"]})
    task_registry.hydrate_rows(session_id, [background_tasks.get_background_task("cached", session_id)])
    newer = turns.enqueue_turn(session_id, "user", prompt="new", profile_id="default")
    scope = qsl.capture_stop_scope(session_id, before_turn=newer["insert_order"])
    assert [row["id"] for row in scope.active] == ["cached"]
    with base.get_db() as conn:
        _legacy_write(conn.cursor(), session_id, "cached", "upsert")
    stop = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(qsl, "stop_session_task", stop)
    try:
        result = await qsl.stop_all_background_tasks(session_id, scope=scope)
        stop.assert_not_awaited()
        assert result == [{"ok": False, "incomplete": True,
                           "task_id": "cached", "skipped": "unattributed"}]
        assert _saved_task(session_id, "cached")["status"] == "running"
    finally:
        task_registry.clear_session(session_id)


def test_guard_install_and_backfill_are_atomic_against_an_old_writer(connect, session_id):
    from app.db import background_tasks, database, turns

    turns.enqueue_turn(session_id, "user", prompt="legacy", profile_id="default")
    # Recreate the pre-guard state ONLY in this test-owned disposable cluster.
    setup = connect(autocommit=True)
    setup.cursor().execute("DROP TRIGGER trg_background_task_order_guard ON background_tasks")
    _legacy_write(setup.cursor(), session_id, "upgrade-race", "insert")
    ddl, writer = connect(), connect()
    error, thread = [], None
    try:
        cursor = ddl.cursor()
        background_tasks.create_order_guard(cursor)
        def write():
            try:
                with writer:
                    _legacy_write(writer.cursor(), session_id, "upgrade-race", "upsert")
            except Exception as exc:
                error.append(exc)
        thread = threading.Thread(target=write)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = setup.cursor()
            probe.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (writer.get_backend_pid(),))
            if probe.fetchone()[0] == "Lock":
                break
            time.sleep(.01)
        else:
            pytest.fail("old writer crossed the uncommitted guard install")
        database._backfill_legacy_task_turn_bounds(cursor)
        ddl.commit()
        thread.join(5)
        assert not thread.is_alive() and not error
        saved = _saved_task(session_id, "upgrade-race")
        assert saved["turn_order"] is saved["turn_order_upper_bound"] is saved["turn_order_incarnation"] is None
    finally:
        ddl.rollback()
        if thread:
            thread.join(5)
        background_tasks.create_order_guard(setup.cursor())
        setup.close()
        ddl.close()
        writer.close()


def test_blocked_legacy_update_sees_current_admission_before_invalidating(connect, session_id):
    from app.db import background_tasks, turns

    old = turns.enqueue_turn(session_id, "user", prompt="old", profile_id="default")
    cached = {"task_id": "blocked-old", "session_id": session_id, "kind": "agent",
              "status": "running", "started_at": CLOCK, "remote_device_id": "remote",
              "turn_order": old["insert_order"]}
    background_tasks.upsert_background_task(cached)
    holder, writer = connect(), connect()
    error = []
    try:
        # A new-code metadata-only write holds the row without invalidating it.
        holder.cursor().execute("UPDATE background_tasks SET turn_order_write_nonce='holder' "
                                "WHERE task_id='blocked-old' AND session_id=%s", (session_id,))
        def write():
            try:
                with writer:
                    _legacy_write(writer.cursor(), session_id, "blocked-old", "update")
            except Exception as exc:
                error.append(exc)
        thread = threading.Thread(target=write)
        thread.start()
        observer = connect(autocommit=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                cursor = observer.cursor()
                cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (writer.get_backend_pid(),))
                if cursor.fetchone()[0] == "Lock":
                    break
                time.sleep(.01)
            else:
                pytest.fail("old writer never reached the held row")
            newer = turns.enqueue_turn(session_id, "user", prompt="during old write", profile_id="default")
        finally:
            observer.close()
        holder.commit()
        thread.join(5)
        assert not thread.is_alive() and not error
        assert _saved_task(session_id, "blocked-old")["turn_order_invalid_through"] >= newer["insert_order"]
        background_tasks.upsert_background_task({**cached, "turn_order": newer["insert_order"]})
        assert _saved_task(session_id, "blocked-old")["turn_order"] is None
    finally:
        holder.rollback()
        holder.close()
        writer.close()
