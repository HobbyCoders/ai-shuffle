"""Recovery keeps accepted input identity; warning delivery needs a committed ack."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (
    cluster, connect, session_id,  # noqa: F401 - private-cluster fixtures
)

pytestmark = requires_real_pg


def _started(sid):
    from app.db import turns
    queued = turns.enqueue_turn(sid, "user", prompt="old", profile_id="default")
    return turns.start_turn(queued["id"], None, "old-owner")


def _notices(sid):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s "
                       "AND event_type='system.note' AND payload->>'subtype'='stop_incomplete'", (sid,))
        return list(cursor.fetchall())


@pytest.mark.parametrize("source", ["user", "queue", "agent_message", "promotion"])
@pytest.mark.parametrize("recovery", ["reap", "requeue"])
def test_absorbed_input_survives_reap_and_delayed_old_stop(session_id, source, recovery):
    from app.db import runs, turns
    old = _started(session_id)
    cutoff = turns.read_stop_receipt(session_id)
    turns.park_turn(old["id"])
    new = turns.enqueue_turn(session_id, source, prompt="new", profile_id="default",
                             payload={turns.PERSON_INPUT_KEY: True})
    run = runs.claim_run(session_id, "new-owner", ttl_seconds=-1)
    absorbed = turns.start_turn(new["id"], run["id"], "new-owner", can_resume=lambda *_: True)
    assert absorbed["id"] == old["id"]
    assert absorbed["payload"]["accepted_input_order"] == new["insert_order"]
    if recovery == "reap":
        assert any(row["id"] == old["id"] for row in turns.reap_expired())
    else:
        assert turns.requeue_turn(old["id"])["state"] == "queued"
    recovered = turns.start_turn(old["id"], None, "recovery-owner")
    assert recovered["payload"]["accepted_input_id"] == new["id"]
    assert recovered["payload"]["accepted_input_order"] == new["insert_order"]
    assert turns.interrupt_turn(session_id, "stop", stop_background=True,
                                turn_id=old["id"], stop_receipt=cutoff) is None
    assert turns.get_turn(old["id"])["state"] == "running"
    current = turns.read_stop_receipt(session_id)
    assert turns.interrupt_turn(session_id, "stop", turn_id=old["id"],
                                stop_receipt=current)["state"] == "interrupting"


def test_copied_payload_is_new_admission_not_recovery(session_id):
    from app.db import turns
    old = _started(session_id)
    turns.park_turn(old["id"])
    second = turns.enqueue_turn(session_id, "user", prompt="second", profile_id="default",
                                payload={turns.PERSON_INPUT_KEY: True})
    absorbed = turns.start_turn(second["id"], None, "second", can_resume=lambda *_: True)
    cutoff = turns.read_stop_receipt(session_id)
    turns.park_turn(absorbed["id"])
    third = turns.enqueue_turn(session_id, "user", prompt="third", profile_id="default",
                               payload=absorbed["payload"])
    running = turns.start_turn(third["id"], None, "third", can_resume=lambda *_: True)
    assert running["payload"]["accepted_input_order"] == third["insert_order"]
    assert third["insert_order"] > second["insert_order"]
    assert turns.interrupt_turn(session_id, "stop", turn_id=old["id"], stop_receipt=cutoff) is None


async def test_deliver_stops_keeps_notice_authority_after_failed_write(session_id, monkeypatch):
    from app.core import remote_task
    from app.core.task_registry import USER_STOP_REASON
    from app.db import remote_tasks, session_events
    child = uuid4().hex
    remote_tasks.record_dispatch(worker_session_id=child, target_device_id="old-worker",
        owner_session_id=session_id, root_session_id=session_id, tool_use_id=None,
        mode="background", label="legacy", agent="agent")
    remote_tasks.request_stop(child, USER_STOP_REASON)
    send = AsyncMock(side_effect=AssertionError("must not use a stable-id Stop"))
    monkeypatch.setattr(remote_task, "_send_stops", send)
    real_append = session_events.append_events
    def failed(*args, **kwargs):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(session_events, "append_events", failed)
    await remote_task.deliver_stops()
    assert _notices(session_id) == []
    assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_INCOMPLETE_NOTICE
    monkeypatch.setattr(session_events, "append_events", real_append)
    await remote_task.deliver_stops()
    await remote_task.deliver_stops()
    assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_INCOMPLETE
    assert len(_notices(session_id)) == 1
    send.assert_not_awaited()


async def test_receipt_is_not_consumed_without_durable_warning(session_id, monkeypatch):
    from app.core import query_session_lifecycle, stop_receipts as consumer
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import session_events, stop_receipts, turns
    old = _started(session_id)
    task_registry.note_turn_live(session_id, old["insert_order"])
    task_registry.start_task(session_id, "remote-child", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    turns.read_stop_receipt(session_id)
    row = stop_receipts.pending("old-owner", turn_id=old["id"])[0]
    attempt = AsyncMock(wraps=query_session_lifecycle.stop_all_background_tasks)
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", attempt)
    real_append = session_events.append_events
    def failed(*args, **kwargs):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(session_events, "append_events", failed)
    try:
        results = await consumer.consume(row)
        assert any(r.get("incomplete") and "Other worker" in r.get("detail", "") for r in results)
        assert task_registry.get_task(session_id, "remote-child")["status"] == "running"
        assert _notices(session_id) == []
        assert stop_receipts.get(row["id"])["consumed_at"] is None
        monkeypatch.setattr(session_events, "append_events", real_append)
        # Resume under the same logical id before retry. Notice-only authority
        # must not re-execute cleanup, re-latch Stop, or interrupt this input.
        turns.park_turn(old["id"])
        newer = turns.enqueue_turn(session_id, "user", prompt="new", profile_id="default",
                                   payload={turns.PERSON_INPUT_KEY: True})
        running = turns.start_turn(newer["id"], None, "new-owner", can_resume=lambda *_: True)
        task_registry.clear_user_stop(session_id)
        task_registry.note_turn_live(session_id, newer["insert_order"])
        task_registry.complete_task(session_id, "remote-child", "completed")
        task_registry.start_task(session_id, "remote-child", "agent", "resumed remote work",
                                 remote_device_id="old-worker", worker_name="Other worker")
        incarnation = task_registry.task_incarnation(session_id, "remote-child")
        consumer._running.clear()
        await consumer.consume(stop_receipts.get(row["id"]))
        assert turns.get_turn(running["id"])["state"] == "running"
        assert turns.get_turn(running["id"])["payload"]["accepted_input_order"] == newer["insert_order"]
        assert stop_receipts.get(row["id"])["consumed_at"] is not None
        assert len(_notices(session_id)) == 1
        assert task_registry.get_task(session_id, "remote-child")["status"] == "running"
        assert task_registry.task_incarnation(session_id, "remote-child") == incarnation
        assert not task_registry.is_user_stopped(session_id)
        attempt.assert_awaited_once()
    finally:
        task_registry.clear_session(session_id)
        await flush()


@pytest.mark.parametrize("uncertain_commit", [False, True])
async def test_pending_or_uncertain_commit_is_not_a_durable_ack(session_id, monkeypatch, uncertain_commit):
    from app.core import scoped_remote_stop as scoped
    from app.db import stop_notices

    real_db = stop_notices.get_event_db
    reached_commit, release = threading.Event(), threading.Event()
    @contextmanager
    def paused_commit():
        with real_db() as conn:
            yield conn
            reached_commit.set()
            assert release.wait(5), "test did not release the transaction"
        if uncertain_commit:
            # WAL committed but the client lost the acknowledgement. A restart
            # sees the atomic delivered marker and does not duplicate the note.
            raise ConnectionError("injected lost commit acknowledgement")

    monkeypatch.setattr(stop_notices, "get_event_db", paused_commit)
    result = scoped.incomplete("child", "old-worker", "Other worker", "unknown enforcement")
    started = time.monotonic()
    try:
        acknowledged = await scoped.record_incomplete_note(session_id, [result], notice_key="delayed")
        assert acknowledged is False
        assert time.monotonic() - started < 2
        assert reached_commit.is_set()
        assert _notices(session_id) == []
        pending = stop_notices.pending()
        assert len(pending) == 1
        writer = scoped._notice_writes[pending[0]]
    finally:
        release.set()
    await asyncio.gather(writer, return_exceptions=True)
    monkeypatch.setattr(stop_notices, "get_event_db", real_db)
    assert await scoped.record_incomplete_note(session_id, [result], notice_key="delayed") is True
    assert stop_notices.pending() == []
    notes = _notices(session_id)
    assert len(notes) == 1
    text = notes[0]["payload"]["data"]["content"]
    assert "Other worker" in text and "STILL BE RUNNING" in text
    assert "Update" in text and "stop the work there" in text


async def test_locked_event_counter_keeps_durable_retry_authority(connect, session_id):
    from app.core import scoped_remote_stop as scoped
    from app.db import session_events, stop_notices

    session_events.append_events(session_id, [{"type": "system.note", "payload": {"subtype": "setup"}}])
    blocked = connect()
    try:
        blocked.cursor().execute("UPDATE session_event_counters SET next_seq=next_seq WHERE session_id=%s",
                                 (session_id,))
        started = time.monotonic()
        result = scoped.incomplete("child", "old-worker", "Other worker", "unknown enforcement")
        assert await scoped.record_incomplete_note(session_id, [result], notice_key="locked") is False
        assert time.monotonic() - started < 2
        assert len(stop_notices.pending()) == 1
        assert _notices(session_id) == []
    finally:
        blocked.rollback()
    await scoped.retry_incomplete_notes()
    await scoped.retry_incomplete_notes()
    assert stop_notices.pending() == []
    assert len(_notices(session_id)) == 1


async def test_locked_session_cannot_block_notice_authority_or_ack_failed_delivery(connect, session_id):
    from app.core import scoped_remote_stop as scoped
    from app.db import stop_notices
    blocked = connect()
    try:
        blocked.cursor().execute("UPDATE sessions SET title='uncommitted' WHERE id=%s", (session_id,))
        result = scoped.incomplete("child", "old-worker", "Other worker", "unknown enforcement")
        started = time.monotonic()
        # The ordinary event projection touches sessions. Delivery must time out,
        # not deadlock Stop or lose the independent, FK-free warning authority.
        assert await scoped.record_incomplete_note(session_id, [result], notice_key="no-fk") is False
        assert time.monotonic() - started < 2
        assert len(stop_notices.pending()) == 1
        assert _notices(session_id) == []
    finally:
        blocked.rollback()
    await scoped.retry_incomplete_notes()
    assert len(_notices(session_id)) == 1
    assert stop_notices.pending() == []


async def test_pending_notice_replayed_in_fresh_process_after_session_deletion(cluster, session_id, monkeypatch, tmp_path):
    from app.core import scoped_remote_stop as scoped
    from app.core import query_session_lifecycle, stop_receipts as consumer
    from app.db import session_events, sessions, stop_notices, stop_receipts, turns
    real_append = session_events.append_events
    def failed(*args, **kwargs):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(session_events, "append_events", failed)
    old = _started(session_id)
    turns.read_stop_receipt(session_id)
    row = stop_receipts.pending("old-owner", turn_id=old["id"])[0]
    result = scoped.incomplete("child", "old-worker", "Other worker", "unknown enforcement")
    attempt = AsyncMock(return_value=[result])
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", attempt)
    await consumer.consume(row)
    assert stop_receipts.get(row["id"])["consumed_at"] is None
    assert len(stop_notices.pending()) == 1
    monkeypatch.setattr(session_events, "append_events", real_append)
    sessions.delete_session(session_id)
    # Use a clean process and scratch DATA_DIR: no caller task, latch, pending
    # writer or mutable dispatch is available to manufacture an acknowledgement.
    script = """
