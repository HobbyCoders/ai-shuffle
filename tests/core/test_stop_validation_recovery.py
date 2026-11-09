"""Stop validation failures must spare input, not the captured process kills."""
from unittest.mock import AsyncMock, Mock

import pytest
from psycopg2 import InterfaceError, OperationalError
from psycopg2.errors import LockNotAvailable, QueryCanceled

from app.core import query_session_lifecycle as lifecycle, stop_receipts as recovery
from app.core.task_registry import task_registry
from app.db import runs, session_events, stop_receipts as receipts, turns
from tests.core.test_recording_failure_stop import (  # noqa: F401
    SESSION, fail_recording, monitor, session, seed_session, clean_task_registry,
)


@pytest.mark.parametrize("error", [LockNotAvailable, QueryCanceled, OperationalError, InterfaceError, RuntimeError])
@pytest.mark.parametrize("durable", [False, True])
async def test_validation_failure_stops_captured_work_but_does_not_consume(monkeypatch, monitor, error, durable):
    _lease, turn, queued = fail_recording(monkeypatch)
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    scope = lifecycle.capture_stop_scope(SESSION)
    with monkeypatch.context() as blocked:
        blocked.setattr(turns, "apply_stop_receipt", Mock(side_effect=error("validation unavailable")))
        if durable:
            results = await recovery.consume(saved, scope=scope)
        else:
            results = await lifecycle.stop_all_background_tasks(SESSION, user_stop=True, scope=scope, receipt=receipt)
    assert monitor[0].returncode is not None
    assert any(result.get("retryable") for result in results)
    assert not task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(queued["id"])["state"] == "queued"
    assert receipts.get(saved["id"])["result"] is None
    assert receipts.get(saved["id"])["consumed_at"] is None
    await recovery.consume(saved, scope=scope)
    assert task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(queued["id"])["state"] == "aborted"
    assert receipts.get(saved["id"])["consumed_at"] is not None
    await recovery.consume(saved, scope=scope)
    unsent = [event for event in session_events.get_events(SESSION)
              if event["event_type"] == "message.user" and event["payload"].get("not_sent")]
    assert len(unsent) == 1


