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


def _started(sid, owner="old-owner"):
    from app.db import turns
    queued = turns.enqueue_turn(sid, "user", prompt="old", profile_id="default")
    return turns.start_turn(queued["id"], None, owner)


def _notices(sid):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s "
                       "AND event_type='system.note' AND payload->>'subtype'='stop_incomplete'", (sid,))
        return list(cursor.fetchall())


@pytest.mark.parametrize("queue_order", ["fifo", "front", "position_tie", "legacy_time", "legacy_id"])
async def test_stop_receipt_preserves_queue_order_through_not_sent_events(session_id, queue_order):
    from app.core import stop_receipts as consumer
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import base, stop_receipts, turns

    owner = _started(session_id)
    first = turns.enqueue_turn(session_id, "queue", prompt="first", profile_id="default")
    second = turns.enqueue_turn(session_id, "user", prompt="second", profile_id="default")
    third = turns.enqueue_turn(session_id, "queue", prompt="third", profile_id="default",
                               at_front=queue_order in ("front", "position_tie"))
    expected = [first["id"], second["id"], third["id"]]
    if queue_order == "front":
        # A genuine front insertion differs from admission/heap order.
        expected = [third["id"], first["id"], second["id"]]
    elif queue_order == "position_tie":
        # Recovery keeps the old position: it ties the newer front insertion.
        # Admission order must break that tie, before the ordinary queued row.
        recovered_id = owner["id"]
        turns.requeue_turn(recovered_id)
        owner = turns.start_turn(second["id"], None, "old-owner")
        queued = turns.get_queued_turns(session_id)
        assert queued[0]["position"] == queued[1]["position"]
        assert queued[0]["insert_order"] < queued[1]["insert_order"]
        expected = [recovered_id, third["id"], first["id"]]
    elif queue_order.startswith("legacy_"):
        # Frozen legacy imports can have equal positions and zero ordinals.
        # Keep the genuine enqueued prompts; model only their old ordering keys.
        with base.get_db() as conn:
            cursor = conn.cursor()
            for index, row in enumerate((first, second, third)):
                day = 3 - index if queue_order == "legacy_time" else 1
                cursor.execute("UPDATE turns SET position=0, insert_order=0, created_at=%s WHERE id=%s",
                               (f"2000-01-0{day}T00:00:00+00:00", row["id"]))
        expected = list(reversed(expected)) if queue_order == "legacy_time" else sorted(expected)

    canonical = [row["id"] for row in turns.get_queued_turns(session_id)]
    assert canonical == expected
    receipt = turns.read_stop_receipt(session_id)
    [published] = stop_receipts.pending("old-owner", turn_id=owner["id"])
    newer = turns.enqueue_turn(session_id, "user", prompt="after Stop", profile_id="default",
                               payload={turns.PERSON_INPUT_KEY: True})
    assert newer["insert_order"] > receipt["input_cutoff"]
    assert newer["id"] not in receipt["queued"]
    try:
        # Real durable consumption quiesces the queue and writes message.user
        # in the receipt's sequence. Replaying the same authority is harmless.
        assert await consumer.consume(published) == []
        assert await consumer.consume(published) == []
        saved = stop_receipts.get(published["id"])
        assert saved["consumed_at"] is not None
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT turn_id, seq, payload FROM session_events WHERE session_id=%s "
                           "AND event_type='message.user' ORDER BY seq", (session_id,))
            messages = list(cursor.fetchall())
            cursor.execute("SELECT stop_seq FROM session_stops WHERE session_id=%s", (session_id,))
            assert cursor.fetchone()["stop_seq"] == receipt["stop_seq"] == 1
        message_order = [row["turn_id"] for row in messages]
        assert message_order == saved["receipt"]["queued"] == receipt["queued"]
        assert message_order == canonical
        assert len({row["seq"] for row in messages}) == len(canonical)
        for message in messages:
            turn = turns.get_turn(message["turn_id"])
            assert turn["state"] == "aborted"
            assert message["payload"]["not_sent"] is True
            assert message["payload"]["queued_turn_id"] == turn["id"]
            assert message["payload"]["content"] == turn["prompt"]
        assert [row["id"] for row in turns.get_queued_turns(session_id)] == [newer["id"]]
    finally:
        task_registry.clear_session(session_id)
        await flush()


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


