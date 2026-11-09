"""Stop still quiesces failure-owned work, without rewriting its closing intent."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from psycopg2.errors import LockNotAvailable

from app.core import turn_manager
from app.core.task_registry import task_registry
from app.db import runs, session_events, stop_notices, turns
from app.mcp import builtin_tools
from tests.shared_fixtures import clean_task_registry, seed_session  # noqa: F401

SESSION = "failure-owned-stop"


@pytest.mark.parametrize("transport", ["ws", "peer"])
async def test_idle_stop_captures_empty_work_without_recapturing(monkeypatch, transport):
    monkeypatch.setattr(turns, "read_stop_receipt", Mock(side_effect=AssertionError("must not recapture")))
    await stop(transport, None, monkeypatch)
    assert turns.get_queued_turns(SESSION) == []
    assert task_registry.is_user_stopped(SESSION)


async def test_durable_cleanup_replay_cannot_latch_successor(monkeypatch):
    from app.core import stop_receipts as recovery
    from app.core.scoped_remote_stop import retry_incomplete_notes
    from app.db import stop_receipts as receipts

    lease, turn, _queued = fail_recording(monkeypatch)
    receipt = turn_manager.capture_stop_receipt(SESSION, turn["id"], None)
    saved = receipts.get(receipt["cleanup_receipts"][0])
    assert saved["receipt"]["cleanup_guard"] == receipt["cleanup_guard"]
    await retry_incomplete_notes()
    assert runs.release_run(lease["id"], lease["fence_token"])
    owner = runs.claim_run(SESSION, "replacement-runner", 60)
    successor = turns.enqueue_turn(SESSION, "user", prompt="replacement work")
    assert turns.start_turn(successor["id"], owner["id"], "replacement-runner")
    newer = turns.enqueue_turn(SESSION, "queue", prompt="new words")
    monkeypatch.setattr(turns, "read_stop_receipt", Mock(side_effect=AssertionError("must not recapture")))
    await recovery.consume(saved)
    assert turns.get_turn(successor["id"])["state"] == "running"
    assert turns.get_turn(newer["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)
    assert receipts.get(saved["id"])["consumed_at"] is not None


@pytest.mark.parametrize("transport", ["ws", "peer"])
@pytest.mark.parametrize("empty", [False, True])
async def test_captured_receipt_never_expands_to_later_input(monkeypatch, transport, empty):
    from app.core import query_session_lifecycle as lifecycle

    _lease, turn, _queued = fail_recording(monkeypatch)
    cleanup = lifecycle.stop_all_background_tasks
    newer = {}
    if empty:
        monkeypatch.setattr(turn_manager, "capture_stop_receipt", lambda *_: {})
    monkeypatch.setattr(turns, "read_stop_receipt", Mock(side_effect=AssertionError("must not recapture")))

    async def after_admission(*args, **kwargs):
        # A current owner too must not have its NEW input absorbed by old Stop.
        assert kwargs["receipt"] is not None
        newer.update(turns.enqueue_turn(SESSION, "queue", prompt="words after admission"))
        return await cleanup(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "stop_all_background_tasks", after_admission)
    await stop(transport, turn["id"], monkeypatch)
    assert turns.get_turn(newer["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(turn["id"])["stop_reason"] == "recording_failed"
    assert stop_notices.pending() == ["recording-failed:" + turn["id"]]


@pytest.mark.parametrize("transport", ["ws", "peer"])
async def test_orphan_flip_keeps_its_original_input_boundary(monkeypatch, transport):
    from app.core import query_session_lifecycle as lifecycle

    turn = turns.enqueue_turn(SESSION, "user", prompt="orphan work")
    assert turns.start_turn(turn["id"], None, "gone-runner")
    # Orphan Stop used to capture after its standalone terminal append, even
    # though that wait can let new owner input arrive after the flip.
    append = session_events.append_events_with_lock_retry
    newer = {}
    def after_flip(sid, events, **kwargs):
        newer.update(turns.enqueue_turn(SESSION, "queue", prompt="after orphan flip"))
        return append(sid, events, **kwargs)
    monkeypatch.setattr(session_events, "append_events_with_lock_retry", after_flip)
    await stop(transport, turn["id"], monkeypatch)
    assert turns.get_turn(newer["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)


@pytest.mark.parametrize("transport", ["ws", "peer"])
@pytest.mark.parametrize("target", ["failure", "parked", "flipped", "session"])
async def test_takeover_after_stop_admission_spares_new_input_and_latch(monkeypatch, transport, target):
    from app.core import query_session_lifecycle as lifecycle
    from app.core.scoped_remote_stop import retry_incomplete_notes

    if target == "failure":
        lease, turn, _queued = fail_recording(monkeypatch)
    else:
        lease = runs.claim_run(SESSION, "original-runner", 60)
        turn = turns.enqueue_turn(SESSION, "user", prompt="old work")
        assert turns.start_turn(turn["id"], lease["id"], "original-runner")
        if target == "parked":
            assert turns.park_turn(turn["id"])
    cleanup = lifecycle.stop_all_background_tasks
    replacement = {}

    async def overtake(*args, **kwargs):
        # Admission has returned across the await, but cleanup has not latched
        # or read input yet. Exercise the real recovery/start/enqueue producers.
        if target == "failure":
            await retry_incomplete_notes()
        elif target == "parked":
            assert turns.settle_parked_turns(SESSION)
        else:
            assert turns.settle_turn(turn["id"], "completed")
        assert runs.release_run(lease["id"], lease["fence_token"])
        owner = runs.claim_run(SESSION, "replacement-runner", 60)
        successor = turns.enqueue_turn(SESSION, "user", prompt="new work")
        assert turns.start_turn(successor["id"], owner["id"], "replacement-runner")
        newer = turns.enqueue_turn(SESSION, "queue", prompt="new owner words")
        replacement.update(successor=successor, newer=newer)
        return await cleanup(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "stop_all_background_tasks", overtake)
    await stop(transport, None if target == "session" else turn["id"], monkeypatch)
    assert turns.get_turn(replacement["successor"]["id"])["state"] == "running"
    assert turns.get_turn(replacement["newer"]["id"])["state"] == "queued"
    assert not task_registry.is_user_stopped(SESSION)



@pytest.fixture(autouse=True)
def session(mock_db, monkeypatch):
    seed_session(SESSION)
    monkeypatch.setattr(session_events, "LOCK_RETRY_DELAYS_SECONDS", (0,))
    # Only engine retirement and routing are replaced, not Stop or process kill.
    monkeypatch.setattr("app.core.turn_scheduler.retire_task_terminals", AsyncMock(return_value=True))
    monkeypatch.setattr("app.core.query_session_lifecycle._keep_monitor_shells_on_stop", lambda: False)


@pytest.fixture
async def monitor(monkeypatch):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        start_new_session=os.name != "nt",
    )
    shell = builtin_tools._BackgroundTask("controlled-monitor", "controlled wait", None, session_id=SESSION)
    shell.process = proc
    monkeypatch.setitem(builtin_tools._background_tasks, shell.task_id, shell)
    task_registry.start_task(SESSION, shell.task_id, "shell", "controlled wait")
    task_registry.mark_monitor_shell_task(SESSION, shell.task_id)
    try:
        yield proc, shell.task_id
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), 3)


def fail_recording(monkeypatch):
    lease = runs.claim_run(SESSION, "original-runner", 60)
    turn = turns.enqueue_turn(SESSION, "user", prompt="already executed")
    assert turns.start_turn(turn["id"], lease["id"], "original-runner")
    queued = turns.enqueue_turn(
        SESSION, "queue", prompt="keep these words", command_id="queued-owner-send",
    )
    with monkeypatch.context() as blocked:
        blocked.setattr(session_events, "append_events", Mock(side_effect=LockNotAvailable()))
        assert turns.settle_turn(turn["id"], "aborted", "recording_failed", run_id=lease["id"])
    assert stop_notices.pending() == ["recording-failed:" + turn["id"]]
    return lease, turn, queued


async def stop(transport, turn_id, monkeypatch, *, finished=False):
    if transport == "peer":
        from app.network import control

        monkeypatch.setattr(control, "_require_owned", lambda *_: None)
        result = await control._interrupt({
            "session_id": SESSION, "mode": "stop", "stop_background": True,
            "turn_id": turn_id, "source": "user_stop",
        }, "source-worker")
        assert result["finished"] is finished
        return result
    from app.api.chat_ws_handler import ChatWebSocketHandler

    handler = ChatWebSocketHandler(websocket=Mock(), device_id="device", user_id="admin", api_user_id=None)
    handler._may_act_on_session = AsyncMock(return_value=True)
    handler._network_owner = AsyncMock(return_value=None)
    handler._v2_broadcast_queue = AsyncMock()
    handler.send_json = AsyncMock()
    await handler.handle_stop({"session_id": SESSION, "turn_id": turn_id})
    frames = [call.args[0] for call in handler.send_json.call_args_list]
    assert not [frame for frame in frames if frame.get("type") == "error"]
    misses = [frame for frame in frames if frame.get("type") == "stop_result"]
    assert len(misses) == int(finished)
    if finished:
        assert misses[0]["stopped"] is False


@pytest.mark.parametrize("transport", ["ws", "peer"])
@pytest.mark.parametrize("ownership", [
    "same", "released", "replaced", "replacement-released", "replacement-expired",
])
async def test_failure_owned_stop_quiesces_only_the_current_run(monkeypatch, monitor, transport, ownership):
    lease, turn, queued = fail_recording(monkeypatch)
    if ownership != "same":
        assert runs.release_run(lease["id"], lease["fence_token"])
    if ownership in ("replaced", "replacement-released", "replacement-expired"):
        replacement = runs.claim_run(
            SESSION, "replacement-runner", -1 if ownership == "replacement-expired" else 60,
        )
        assert replacement and replacement["id"] != lease["id"]
        if ownership == "replacement-released":
            assert runs.release_run(replacement["id"], replacement["fence_token"])
    marker = turns.get_turn(turn["id"])
    proc, shell_id = monitor
    await asyncio.wait_for(stop(transport, turn["id"], monkeypatch), 5)
    current = ownership in ("same", "released")
    # Real process termination, registry latch and durable queue mutation -- not
    # merely an assertion that a mocked stop_all_background_tasks was called.
    assert (proc.returncode is not None) is current
    assert task_registry.is_user_stopped(SESSION) is current
    assert (turns.get_turn(queued["id"])["state"] == "aborted") is current
    assert (task_registry.get_task(SESSION, shell_id)["status"] == "killed") is current
    assert turns.get_turn(turn["id"]) == marker
    assert stop_notices.pending() == ["recording-failed:" + turn["id"]]
    if transport == "peer":
        # Shared predicate is also the owning peer receiver's cleanup admission.
        assert (turn_manager.capture_stop_receipt(SESSION, turn["id"], None) is not None) is current
    from app.core.scoped_remote_stop import retry_incomplete_notes

    await retry_incomplete_notes()
    assert stop_notices.pending() == []
    rows = session_events.get_events(SESSION)
    assert len([row for row in rows if row["event_type"] == "turn.aborted"
                and row["turn_id"] == turn["id"]]) == 1
    if current:
        assert turns.pop_ready(SESSION) is None
        assert any(row["event_type"] == "queued_message.withdrawn" for row in rows)
        assert any(row["event_type"] == "message.user"
                   and row["payload"].get("not_sent")
                   and row["payload"].get("content") == "keep these words" for row in rows)
    else:
        assert turns.pop_ready(SESSION)["id"] == queued["id"]


@pytest.mark.parametrize("transport", ["ws", "peer"])
async def test_ordinary_stop_also_holds_queued_words(monkeypatch, monitor, transport):
    lease = runs.claim_run(SESSION, "original-runner", 60)
    turn = turns.enqueue_turn(SESSION, "user", prompt="executing")
    assert turns.start_turn(turn["id"], lease["id"], "original-runner")
    queued = turns.enqueue_turn(
        SESSION, "queue", prompt="keep these words", command_id="queued-owner-send",
    )
    await asyncio.wait_for(stop(transport, turn["id"], monkeypatch), 5)
    assert monitor[0].returncode is not None
    assert task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(queued["id"])["state"] == "aborted"
    assert turns.pop_ready(SESSION) is None
    assert any(row["event_type"] == "message.user"
               and row["payload"].get("not_sent")
               and row["payload"].get("content") == "keep these words"
               for row in session_events.get_events(SESSION))


@pytest.mark.parametrize("transport", ["ws", "peer"])
async def test_delayed_failure_owned_stop_spares_started_successor(monkeypatch, monitor, transport):
    lease, turn, queued = fail_recording(monkeypatch)
    from app.core.scoped_remote_stop import retry_incomplete_notes

    await retry_incomplete_notes()
    assert runs.release_run(lease["id"], lease["fence_token"])
    successor = runs.claim_run(SESSION, "replacement-runner", 60)
    assert turns.start_turn(queued["id"], successor["id"], "replacement-runner")
    behind = turns.enqueue_turn(SESSION, "queue", prompt="successor's next input")
    await asyncio.wait_for(stop(transport, turn["id"], monkeypatch, finished=True), 5)
    assert monitor[0].returncode is None
    assert not task_registry.is_user_stopped(SESSION)
    assert turns.get_turn(queued["id"])["state"] == "running"
    assert turns.get_turn(behind["id"])["state"] == "queued"
