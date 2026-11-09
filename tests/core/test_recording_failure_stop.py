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
@pytest.mark.parametrize("ownership", ["same", "released", "replaced", "replacement-released"])
async def test_failure_owned_stop_quiesces_only_the_current_run(monkeypatch, monitor, transport, ownership):
    lease, turn, queued = fail_recording(monkeypatch)
    if ownership != "same":
        assert runs.release_run(lease["id"], lease["fence_token"])
    if ownership in ("replaced", "replacement-released"):
        replacement = runs.claim_run(SESSION, "replacement-runner", 60)
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
        assert turn_manager.stop_reaches_background_work(SESSION, turn["id"], None) is current
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
