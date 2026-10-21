"""Safety regressions for owner/peer update waiting-state display data."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.internal import DesktopUpdateState
from app.core import desktop_lifecycle
from app.core import update_workers as fleet
from app.core.task_registry import TaskRegistry
from app.db import database
from app.mcp import builtin_tools
from tests.test_update_workers import setup, busy, B, real_active_session_ids


@pytest.fixture
def service(setup, mock_db, monkeypatch):
    registry = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", registry)
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", real_active_session_ids)
    monkeypatch.setattr(database, "get_session", lambda sid: {"title": "Preview chat"})
    monkeypatch.setattr(fleet.engine, "context", lambda: SimpleNamespace(member=lambda _: SimpleNamespace(kind="worker", status="active")))
    monkeypatch.setitem(builtin_tools._background_tasks, "service", SimpleNamespace(is_running=lambda: True))
    def register(command):
        registry.start_task("chat", "service", "shell", command)
        registry.mark_monitor_shell_task("chat", "service", serves=True)
    return register


async def test_command_credentials_never_reach_update_surfaces(service, monkeypatch):
    key = "sk-" + "synthetic" * 5
    bearer = "test-only-bearer-value"
    service(f'OPENAI_API_KEY={key} curl -H "Authorization: Bearer {bearer}" http://localhost:5173')
    snapshot = await fleet.snapshot()
    peer = await fleet.peer_action({"operation": "status"}, "peer")
    receipt = await fleet.dispatch("install")
    monkeypatch.setattr(desktop_lifecycle, "stop_session_work", AsyncMock(side_effect=RuntimeError("stop failed")))
    monkeypatch.setattr(fleet, "STOP_WAIT_SECONDS", 0)
    stopping = await fleet.dispatch("install", "stop")
    await asyncio.gather(*fleet._stop_tasks)
    stopped_snapshot = await fleet.snapshot()
    stopped_peer = await fleet.peer_action({"operation": "status"}, "peer")
    assert stopped_peer["stop_result"]["accepted"] is False
    for value in (snapshot, peer, receipt, stopping, stopped_snapshot, stopped_peer):
        serialized = json.dumps(value)
        assert key not in serialized and bearer not in serialized
    for blockers in (snapshot["workers"][0]["blocked_by"], peer["blocked_by"], receipt["results"][-1]["blocked_by"]):
        assert "subject" not in blockers[0]
        assert blockers[0]["port"] == 5173


async def test_busy_metadata_reads_stay_off_event_loop(service, monkeypatch):
    service("node preview.js --port 5173")
    loop_thread = threading.get_ident()
    threads = []
    def session(sid):
        threads.append(threading.get_ident())
        return {"title": "Preview chat"}
    monkeypatch.setattr(database, "get_session", session)
    await fleet.snapshot()
    await fleet.peer_action({"operation": "status"}, "peer")
    await fleet.dispatch("install")
    assert threads and all(thread != loop_thread for thread in threads)


@pytest.mark.parametrize("command,port", [
    ("ffmpeg -ss 00:01:30 input.mp4", None),
    ("echo 12:30", None),
    ("node app.js --port=5173", 5173),
    ("curl http://localhost:8000", 8000),
    ("curl http://127.0.0.1:8000", 8000),
    ("curl http://[::1]:8000", 8000),
    ("node app.js --port 99999", None),
])
def test_service_port_requires_network_context(service, command, port):
    service(command)
    assert desktop_lifecycle.busy_work()[0]["task_details"][0]["port"] == port


@pytest.mark.parametrize("failure", ["waiting_status", "busy_work"])
async def test_waiting_failure_does_not_lose_queued_install_or_break_status(setup, monkeypatch, caplog, failure):
    def broken(*args, **kwargs):
        raise RuntimeError("metadata failed")
    monkeypatch.setattr(fleet if failure == "waiting_status" else desktop_lifecycle, failure, broken)
    result = await fleet.dispatch("install")
    assert result["results"][-1]["accepted"] is True
    assert fleet.progress.load()["workers"][0]["accepted"] is True
    assert fleet.updates._desktop_request["action"] == "install"
    member = SimpleNamespace(kind="worker", status="active")
    monkeypatch.setattr(fleet.engine, "context", lambda: SimpleNamespace(member=lambda _: member))
    peer = await fleet.peer_action({"operation": "status"}, "peer")
    snapshot = await fleet.snapshot()
    assert peer["install_state"] is None and peer["blocked_by"] is None
    assert snapshot["workers"][0]["install_state"] is None
    assert "waiting state unavailable" in caplog.text


@pytest.mark.parametrize("mode", ["stop", "when_idle"])
async def test_waiting_failure_keeps_scheduled_receipt(busy, monkeypatch, mode):
    def broken(*args, **kwargs):
        raise RuntimeError("metadata failed")
    monkeypatch.setattr(fleet, "waiting_status", broken)
    monkeypatch.setattr(fleet, "_stop_then_restart", AsyncMock())
    result = fleet.local_action("install", "scheduled", mode)
    assert result["accepted"] is True
    assert fleet.local_action("install", "scheduled", mode) == result
    if mode == "stop":
        await asyncio.gather(*fleet._stop_tasks)
    else:
        task = fleet._deferred["task"]
        fleet.local_action("cancel", "cancel-scheduled")
        await asyncio.gather(task, return_exceptions=True)


def test_presend_refusal_preserves_real_peer_reason(setup, monkeypatch):
    values, _ = setup
    values[fleet.updates.ALL_KEY] = "true"
    fleet._refusals["peer"] = "Busy with running work: original service"
    fleet._refusals_at = 123.0
    monkeypatch.setattr(fleet.progress, "load", lambda: {"phase": "waiting"})
    monkeypatch.setattr(fleet, "snapshot", AsyncMock(return_value={"operation": {"phase": "waiting"}}))
    result = asyncio.run(fleet.dispatch("install", "stop"))
    assert fleet._refusals == {"peer": "Busy with running work: original service"}
    assert fleet._refusals_at == 123.0
    assert all(row["reason"] == "Waiting on workers to finish the previous update" for row in result["results"])


async def test_new_work_during_metadata_read_still_blocks_install(setup, monkeypatch):
    active = set()
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: set(active))
    def read():
        active.add("late-session")
        return []  # The metadata snapshot missed work that just started.
    monkeypatch.setattr(desktop_lifecycle, "busy_work", read)
    result = await fleet.dispatch("install")
    receipt = result["results"][-1]
    assert receipt["accepted"] is False
    assert receipt["busy"][0]["session_id"] == "late-session"
    assert fleet.updates._desktop_request is None


def test_retained_staged_idle_is_not_an_updater_error(setup):
    state = fleet.waiting_status({"phase": "idle", "staged_version": B})
    assert state["wait_reason"] == "updater_idle"


@pytest.mark.parametrize("revision", ["not-a-revision", "a" * 6, "A" * 40])
def test_bridge_rejects_invalid_staged_revision(revision):
    with pytest.raises(ValidationError):
        DesktopUpdateState(phase="ready", staged_version=revision)
