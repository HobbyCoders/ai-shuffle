"""Actual PostgreSQL producer/receiver boundaries for generation-scoped Stop."""
import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import cluster, session_id, connect  # noqa: F401

pytestmark = requires_real_pg


@pytest.fixture
def child(session_id, monkeypatch):
    from app.core import remote_task
    from app.db import base, remote_tasks, turns
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda _: None)  # signed auth tested separately
    turn = turns.enqueue_turn(session_id, "provider_agent", prompt="original remote work", profile_id="default")
    remote_tasks.init_schema()
    with base.get_db() as conn:
        remote_tasks.record_child(session_id, "parent", conn=conn, input_order=turn["insert_order"])
    return turn, {"generation": session_id, "epoch": 1}


def test_claim_rejects_an_actual_new_message_generation(child, session_id):
    from app.core import scoped_remote_stop
    from app.db import remote_tasks
    _, scope = child
    newer = remote_tasks.reserve_message_scope("new-message", session_id, "parent")
    assert newer["epoch"] > scope["epoch"]
    assert scoped_remote_stop.claim(session_id, scope, "parent") == {"accepted": False}
    assert remote_tasks.get_child(session_id)["stop_requested_at"] is None


def test_repeatable_stop_receipt_and_update_spare_concurrent_logical_resume(child, session_id, monkeypatch):
    from app.core import scoped_remote_stop, turn_manager
    from app.db import stop_receipts, turns
    queued, scope = child
    old = turns.start_turn(queued["id"], None, "old-owner")
    snapshot, resume = threading.Event(), threading.Event()
    original = turns._stop_receipt
    def gate(cursor, sid):
        snapshot.set()
        assert resume.wait(5)
        return original(cursor, sid)
    monkeypatch.setattr(turns, "_stop_receipt", gate)
    results, errors = [], []
    def stop():
        try:
            results.append(scoped_remote_stop.claim(session_id, scope, "parent"))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=stop)
    thread.start()
    try:
        assert snapshot.wait(5)
        turns.park_turn(old["id"])
        new_id = turn_manager.submit(session_id, "user", prompt="a new human instruction", no_kick=True)
        new = turns.get_turn(new_id)
        started = turns.start_turn(new_id, None, "new-owner", can_resume=lambda *_: True)
        assert started["id"] == old["id"]
        assert started["payload"]["accepted_input_order"] == new["insert_order"]
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    [claimed] = results
    assert claimed["accepted"] and claimed["receipt"]["input_cutoff"] == old["insert_order"]
    assert claimed["turn_id"] == old["id"]
    assert turns.interrupt_turn(session_id, "stop", True, old["id"],
        source="user_stop", stop_receipt=claimed["receipt"]) is None
    current = turns.get_turn(old["id"])
    assert current["state"] == "running" and current["instance_id"] == "new-owner"
    assert current["payload"]["accepted_input_order"] == new["insert_order"]
    pending = stop_receipts.pending("old-owner", turn_id=old["id"])
    assert len(pending) == 1 and pending[0]["input_cutoff"] == old["insert_order"]
    assert stop_receipts.pending("new-owner", turn_id=old["id"]) == []


def test_pending_message_retry_cannot_replace_a_newer_incarnation(child, session_id):
    from app.db import remote_tasks
    first = remote_tasks.reserve_message_scope("pending-old", session_id, "parent")
    second = remote_tasks.reserve_message_scope("pending-new", session_id, "parent")
    assert second["epoch"] > first["epoch"]
    with pytest.raises(ValueError, match="superseded"):
        remote_tasks.reserve_message_scope("pending-old", session_id, "parent")
    assert remote_tasks.get_child(session_id)["scope_generation"] == "pending-new"
    assert remote_tasks.message_answer("pending-old")["answer"] == {"_scope_pending": True}
    answer = {"status": "queued", "stop_scope": second}
    assert remote_tasks.record_message("pending-new", worker_session_id=session_id,
        from_device_id="parent", direction="child", answer=answer) is True
    assert remote_tasks.message_answer("pending-new")["answer"] == answer