@pytest.mark.parametrize("stage", ["capture", "validation"])
@pytest.mark.parametrize("transport", ["ws", "peer"])
async def test_stop_entrypoints_report_storage_failure(monkeypatch, monitor, stage, transport):
    from app.api.chat_ws_handler import ChatWebSocketHandler
    from app.network import control

    _lease, turn, queued = fail_recording(monkeypatch)
    function = "capture_stop_receipt" if stage == "capture" else "apply_stop_receipt"
    with monkeypatch.context() as blocked:
        blocked.setattr(turns, function, Mock(side_effect=LockNotAvailable()))
        if transport == "ws":
            handler = ChatWebSocketHandler(websocket=Mock(), device_id="device", user_id="admin", api_user_id=None)
            handler._may_act_on_session = AsyncMock(return_value=True)
            handler._network_owner = AsyncMock(return_value=None)
            handler._v2_broadcast_queue = AsyncMock()
            handler.send_json = AsyncMock()
            await handler.handle_stop({"session_id": SESSION, "turn_id": turn["id"]})
            errors = [call.args[0] for call in handler.send_json.call_args_list
                      if call.args[0].get("type") == "error"]
            assert len(errors) == 1
            assert "Stop" in errors[0]["message"]
        else:
            blocked.setattr(control, "_require_owned", lambda *_: None)
            result = await control._interrupt({"session_id": SESSION, "mode": "stop",
                "stop_background": True, "turn_id": turn["id"]}, "source-worker")
            assert result["incomplete"] is True
            assert result["finished"] is False
    # Capture has not admitted ANY ownership, so killing its scope would risk a
    # successor. Validation has admitted a boundary, so captured kills still run.
    assert (monitor[0].returncode is None) is (stage == "capture")
    assert turns.get_turn(queued["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)


async def test_guard_release_failure_after_callback_keeps_validated_authority(monkeypatch, monitor):
    _lease, turn, queued = fail_recording(monkeypatch)
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    apply = turns.apply_stop_receipt
    def fail_after_validation(*args):
        apply(*args)
        raise OperationalError("connection lost releasing read-only guard")
    monkeypatch.setattr(turns, "apply_stop_receipt", fail_after_validation)
    results = await lifecycle.stop_all_background_tasks(SESSION, user_stop=True, receipt=receipt)
    assert monitor[0].returncode is not None
    assert not any(result.get("retryable") for result in results)
    assert task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(queued["id"])["state"] == "aborted"


async def test_queue_failure_after_validation_remains_retryable(monkeypatch, monitor):
    _lease, turn, queued = fail_recording(monkeypatch)
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    with monkeypatch.context() as blocked:
        blocked.setattr(turns, "stop_queued_turns", Mock(side_effect=OperationalError()))
        results = await recovery.consume(saved, scope=lifecycle.capture_stop_scope(SESSION))
    assert monitor[0].returncode is not None
    assert any(result.get("retryable") for result in results)
    assert receipts.get(saved["id"])["result"] is None
    assert receipts.get(saved["id"])["consumed_at"] is None
    assert turns.get_turn(queued["id"])["state"] == "queued"
    await recovery.consume(saved)
    await recovery.consume(saved)
    unsent = [event for event in session_events.get_events(SESSION)
              if event["event_type"] == "message.user" and event["payload"].get("not_sent")]
    assert len(unsent) == 1


async def test_unexpected_cleanup_error_does_not_checkpoint_receipt(monkeypatch):
    _lease, turn, _queued = fail_recording(monkeypatch)
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    with monkeypatch.context() as blocked:
        blocked.setattr(lifecycle, "stop_all_background_tasks", AsyncMock(side_effect=RuntimeError()))
        results = await recovery.consume(saved)
    assert any(result.get("retryable") for result in results)
    assert receipts.get(saved["id"])["result"] is None
    assert receipts.get(saved["id"])["consumed_at"] is None
    await recovery.consume(saved)
    assert receipts.get(saved["id"])["consumed_at"] is not None


async def test_validation_retry_does_not_recapture_resumed_task(monkeypatch, monitor):
    from app.core import task_persistence

    _lease, turn, _queued = fail_recording(monkeypatch)
    task_registry.note_turn_live(SESSION, turn["insert_order"])
    task_registry.resume_task(SESSION, monitor[1])  # a task attributed to this turn
    await task_persistence.flush()
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    assert any(task["task_id"] == monitor[1] for task in saved["receipt"]["tasks"])
    with monkeypatch.context() as blocked:
        blocked.setattr(turns, "apply_stop_receipt", Mock(side_effect=LockNotAvailable()))
        await recovery.consume(saved)  # reconstruct the original incarnation
    assert monitor[0].returncode is not None
    task_registry.resume_task(SESSION, monitor[1])
    stop_task = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(lifecycle, "stop_session_task", stop_task)
    await recovery.consume(saved)
    stop_task.assert_not_awaited()
    assert task_registry.get_task(SESSION, monitor[1])["status"] == "running"


async def test_failed_validation_retry_spares_successor(monkeypatch, monitor):
    from app.core.scoped_remote_stop import retry_incomplete_notes
    lease, turn, queued = fail_recording(monkeypatch)
    receipt = turns.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    with monkeypatch.context() as blocked:
        blocked.setattr(turns, "apply_stop_receipt", Mock(side_effect=LockNotAvailable()))
        await recovery.consume(saved, scope=lifecycle.capture_stop_scope(SESSION))
    assert monitor[0].returncode is not None
    await retry_incomplete_notes()
    assert runs.release_run(lease["id"], lease["fence_token"])
    owner = runs.claim_run(SESSION, "replacement-runner", 60)
    successor = turns.enqueue_turn(SESSION, "user", prompt="new owner")
    assert turns.start_turn(successor["id"], owner["id"], "replacement-runner")
    newer = turns.enqueue_turn(SESSION, "queue", prompt="new words")
    await recovery.consume(saved)
    assert turns.get_turn(successor["id"])["state"] == "running"
    assert turns.get_turn(queued["id"])["state"] == "queued"
    assert turns.get_turn(newer["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)
    assert receipts.get(saved["id"])["consumed_at"] is not None
