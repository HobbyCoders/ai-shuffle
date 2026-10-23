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


COMMAND_CREDENTIALS = [
    ('OPENAI_API_KEY=sk-' + 'synthetic' * 5, ['sk-' + 'synthetic' * 5]),
    ('curl -H "Authorization: Bearer test-only-bearer-value"', ['test-only-bearer-value']),
    ("curl -H 'Authorization: Bearer eyJfixture.payload.signature'", ['eyJfixture.payload.signature']),
    ('curl -H "Authorization: Bearer ghp_' + 'x' * 30 + '"', ['ghp_' + 'x' * 30]),
    ('psql postgresql://admin:fixture-db-secret@db.example.com:5432/app', ['fixture-db-secret']),
    ('git clone https://user:ghp_' + 'x' * 30 + '@github.com/x/y', ['ghp_' + 'x' * 30]),
    ('tool --token=fixture-token-equal', ['fixture-token-equal']),
    ('tool --token fixture-token-space', ['fixture-token-space']),
    ('tool --api-key=fixture-api-key', ['fixture-api-key']),
    ('tool --password fixture-password', ['fixture-password']),
    ('export ANTHROPIC_API_KEY=sk-ant-api03-' + 'A' * 90, ['sk-ant-api03-' + 'A' * 90]),
    ('x' * 100 + ' sk-ant-api03-' + 'B' * 60, ['sk-ant-api03-' + 'B' * 60]),
    ('aws AKIAIOSFODNN7EXAMPLE secret', ['AKIAIOSFODNN7EXAMPLE']),
    ('aws ASIAIOSFODNN7EXAMPLE secret', ['ASIAIOSFODNN7EXAMPLE']),
    ('npm run dev -- --host 0.0.0.0 --port 5173', []),
    ('ffmpeg -ss 00:01:30 -i a.mp4', []),
] + [(name + '=fixture-env-value', ['fixture-env-value']) for name in (
    'GITHUB_TOKEN', 'DB_PASSWORD', 'AWS_SECRET_ACCESS_KEY', 'HF_TOKEN',
    'CLOUDFLARE_API_TOKEN', 'STRIPE_SECRET', 'PGPASSWORD', 'MY_KEY',
    'db_passwd', 'APP_PWD', 'SERVICE_CREDENTIALS',
)]


@pytest.mark.parametrize('command,secrets', COMMAND_CREDENTIALS)
async def test_command_credentials_never_reach_update_surfaces(service, monkeypatch, command, secrets):
    service(command)
    snapshot = await fleet.snapshot()
    peer = await fleet.peer_action({"operation": "status"}, "peer")
    receipt = await fleet.dispatch("install")
    monkeypatch.setattr(desktop_lifecycle, "stop_session_work", AsyncMock(side_effect=RuntimeError("stop failed")))
    monkeypatch.setattr(fleet, "STOP_WAIT_SECONDS", 0)
    stopping = await fleet.dispatch("install", "stop")
    await asyncio.gather(*fleet._stop_tasks)
    stopped_snapshot = await fleet.snapshot()
    stopped_peer = await fleet.peer_action({"operation": "status"}, "peer")
    # The stop failed, so the update force-stopped the service and went ahead,
    # naming the chat (never the command line) in its note.
    assert stopped_peer["stop_result"]["accepted"] is True
    assert stopped_peer["stop_result"]["forced"] == ["Preview chat"]
    assert "force-stopped" in stopped_peer["stop_result"]["reason"]
    for value in (snapshot, peer, receipt, stopping, stopped_snapshot, stopped_peer):
        serialized = json.dumps(value)
        assert all(secret not in serialized for secret in secrets)
    for blockers in (snapshot["workers"][0]["blocked_by"], peer["blocked_by"], receipt["results"][-1]["blocked_by"]):
        assert "subject" not in blockers[0]
        assert "label" in blockers[0]


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


async def test_finished_work_during_metadata_read_is_not_a_blocker(service, monkeypatch):
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: {"still-running"})
    monkeypatch.setattr(desktop_lifecycle, "busy_work", lambda: [
        {"session_id": "finished", "title": "Finished chat", "tasks": []},
        {"session_id": "still-running", "title": "Live chat", "tasks": []},
    ])
    snapshot = await fleet.snapshot()
    peer = await fleet.peer_action({"operation": "status"}, "peer")
    receipt = (await fleet.dispatch("install"))["results"][-1]
    assert "Finished chat" not in receipt["reason"]
    assert [row["session_id"] for row in receipt["busy"]] == ["still-running"]
    for status in (snapshot["workers"][0], peer, receipt):
        assert {row["session_id"] for row in status["blocked_by"]} == {"still-running"}


async def test_new_busy_rows_respect_naming_limit(setup, monkeypatch):
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: {f"chat-{i:02}" for i in range(20)})
    monkeypatch.setattr(desktop_lifecycle, "busy_work", lambda: [])
    receipt = (await fleet.dispatch("install"))["results"][-1]
    assert receipt["accepted"] is False
    assert len(receipt["busy"]) == desktop_lifecycle.BUSY_LIMIT
    assert len(receipt["blocked_by"]) == desktop_lifecycle.BUSY_LIMIT
    assert "and 12 more" in receipt["reason"]


def test_retained_staged_idle_is_not_an_updater_error(setup):
    state = fleet.waiting_status({"phase": "idle", "staged_version": B})
    assert state["wait_reason"] == "updater_idle"


@pytest.mark.parametrize("revision", ["not-a-revision", "a" * 6, "A" * 40])
def test_bridge_rejects_invalid_staged_revision(revision):
    with pytest.raises(ValidationError):
        DesktopUpdateState(phase="ready", staged_version=revision)