def test_stale_projection_cannot_pair_new_generation_with_old_admission(child, session_id):
    from app.db import background_tasks, turns
    old, scope = child
    snapshot = {"task_id": "projection", "session_id": session_id, "kind": "agent", "status": "running",
                "started_at": "2021-01-01T00:00:00Z", "remote_device_id": "owner",
                "turn_order": old["insert_order"], "remote_stop_scope": scope}
    background_tasks.upsert_background_task(snapshot)
    newer = turns.enqueue_turn(session_id, "user", prompt="new parent intent", profile_id="default")
    scope2 = {"generation": "new-message", "epoch": scope["epoch"] + 1}
    background_tasks.upsert_background_task({**snapshot, "turn_order": newer["insert_order"], "remote_stop_scope": scope2})
    background_tasks.upsert_background_task(snapshot)
    row = background_tasks.get_background_task("projection", session_id)
    assert row["turn_order"] == newer["insert_order"]
    assert row["remote_stop_scope"] == scope2


@pytest.mark.parametrize("cancel_queued_caller", [False, True])
async def test_repeated_scoped_stop_reports_confirmed_runtime_settlement(
        child, session_id, monkeypatch, cancel_queued_caller):
    from app.core import query_session_lifecycle, scoped_remote_stop, session_runtime
    from app.core import stop_receipts as consumer, turn_manager
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import base, remote_tasks, turns
    from app.mcp.computer_use import abort, access

    queued, scope = child
    old = turns.start_turn(queued["id"], None, "receiver-owner")
    runtime = session_runtime.SessionRuntime(old)
    monkeypatch.setitem(session_runtime._live_runtimes, session_id, runtime)
    monkeypatch.setattr(abort, "request_stop", Mock())
    monkeypatch.setattr(access, "revoke", Mock())
    entered, release, duplicate_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def provider_stop(sid):
        assert sid == session_id
        entered.set()
        await release.wait()
        assert turns.settle_turn(old["id"], "aborted", "user_stop") is not None
    signal = AsyncMock(side_effect=provider_stop)
    monkeypatch.setattr(query_session_lifecycle, "interrupt_session", signal)
    cleanup = AsyncMock(wraps=consumer._consume)
    monkeypatch.setattr(consumer, "_consume", cleanup)
    cancel = Mock(wraps=runtime._cancel_pump)
    monkeypatch.setattr(runtime, "_cancel_pump", cancel)
    eviction = AsyncMock()
    monkeypatch.setattr(session_runtime, "_evict_pooled_client", eviction)
    body = {"version": 1, "worker_session_id": session_id, "scope": scope}
    first = asyncio.create_task(scoped_remote_stop.handle(body, "parent"))
    duplicate = None
    async def duplicate_request():
        duplicate_entered.set()
        return await scoped_remote_stop.handle(body, "parent")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # An unfinished driver has no terminal proof and must not re-signal,
        # start another escalation clock or cancel the provider's pump.
        assert await runtime.interrupt("stop", source="user_stop") == "interrupting"
        assert not runtime._interrupt_finished
        duplicate = asyncio.create_task(duplicate_request())
        await asyncio.wait_for(duplicate_entered.wait(), 2)
        assert not duplicate.done()  # identical endpoint request waits on its lock
        if cancel_queued_caller:
            duplicate.cancel()
            await asyncio.gather(duplicate, return_exceptions=True)
        release.set()
        assert await asyncio.wait_for(first, 3) == {"scope_enforced": True, "ok": True}
        assert runtime._interrupt_finished
        assert turns.get_turn(old["id"])["state"] == "aborted"
        if cancel_queued_caller:
            repeated = await asyncio.wait_for(scoped_remote_stop.handle(body, "parent"), 3)
        else:
            repeated = await asyncio.wait_for(duplicate, 3)
        assert repeated == {"scope_enforced": True, "ok": True}, (
            "same-boundary retry falsely reported incomplete after durable settlement", repeated)
        assert await runtime.interrupt("stop", source="user_stop") == "aborted"
        cleanup.assert_awaited_once()
        signal.assert_awaited_once_with(session_id)
        eviction.assert_not_awaited()
        cancel.assert_not_called()
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM session_events WHERE session_id=%s "
                           "AND event_type='system.note' AND payload->>'subtype'='stop_incomplete'", (session_id,))
            assert cursor.fetchone()["n"] == 0
        stop_stamp = remote_tasks.get_child(session_id)["stop_requested_at"]
        # A real newer human admission releases Stop. The old captured scope
        # must still fail closed without signalling or stopping that input.
        new_id = await asyncio.to_thread(turn_manager.submit, session_id, "user",
            prompt="newer receiver instruction", profile_id="default", no_kick=True)
        newer = turns.start_turn(new_id, None, "new-owner")
        generation = task_registry.stop_generation(session_id)
        assert not task_registry.is_user_stopped(session_id)
        assert await scoped_remote_stop.handle(body, "parent") == {
            "scope_enforced": True, "ok": False, "incomplete": True}
        assert turns.get_turn(newer["id"])["state"] == "running"
        assert task_registry.stop_generation(session_id) == generation
        assert not task_registry.is_user_stopped(session_id)
        assert remote_tasks.get_child(session_id)["stop_requested_at"] == stop_stamp
        cleanup.assert_awaited_once()
        signal.assert_awaited_once()
        cancel.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(first, *([duplicate] if duplicate is not None else []), return_exceptions=True)
        task_registry.clear_session(session_id)
        await flush()


