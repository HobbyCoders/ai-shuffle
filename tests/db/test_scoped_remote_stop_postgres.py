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


@pytest.fixture
async def message_child(session_id, monkeypatch):
    """Real remote birth/scope and parked turn; no provider or signed transport."""
    from app.core import remote_task, turn_scheduler
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import base, remote_tasks, turns

    owner = session_id + "-owner"
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (owner,))
        original = turns.enqueue_turn(session_id, "provider_agent", prompt="original work",
            profile_id="default", command_id=remote_task.COMMAND_PREFIX + session_id,
            payload={remote_task.REPORT_DEVICE_KEY: "parent",
                     remote_task.REPORT_KEY: {"parent_session_id": owner, "owner_session_id": owner}},
            _conn=conn)
        remote_tasks.record_child(session_id, "parent", conn=conn, input_order=original["insert_order"])
    scope = remote_task._answer(session_id, original, {}, created=True)["stop_scope"]
    assert turns.start_turn(original["id"]) is not None
    assert turns.park_turn(original["id"]) is not None
    # Network membership and provider execution are outside this DB/admission test.
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda _: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    yield original, scope, owner
    task_registry.clear_session(session_id)
    task_registry.clear_session(owner)
    await flush()


def _message_body(session_id, owner, message_id="followup", *, kind="alert", completion=None):
    from app.core import remote_task
    return {"version": remote_task.WIRE_VERSION, "to": "child", "worker_session_id": session_id,
            "sender_session_id": owner, "message_id": session_id + message_id, "text": "continue",
            "kind": kind, "known_completion_id": completion}


@pytest.mark.parametrize("resumable", [False, True])
async def test_checkout_refused_message_preserves_acknowledged_stop(message_child, session_id, monkeypatch, resumable):
    from app.core import remote_task, scoped_remote_stop, worker_checkout
    from app.db import remote_tasks, turns

    original, scope, owner = message_child
    completion = None
    if resumable:
        assert turns.settle_parked_turns(session_id) == 1
        completion = "completed-report"
        assert remote_tasks.owe_report(session_id, completion, {"status": "completed"})
    # Controlled admission fault: the real scheduler refuses before enqueue.
    restore = AsyncMock(return_value=(False, "assigned checkout branch mismatch"))
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", restore)
    body = _message_body(session_id, owner, completion=completion)
    with pytest.raises(ValueError, match="assigned checkout branch mismatch"):
        await remote_task.handle_task_message(body, "parent")
    restore.assert_awaited_once_with(session_id)
    assert turns.get_queued_turns(session_id) == []
    assert remote_tasks.message_answer(body["message_id"]) is None
    assert scoped_remote_stop.claim(session_id, scope, "parent")["accepted"] is True