import asyncio, json, sys
from app.core.config import settings
from app.db import base, stop_receipts
from app.core import query_session_lifecycle, stop_receipts as consumer
from app.core.scoped_remote_stop import retry_incomplete_notes
object.__setattr__(settings, 'get_database_connection', lambda: json.loads(sys.argv[1]))
base.init_pool(min_conn=1, max_conn=4)
called = []
async def forbidden_cleanup(*args, **kwargs):
    called.append(True)
    raise AssertionError('notice replay must not run Stop cleanup')
query_session_lifecycle.stop_all_background_tasks = forbidden_cleanup
async def replay():
    await retry_incomplete_notes()
    await consumer.consume(stop_receipts.get(sys.argv[2]))
    await consumer.consume(stop_receipts.get(sys.argv[2]))
    await retry_incomplete_notes()
try:
    asyncio.run(replay())
    assert not called
finally:
    base.close_pool()
"""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(TESTING="1", DATA_DIR=str(tmp_path / "child-data"), HOME=str(tmp_path),
               PYTHONPATH=str(Path(__file__).resolve().parents[2] / "fluid"))
    child = subprocess.run([sys.executable, "-c", script, json.dumps(cluster), row["id"]],
                           env=env, capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert stop_notices.pending() == []
    assert stop_receipts.get(row["id"])["consumed_at"] is not None
    assert len(_notices(session_id)) == 1
    attempt.assert_awaited_once()


async def test_idle_stop_preserves_failed_notice_without_cleanup_receipt(session_id, monkeypatch):
    from app.core import query_session_lifecycle, scoped_remote_stop as scoped
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import session_events, stop_notices, stop_receipts

    task_registry.start_task(session_id, "remote-child", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    real_append = session_events.append_events
    def failed(*args, **kwargs):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(session_events, "append_events", failed)
    try:
        results = await query_session_lifecycle.stop_all_background_tasks(session_id, user_stop=True)
        assert any(r.get("incomplete") and "Other worker" in r.get("detail", "") for r in results)
        assert task_registry.get_task(session_id, "remote-child")["status"] == "running"
        assert stop_receipts.pending("old-owner", turn_id="absent") == []
        assert len(stop_notices.pending()) == 1
        assert _notices(session_id) == []
        monkeypatch.setattr(session_events, "append_events", real_append)
        await scoped.retry_incomplete_notes()
        assert len(_notices(session_id)) == 1
        assert stop_notices.pending() == []
        assert task_registry.get_task(session_id, "remote-child")["status"] == "running"
    finally:
        task_registry.clear_session(session_id)
        await flush()


def test_notice_outbox_is_created_by_upgrade_migration(session_id):
    from app.db import base, database, stop_notices
    with base.get_db() as conn:
        # Only this suite's private cluster; exercise the real additive upgrade.
        conn.cursor().execute("DROP TABLE stop_incomplete_notices")
        database._run_column_migrations(conn.cursor())
    stop_notices.save(uuid4().hex, session_id, {"content": "pending"})
    assert len(stop_notices.pending()) == 1
    for notice_id in stop_notices.pending():
        assert stop_notices.deliver(notice_id)