@pytest.mark.parametrize("proof", ["nonterminal", "missing", "other_terminal", "newer_logical_terminal"])
async def test_repeated_interrupt_requires_its_own_accepted_terminal_row(child, session_id, monkeypatch, proof):
    from app.core import session_runtime, turn_manager
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import base, turns

    queued, _ = child
    old = turns.start_turn(queued["id"], None, "old-owner")
    runtime = session_runtime.SessionRuntime(old)
    assert turns.interrupt_turn(session_id, "stop", True, old["id"], source="user_stop") is not None
    # Finish the first driver without terminal proof, as asynchronous drain
    # teardown can do. The repeated-call path must not trust its old result.
    drive = AsyncMock(return_value="draining")
    orphan = AsyncMock()
    monkeypatch.setattr(runtime, "_drive_interrupt", drive)
    monkeypatch.setattr(runtime, "_end_orphaned_pump", orphan)
    assert await runtime.interrupt("stop", source="user_stop") == "draining"
    try:
        if proof == "missing":
            with base.get_db() as conn:
                conn.cursor().execute("DELETE FROM turns WHERE id=%s", (old["id"],))
        elif proof == "other_terminal":
            other = turns.enqueue_turn(session_id, "provider_agent", prompt="other work", profile_id="default")
            assert turns.settle_turn(other["id"], "completed") is not None
        elif proof == "newer_logical_terminal":
            assert turns.park_turn(old["id"]) is not None
            new_id = await asyncio.to_thread(turn_manager.submit, session_id, "user",
                prompt="new logical admission", profile_id="default", no_kick=True)
            resumed = turns.start_turn(new_id, None, "new-owner", can_resume=lambda *_: True)
            assert resumed["id"] == old["id"]
            assert resumed["payload"]["accepted_input_order"] > runtime.admission_order
            assert turns.settle_turn(resumed["id"], "completed") is not None
        assert await runtime.interrupt("stop", source="user_stop") == "interrupting"
        if proof == "newer_logical_terminal":
            assert turns.get_turn(old["id"])["state"] == "completed"
        drive.assert_awaited_once()
        orphan.assert_not_awaited()
    finally:
        task_registry.clear_session(session_id)
        await flush()