@pytest.mark.parametrize("commit_uncertain", [False, True])
async def test_idle_stop_retries_initial_notice_save_until_committed(session_id, monkeypatch, commit_uncertain):
    from psycopg2 import OperationalError
    from app.core import query_session_lifecycle
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import stop_notices, stop_receipts

    task_registry.start_task(session_id, "initial-save-child", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    real_save = stop_notices.save
    attempts = []
    def fail_first_save(*args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 1:
            if commit_uncertain:
                real_save(*args, **kwargs)
            raise OperationalError("injected initial outbox connection failure")
        return real_save(*args, **kwargs)
    monkeypatch.setattr(stop_notices, "save", fail_first_save)
    try:
        started = time.monotonic()
        results = await query_session_lifecycle.stop_all_background_tasks(session_id, user_stop=True)
        assert time.monotonic() - started < 2
        assert any(r.get("incomplete") and "Other worker" in r.get("detail", "") for r in results)
        assert task_registry.get_task(session_id, "initial-save-child")["status"] == "running"
        assert stop_receipts.pending("old-owner", turn_id="absent") == []
        assert len(attempts) == 2, "an initial save failure must not discard warning-only retry authority"
        assert attempts[0] == attempts[1]
        notes = _notices(session_id)
        assert len(notes) == 1
        assert "Other worker" in notes[0]["payload"]["data"]["content"]
        assert "STILL BE RUNNING" in notes[0]["payload"]["data"]["content"]
        assert stop_notices.pending() == []
    finally:
        task_registry.clear_session(session_id)
        await flush()


@pytest.mark.parametrize("first_owner", ["runtime", "observer"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_receipt_single_flight_owns_cleanup_result_not_its_caller(
        session_id, monkeypatch, first_owner, cancel_caller):
    from app.core import query_session_lifecycle, scoped_remote_stop as scoped, stop_receipts as consumer
    from app.db import stop_receipts, turns

    owner = "flight-" + uuid4().hex
    old = _started(session_id, owner)
    receipt = turns.read_stop_receipt(session_id)
    row = stop_receipts.pending(owner, turn_id=old["id"])[0]
    entered, release_cleanup = asyncio.Event(), asyncio.Event()
    caller_received, release_tail, endpoint_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, caller_results = [], []
    async def cleanup(*args, **kwargs):
        calls.append(kwargs["receipt"])
        entered.set()
        await release_cleanup.wait()
        turns.settle_turn(old["id"], "aborted", "interrupted")
        return [{"ok": True, "task_id": "captured"}]
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", cleanup)
    # The endpoint joins the same already-published Stop, not another click.
    monkeypatch.setattr(scoped, "claim", lambda *args: {
        "accepted": True, "receipt": receipt, "turn_id": old["id"]})
    real_consume = consumer.consume
    endpoint = None
    async def observed_consume(*args, **kwargs):
        if asyncio.current_task() is endpoint:
            endpoint_waiting.set()
        return await real_consume(*args, **kwargs)
    monkeypatch.setattr(consumer, "consume", observed_consume)
    async def runtime_caller():
        caller_results.append(await consumer.consume(row))
        caller_received.set()
        await release_tail.wait()  # unrelated caller work must never own cleanup
        return None

    if first_owner == "observer":
        await consumer.observe(owner)
        await asyncio.wait_for(entered.wait(), 2)
    caller = asyncio.create_task(runtime_caller())
    await asyncio.wait_for(entered.wait(), 2)
    endpoint = asyncio.create_task(scoped.handle({"version": 1, "worker_session_id": session_id,
        "scope": {"generation": "captured", "epoch": 1}}, "parent"))
    try:
        await asyncio.wait_for(endpoint_waiting.wait(), 2)
        await consumer.observe(owner)
        if cancel_caller:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
        release_cleanup.set()
        if not cancel_caller:
            await asyncio.wait_for(caller_received.wait(), 2)
        completed, _ = await asyncio.wait({endpoint}, timeout=1)
        release_tail.set()
        answers = await asyncio.wait_for(asyncio.gather(caller, endpoint, return_exceptions=True), 2)
        assert isinstance(answers[1], dict), f"endpoint received its caller's result instead of cleanup: {answers[1]!r}"
        assert answers[1] == {"scope_enforced": True, "ok": True}
        assert endpoint in completed, "receipt consumption waited for unrelated caller work"
        assert len(calls) == 1
        if not cancel_caller:
            assert caller_results == [[{"ok": True, "task_id": "captured"}]]
        assert stop_receipts.get(row["id"])["consumed_at"] is not None
    finally:
        release_cleanup.set()
        release_tail.set()
        await asyncio.gather(caller, endpoint, return_exceptions=True)


@pytest.mark.parametrize("owner_state", ["running", "parked"])
async def test_actual_stop_with_owner_receipt_emits_one_warning_per_stop(session_id, monkeypatch, owner_state):
    from app.core import query_session_lifecycle, stop_receipts as consumer
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import stop_receipts, turns

    old = _started(session_id)
    task_registry.note_turn_live(session_id, old["insert_order"])
    task_registry.start_task(session_id, "unknown-worker", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    if owner_state == "parked":
        turns.park_turn(old["id"])
    try:
        # Real Stop admission/cleanup publishes the owner's receipt itself.
        # In old/no-RPC mode work remains running; no transport retry is involved.
        first = await query_session_lifecycle.stop_all_background_tasks(session_id, user_stop=True)
        assert any(r.get("incomplete") for r in first)
        pending = stop_receipts.pending("old-owner", turn_id=old["id"])
        assert len(pending) == 1
        first_receipt = pending[0]
        await consumer.consume(first_receipt)
        notes = _notices(session_id)
        assert len(notes) == 1, "one Stop produced both a generic/idle and receipt warning"
        assert notes[0]["payload"]["data"]["stop_receipt_id"] == first_receipt["id"]
        assert "Other worker" in notes[0]["payload"]["data"]["content"]
        assert task_registry.get_task(session_id, "unknown-worker")["status"] == "running"
        # A genuinely second Stop is not deduplicated with its predecessor.
        await query_session_lifecycle.stop_all_background_tasks(session_id, user_stop=True)
        second = stop_receipts.pending("old-owner", turn_id=old["id"])
        if owner_state == "running":
            [second_receipt] = second
            assert second_receipt["stop_seq"] > first_receipt["stop_seq"]
            await consumer.consume(second_receipt)
            second_id = second_receipt["id"]
        else:
            # The first Stop quiesced the parked owner. The genuine second
            # Stop is now idle and therefore owns its warning directly.
            assert second == []
            second_id = None
        notes = _notices(session_id)
        assert len(notes) == 2
        assert {note["payload"]["data"].get("stop_receipt_id") for note in notes} == {
            first_receipt["id"], second_id}
    finally:
        task_registry.clear_session(session_id)
        await flush()


@pytest.mark.parametrize("interruption", ["caller_cancel", "writer_cancel", "outbox_read_failure"])
async def test_unsaved_idle_warning_survives_cancellation_without_repeating_stop(
        session_id, monkeypatch, interruption):
    from psycopg2 import OperationalError
    from app.core import query_session_lifecycle, scoped_remote_stop as scoped, turn_manager
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import stop_notices, turns

    task_registry.start_task(session_id, "retry-child", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    real_save, real_pending = stop_notices.save, stop_notices.pending
    allow_save, first_attempt = threading.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append((args[0], time.monotonic()))
        loop.call_soon_threadsafe(first_attempt.set)
        if not allow_save.is_set():
            raise OperationalError("initial outbox temporarily unavailable")
        return real_save(*args, **kwargs)
    monkeypatch.setattr(stop_notices, "save", unavailable)
    cleanup = AsyncMock(wraps=query_session_lifecycle.stop_all_background_tasks)
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", cleanup)
    producer = asyncio.create_task(query_session_lifecycle.stop_all_background_tasks(session_id, user_stop=True))
    notice_id = None
    try:
        await asyncio.wait_for(first_attempt.wait(), 2)
        started = time.monotonic()
        if interruption == "caller_cancel":
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        else:
            await asyncio.wait_for(asyncio.shield(producer), 2)
            assert len(attempts) >= 2
            assert attempts[1][1] - attempts[0][1] >= scoped.NOTICE_SAVE_RETRY * .8
        assert time.monotonic() - started < 2
        notice_id = attempts[0][0]
        assert notice_id in scoped._notice_saves
        assert real_pending() == [] and _notices(session_id) == []
        if interruption != "caller_cancel":
            writer = scoped._notice_writes[notice_id]
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            assert notice_id in scoped._notice_saves
        # Actual new input producer releases Stop, then the task resumes under a
        # newer admission/incarnation while only a warning save remains owed.
        new_id = await asyncio.to_thread(turn_manager.submit, session_id, "user", prompt="new instructions",
                                         profile_id="default", no_kick=True)
        running = turns.start_turn(new_id, None, "new-owner")
        task_registry.note_turn_live(session_id, running["insert_order"])
        task_registry.complete_task(session_id, "retry-child", "completed")
        task_registry.start_task(session_id, "retry-child", "agent", "new work",
                                 remote_device_id="old-worker", worker_name="Other worker")
        incarnation = task_registry.task_incarnation(session_id, "retry-child")
        generation = task_registry.stop_generation(session_id)
        assert not task_registry.is_user_stopped(session_id)
        if interruption == "outbox_read_failure":
            def failed_read():
                raise OperationalError("outbox read also temporarily unavailable")
            monkeypatch.setattr(stop_notices, "pending", failed_read)
        allow_save.set()
        await scoped.retry_incomplete_notes()
        monkeypatch.setattr(stop_notices, "pending", real_pending)
        assert await scoped._notice_ack(notice_id) is True
        await scoped.retry_incomplete_notes()
        assert notice_id not in scoped._notice_saves
        assert len(_notices(session_id)) == 1
        assert real_pending() == []
        assert task_registry.get_task(session_id, "retry-child")["status"] == "running"
        assert task_registry.task_incarnation(session_id, "retry-child") == incarnation
        assert task_registry.stop_generation(session_id) == generation
        assert not task_registry.is_user_stopped(session_id)
        assert turns.get_turn(new_id)["state"] == "running"
        cleanup.assert_awaited_once()
    finally:
        allow_save.set()
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        monkeypatch.setattr(stop_notices, "pending", real_pending)
        if notice_id is not None:
            await scoped._notice_ack(notice_id)
        task_registry.clear_session(session_id)
        await flush()


async def test_legacy_runtime_user_stop_fallback_does_not_add_a_second_warning(session_id, monkeypatch):
    from app.core import session_runtime
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import turns

    queued = turns.enqueue_turn(session_id, "user", prompt="legacy owner", profile_id="default")
    running = turns.start_turn(queued["id"], None, None)
    task_registry.note_turn_live(session_id, running["insert_order"])
    task_registry.start_task(session_id, "legacy-child", "agent", "remote work",
                             remote_device_id="old-worker", worker_name="Other worker")
    await flush()
    stopped = turns.interrupt_turn(session_id, "stop", True, running["id"], source="user_stop")
    assert not stopped["payload"][turns.STOP_RECEIPT_KEY]["cleanup_receipts"]
    runtime = session_runtime.SessionRuntime(stopped)
    class BackgroundChecked(Exception):
        pass
    async def stop_after_background(work, label):
        assert label == "background-task stop"
        await work
        raise BackgroundChecked
    monkeypatch.setattr(runtime, "_run_interrupt_step", stop_after_background)
    try:
        # Run the actual legacy fallback closure, stopping before provider
        # signalling: no provider/pump is involved in this warning producer.
        with pytest.raises(BackgroundChecked):
            await runtime._drive_interrupt("stop", escalation_timeout=None, source="user_stop")
        notes = _notices(session_id)
        assert len(notes) == 1, "runtime fallback duplicated the lifecycle-owned Stop warning"
        assert "Other worker" in notes[0]["payload"]["data"]["content"]
    finally:
        task_registry.clear_session(session_id)
        await flush()


@pytest.mark.parametrize("quarantine_race", [False, True])
async def test_fresh_individual_stop_persists_positive_retry_authority(session_id, monkeypatch, quarantine_race):
    from app.core import provider_agent_supervisor as supervisor, remote_task
    from app.core.query_session_lifecycle import stop_session_task
    from app.core.task_persistence import flush
    from app.core.task_registry import task_registry
    from app.db import remote_tasks
    from app.network import control

    child = "explicit-" + uuid4().hex
    remote_tasks.record_dispatch(worker_session_id=child, target_device_id="offline-owner",
        owner_session_id=session_id, root_session_id=session_id, tool_use_id=child,
        mode="background", label="Remote job", agent="Default", target_name="Other worker")
    supervisor.register_worker(supervisor.WorkerRecord(
        parent_session_id=session_id, worker_session_id=child, task_id=child,
        tool_use_id=child, remote_device_id="offline-owner"))
    task_registry.start_task(session_id, child, "agent", "Remote job", tool_use_id=child,
        remote_device_id="offline-owner", worker_name="Other worker")
    calls = []
    reachable = False
    async def peer(device, action, body, **kwargs):
        calls.append((device, action, body))
        if not reachable:
            raise control.RunnerUnreachable("controlled offline owner")
        return {"children": {child: {"state": remote_task.CHILD_STOPPED}}}
    monkeypatch.setattr(control, "call", peer)
    entered, release = threading.Event(), threading.Event()
    old_delivery = None
    try:
        # Model the actual pre-upgrade row: no backfilled positive provenance.
        old = remote_tasks.request_stop(child, "user_stopped")
        assert old["stop_origin"] is None
        if quarantine_race:
            real_defer = remote_tasks.defer_unscoped_user_stop
            def delayed_defer(*args, **kwargs):
                entered.set()
                assert release.wait(3)
                return real_defer(*args, **kwargs)
            monkeypatch.setattr(remote_tasks, "defer_unscoped_user_stop", delayed_defer)
            old_delivery = asyncio.create_task(remote_task.deliver_stops())
            assert await asyncio.to_thread(entered.wait, 2)
        else:
            assert await remote_task.deliver_stops() == 0
            assert calls == []
            assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_INCOMPLETE
            assert len(_notices(session_id)) == 1

        answer = await stop_session_task(session_id, child)
        assert answer["ok"] and "sent again when it answers" in answer["detail"]
        row = remote_tasks.get_dispatch(child)
        assert row["stop_origin"] == remote_tasks.EXPLICIT_TASK_STOP
        assert row["stop_requested_at"] == old["stop_requested_at"]
        assert row["stop_reason"] == old["stop_reason"] == "user_stopped"
        assert row["stop_state"] == remote_tasks.STOP_PENDING
        if old_delivery is not None:
            release.set()
            assert await asyncio.wait_for(old_delivery, 3) == 0
            assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_PENDING
        await flush()
        supervisor.reset()
        task_registry.clear_session(session_id)  # no in-memory replay authority
        assert await remote_task.deliver_stops() == 0
        [pending] = [r for r in remote_tasks.pending_stops() if r["worker_session_id"] == child]
        assert pending["stop_origin"] == remote_tasks.EXPLICIT_TASK_STOP
        reachable = True
        assert await remote_task.deliver_stops() == 1
        assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_SENT
        assert await remote_task.deliver_stops() == 0
        assert all(action == remote_task.TASK_STOP_ACTION for _, action, _ in calls)
        assert all("stop_origin" not in body and "explicit_task_stop" not in body for _, _, body in calls)
        assert len(_notices(session_id)) == (0 if quarantine_race else 1)
    finally:
        release.set()
        if old_delivery is not None:
            await asyncio.gather(old_delivery, return_exceptions=True)
        await remote_task.drain_stops()
        supervisor.reset()
        task_registry.clear_session(session_id)
        await flush()