@pytest.mark.parametrize("stopped", [False, True])
async def test_stop_overtakes_limit_resume_checkout_refusal(message_child, session_id, monkeypatch, stopped):
    from app.core import limit_resume, query_session_lifecycle, turn_scheduler, worker_checkout
    from app.core.task_registry import task_registry
    from app.db import wakeups, turns

    _, _, owner = message_child
    task_registry.start_task(owner, session_id, "agent", "waiting worker")
    wake = wakeups.create_wakeup(id=session_id + "-wake", session_id=session_id,
        prompt="automatic resume", fire_at=0, source=limit_resume.LIMIT_RESUME_SOURCE)
    assert wakeups.claim_wakeup(wake["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    async def refuse(_):
        entered.set()
        await release.wait()
        return False, "assigned checkout branch mismatch"
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", refuse)
    # Observe the notification and note boundaries; admission, Stop and status
    # mutations are real, as are the wake's automatically captured stop sequence.
    terminal, record = AsyncMock(), AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    monkeypatch.setattr(limit_resume, "_record", record)
    firing = asyncio.create_task(limit_resume._resume_worker(session_id, owner,
        {"limit_key": "test-limit"}, wake_stop_seq=wake["stop_seq"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if stopped:
            await query_session_lifecycle.enter_user_stop(session_id)
            await query_session_lifecycle._settle_stopped_task(owner, session_id)
            assert task_registry.get_task(owner, session_id)["status"] == "killed"
            assert turns.wake_cancelled_by_stop({"session_id": session_id,
                "payload": {"wake_stop_seq": wake["stop_seq"]}})
        release.set()
        assert await asyncio.wait_for(firing, 3) is None
        assert task_registry.get_task(owner, session_id)["status"] == ("killed" if stopped else "error")
        assert turns.get_queued_turns(session_id) == []
        assert [call.args[1] for call in record.await_args_list] == ["dropped", "dropped"]
        if stopped:
            terminal.assert_not_awaited()
            assert all(call.args[2]["reason"] == "user_stopped" for call in record.await_args_list)
        else:
            terminal.assert_awaited_once()
            assert terminal.await_args.args[1]["status"] == "error"
            assert all(call.args[2]["reason"] == "assigned checkout branch mismatch" for call in record.await_args_list)
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)


@pytest.mark.parametrize("path", ["running", "resumable", "pending_retry"])
async def test_admitted_message_advances_scope_once(message_child, session_id, monkeypatch, path):
    from app.core import remote_task, scoped_remote_stop, session_runtime
    from app.db import remote_tasks, turns

    _, old_scope, owner = message_child
    completion = None
    if path == "resumable":
        assert turns.settle_parked_turns(session_id) == 1
        completion = "completed-report"
        assert remote_tasks.owe_report(session_id, completion, {"status": "completed"})
    body = _message_body(session_id, owner, completion=completion)
    pending = (remote_tasks.reserve_message_scope(body["message_id"], session_id, "parent")
               if path == "pending_retry" else None)
    # No provider runs: stop at the real durable enqueue/steering boundary.
    steer = AsyncMock()
    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    answer = await remote_task.handle_task_message(body, "parent")
    scope = answer["stop_scope"]
    assert scope["generation"] == body["message_id"] and scope["epoch"] > old_scope["epoch"]
    if pending:
        assert scope == pending
    [queued] = turns.get_queued_turns(session_id)
    assert answer["turn_id"] == queued["id"]
    assert scoped_remote_stop.claim(session_id, old_scope, "parent") == {"accepted": False}
    assert await remote_task.handle_task_message(body, "parent") == {**answer, "duplicate": True}
    assert [turn["id"] for turn in turns.get_queued_turns(session_id)] == [queued["id"]]
    with pytest.raises(ValueError, match="another message"):
        await remote_task.handle_task_message(body, "other-device")
    assert remote_tasks.message_answer(body["message_id"])["direction"] == "child"
    assert scoped_remote_stop.claim(session_id, scope, "parent")["accepted"] is True


async def test_unknown_after_enqueue_keeps_reserved_scope(message_child, session_id, monkeypatch):
    from app.core import remote_task, scoped_remote_stop, session_runtime
    from app.db import remote_tasks, turns

    _, old_scope, owner = message_child
    body = _message_body(session_id, owner)
    # Fault AFTER actual enqueue is uncertain, not a definite checkout refusal.
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(side_effect=RuntimeError("delivery unknown")))
    with pytest.raises(RuntimeError, match="delivery unknown"):
        await remote_task.handle_task_message(body, "parent")
    assert len(turns.get_queued_turns(session_id)) == 1
    child = remote_tasks.get_child(session_id)
    assert child["scope_generation"] == body["message_id"]
    assert int(child["scope_epoch"]) > old_scope["epoch"]
    pending = remote_tasks.message_answer(body["message_id"])["answer"]
    assert pending["_scope_pending"] is True and "_scope_reservation" in pending
    assert scoped_remote_stop.claim(session_id, old_scope, "parent") == {"accepted": False}


async def test_progress_remains_held_without_scope_or_checkout_admission(message_child, session_id, monkeypatch):
    from app.core import remote_task, scoped_remote_stop, worker_checkout
    from app.db import remote_tasks, turns

    _, scope, owner = message_child
    restore = AsyncMock(side_effect=AssertionError("idle progress must not restore checkout"))
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", restore)
    answer = await remote_task.handle_task_message(_message_body(session_id, owner, kind="progress"), "parent")
    assert answer == {"status": "held", "turn_id": None, "duplicate": False}
    restore.assert_not_awaited()
    assert turns.get_queued_turns(session_id) == []
    child = remote_tasks.get_child(session_id)
    assert child["scope_generation"] == scope["generation"] and int(child["scope_epoch"]) == scope["epoch"]
    assert scoped_remote_stop.claim(session_id, scope, "parent")["accepted"] is True


async def test_local_input_during_refused_checkout_is_not_erased(message_child, session_id, monkeypatch):
    from app.core import remote_task, scoped_remote_stop, turn_manager, worker_checkout
    from app.db import turns

    _, scope, owner = message_child
    entered, release = asyncio.Event(), asyncio.Event()
    async def refuse(_):
        entered.set()
        await release.wait()
        return False, "assigned checkout branch mismatch"
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", refuse)
    message = asyncio.create_task(remote_task.handle_task_message(_message_body(session_id, owner), "parent"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        local = await asyncio.to_thread(turn_manager.submit, session_id, "user",
            prompt="new human instruction", profile_id="default", no_kick=True)
        release.set()
        with pytest.raises(ValueError, match="assigned checkout branch mismatch"):
            await asyncio.wait_for(message, 3)
        assert turns.get_turn(local)["state"] == "queued"
        assert scoped_remote_stop.claim(session_id, scope, "parent") == {"accepted": False}
        assert turns.get_turn(local)["state"] == "queued"
    finally:
        release.set()
        await asyncio.gather(message, return_exceptions=True)


async def test_definite_refusal_can_retry_then_finalize_once(message_child, session_id, monkeypatch):
    from app.core import remote_task, session_runtime, worker_checkout
    from app.db import remote_tasks, turns

    _, _, owner = message_child
    restore = AsyncMock(side_effect=[(False, "assigned checkout branch mismatch"), (True, None)])
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", restore)
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock())
    body = _message_body(session_id, owner)
    with pytest.raises(ValueError, match="assigned checkout branch mismatch"):
        await remote_task.handle_task_message(body, "parent")
    assert turns.get_queued_turns(session_id) == []
    answer = await remote_task.handle_task_message(body, "parent")
    assert answer["duplicate"] is False
    assert await remote_task.handle_task_message(body, "parent") == {**answer, "duplicate": True}
    assert len(turns.get_queued_turns(session_id)) == 1
    assert restore.await_count == 2
    assert remote_tasks.message_answer(body["message_id"])["answer"] == {k: v for k, v in answer.items() if k != "duplicate"}


@pytest.mark.parametrize("guard", ["newer_scope", "local_input", "stop", "finalized", "pending_retry"])
async def test_fresh_reservation_undo_cannot_erase_newer_authority(message_child, session_id, guard):
    from app.core import turn_manager
    from app.db import remote_tasks, turns

    _, _, owner = message_child
    mid = session_id + "-fresh"
    scope, proof = remote_tasks.reserve_message_scope_for_delivery(mid, session_id, "parent")
    assert proof is not None
    if guard == "newer_scope":
        remote_tasks.reserve_message_scope(mid + "-newer", session_id, "parent")
    elif guard == "local_input":
        turn_manager.submit(session_id, "user", prompt="new local work", profile_id="default", no_kick=True)
    elif guard == "stop":
        turns.read_stop_receipt(session_id)
    elif guard == "finalized":
        assert remote_tasks.record_message(mid, worker_session_id=session_id, from_device_id="parent",
            direction="child", answer={"status": "delivered", "turn_id": None, "stop_scope": scope})
    else:
        retry_scope, retry_proof = remote_tasks.reserve_message_scope_for_delivery(mid, session_id, "parent")
        assert retry_scope == scope and retry_proof is None
    before, answer = remote_tasks.get_child(session_id), remote_tasks.message_answer(mid)
    assert remote_tasks.undo_refused_message_scope(proof) is False
    assert remote_tasks.get_child(session_id) == before
    assert remote_tasks.message_answer(mid) == answer


async def test_refused_pending_retry_never_rewinds_its_original_reservation(message_child, session_id, monkeypatch):
    from app.core import remote_task, scoped_remote_stop, worker_checkout
    from app.db import remote_tasks, turns

    _, original, owner = message_child
    body = _message_body(session_id, owner)
    scope, proof = remote_tasks.reserve_message_scope_for_delivery(body["message_id"], session_id, "parent")
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", AsyncMock(return_value=(False, "checkout mismatch")))
    with pytest.raises(ValueError, match="checkout mismatch"):
        await remote_task.handle_task_message(body, "parent")
    assert turns.get_queued_turns(session_id) == []
    assert remote_tasks.undo_refused_message_scope(proof) is False  # retry revoked even the first caller's proof
    assert scoped_remote_stop.claim(session_id, original, "parent") == {"accepted": False}
    assert scoped_remote_stop.claim(session_id, scope, "parent")["accepted"] is True


async def test_fresh_undo_removes_only_its_own_pending_intent(message_child, session_id):
    from app.db import remote_tasks

    unrelated = session_id + "-unrelated"
    remote_tasks.reserve_message_scope(unrelated, session_id, "parent")
    prior = remote_tasks.get_child(session_id)
    pending = remote_tasks.message_answer(unrelated)
    mid = session_id + "-refused"
    _, proof = remote_tasks.reserve_message_scope_for_delivery(mid, session_id, "parent")
    assert remote_tasks.undo_refused_message_scope(proof) is True
    assert remote_tasks.get_child(session_id) == prior
    assert remote_tasks.message_answer(mid) is None
    assert remote_tasks.message_answer(unrelated) == pending
    assert remote_tasks.undo_refused_message_scope(proof) is False


@pytest.mark.parametrize("engine_refuses", [False, True])
async def test_live_delivery_or_definite_fallback_refusal_keeps_truthful_scope(
        message_child, session_id, monkeypatch, engine_refuses):
    from types import SimpleNamespace
    from app.core import remote_task, scoped_remote_stop, session_pool, session_runtime, worker_checkout
    from app.db import remote_tasks, turns

    _, original, owner = message_child
    # Controlled live consumer: exercise actual deliver_message selection and
    # its RuntimeError (not accepted) fallback, without starting a provider.
    engine = SimpleNamespace(turn_running=True, submit=AsyncMock(
        side_effect=RuntimeError("idle") if engine_refuses else None))
    state = SimpleNamespace(adapter=SimpleNamespace(provider_kind="native", engine=engine))
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(get=lambda _: state))
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda _: object())
    restore = AsyncMock(return_value=(False, "checkout mismatch"))
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async", restore)
    body = _message_body(session_id, owner)
    if engine_refuses:
        with pytest.raises(ValueError, match="checkout mismatch"):
            await remote_task.handle_task_message(body, "parent")
        restore.assert_awaited_once()
        assert remote_tasks.message_answer(body["message_id"]) is None
        assert scoped_remote_stop.claim(session_id, original, "parent")["accepted"] is True
    else:
        answer = await remote_task.handle_task_message(body, "parent")
        assert answer["status"] == "delivered" and answer["turn_id"] is None
        restore.assert_not_awaited()
        assert answer["stop_scope"]["epoch"] > original["epoch"]
        assert scoped_remote_stop.claim(session_id, original, "parent") == {"accepted": False}
        assert await remote_task.handle_task_message(body, "parent") == {**answer, "duplicate": True}
    engine.submit.assert_awaited_once()
    assert turns.get_queued_turns(session_id) == []


@pytest.mark.parametrize("report_boundary", [1, 2])
async def test_stop_during_checkout_refusal_reporting_cannot_publish_error(
        message_child, session_id, monkeypatch, report_boundary):
    from app.core import limit_resume, query_session_lifecycle, turn_scheduler, worker_checkout
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import background_tasks, turns, wakeups

    _, _, owner = message_child
    task_registry.start_task(owner, session_id, "agent", "waiting worker")
    wake = wakeups.create_wakeup(id=session_id + "-report-wake", session_id=session_id,
        prompt="automatic resume", fire_at=0, source=limit_resume.LIMIT_RESUME_SOURCE)
    assert wakeups.claim_wakeup(wake["id"])
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async",
        AsyncMock(return_value=(False, "assigned checkout branch mismatch")))
    entered, release = asyncio.Event(), asyncio.Event()
    actual_record, calls = limit_resume._record, []
    async def report(sid, status, data):
        calls.append((sid, status, dict(data)))
        if len(calls) == report_boundary:
            entered.set()
            await release.wait()  # controlled logging I/O; actual _record still writes below
        await actual_record(sid, status, data)
    monkeypatch.setattr(limit_resume, "_record", report)
    terminal = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    firing = asyncio.create_task(limit_resume._resume_worker(session_id, owner,
        {"limit_key": "reporting-limit"}, wake_stop_seq=wake["stop_seq"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await query_session_lifecycle.enter_user_stop(session_id)
        await query_session_lifecycle._settle_stopped_task(owner, session_id)
        await flush()
        assert task_registry.get_task(owner, session_id)["status"] == "killed"
        assert background_tasks.get_background_task(session_id, owner)["status"] == "killed"
        assert turns.wake_cancelled_by_stop({"session_id": session_id,
            "payload": {"wake_stop_seq": wake["stop_seq"]}})
        release.set()
        assert await asyncio.wait_for(firing, 3) is None
        await flush()
        assert (task_registry.get_task(owner, session_id)["status"],
                background_tasks.get_background_task(session_id, owner)["status"]) == ("killed", "killed")
        terminal.assert_not_awaited()
        assert turns.get_queued_turns(session_id) == []
        assert all(status == "dropped" for _, status, _ in calls)
        assert [(sid, data["reason"]) for sid, _, data in calls[-2:]] == [
            (session_id, "user_stopped"), (owner, "user_stopped")]
        from app.db import base
        for sid in (session_id, owner):
            with base.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT payload FROM session_events WHERE session_id=%s "
                    "AND event_type='system.note' AND payload ->> 'subtype'=%s ORDER BY seq DESC LIMIT 1",
                    (sid, limit_resume.SYSTEM_SUBTYPE))
                note = cursor.fetchone()["payload"]["data"]
            assert note["status"] == "dropped" and note["reason"] == "user_stopped"
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)


async def test_stale_wake_stop_read_cannot_commit_after_stop_and_new_input(
        message_child, session_id, monkeypatch):
    from app.core import limit_resume, query_session_lifecycle, turn_manager, turn_scheduler, worker_checkout
    from app.core.task_registry import task_registry
    from app.core.task_persistence import flush
    from app.db import background_tasks, turns, wakeups

    _, _, owner = message_child
    task_registry.start_task(owner, session_id, "agent", "waiting worker")
    wake = wakeups.create_wakeup(id=session_id + "-stale-read-wake", session_id=session_id,
        prompt="automatic resume", fire_at=0, source=limit_resume.LIMIT_RESUME_SOURCE)
    assert wakeups.claim_wakeup(wake["id"])
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout_async",
        AsyncMock(return_value=(False, "assigned checkout branch mismatch")))
    snapshot, release = threading.Event(), threading.Event()
    actual_read, reads = turns.wake_cancelled_by_stop, []
    def delayed_read(turn):
        result = actual_read(turn)  # actual PostgreSQL snapshot, captured BEFORE Stop
        reads.append(result)
        snapshot.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(turns, "wake_cancelled_by_stop", delayed_read)
    terminal = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    firing = asyncio.create_task(limit_resume._resume_worker(session_id, owner,
        {"limit_key": "stale-read-limit"}, wake_stop_seq=wake["stop_seq"]))
    try:
        assert await asyncio.to_thread(snapshot.wait, 3)
        assert reads == [False]
        await query_session_lifecycle.enter_user_stop(session_id)
        await query_session_lifecycle._settle_stopped_task(owner, session_id)
        await flush()
        assert background_tasks.get_background_task(session_id, owner)["status"] == "killed"
        assert actual_read({"session_id": session_id, "payload": {"wake_stop_seq": wake["stop_seq"]}})
        # A genuine user input clears the latch, but must not revalidate this
        # already-cancelled automatic operation or lose its own queued input.
        new_input = await asyncio.to_thread(turn_manager.submit, session_id, "user",
            prompt="new human instruction", profile_id="default", no_kick=True)
        assert not task_registry.is_user_stopped(session_id)
        release.set()
        assert await asyncio.wait_for(firing, 3) is None
        await flush()
        assert (task_registry.get_task(owner, session_id)["status"],
                background_tasks.get_background_task(session_id, owner)["status"]) == ("killed", "killed")
        terminal.assert_not_awaited()
        assert [turn["id"] for turn in turns.get_queued_turns(session_id)] == [new_input]
        assert reads == [False]  # no repeated async read substituted for the commit fence
    finally:
        release.set()
        await asyncio.gather(firing, return_exceptions=True)
