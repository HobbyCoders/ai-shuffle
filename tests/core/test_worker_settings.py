"""Machine-settings wire contract, controlled peer fanout and token-free status."""
import asyncio
import inspect
import json
import time
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import worker_settings as ws, update_workers
from app.core.claude_cli_status import ClaudeCliStatus
from app.db import database
from app.network import control, engine, membership, tokens


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ws._readings.clear()
    ws._receipts.clear()
    monkeypatch.setattr(ws, "_action_lock", asyncio.Lock())
    member = SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE)
    ctx = SimpleNamespace(device_id="receiver", member=lambda _: member)
    monkeypatch.setattr(engine, "context", lambda: ctx)
    monkeypatch.setattr(engine, "instance_aliases", lambda: {
        row["legacy_instance_id"]: row["device_id"] for row in ws._directory()
        if row.get("legacy_instance_id")
    })
    monkeypatch.setattr(ws.claude_cli_install, "_in_use", lambda: [])
    yield
    ws._readings.clear()
    ws._receipts.clear()


def envelope(action=None, **extra):
    return {"operation": "action", "request_id": "10000000-0000-0000-0000-000000000001",
            "expires_at": time.time() + 12,
            "action": action or {"operation": "claude-install"}, **extra}


def row(device="peer", **extra):
    return {"device_id": device, "device_name": device.title(), "is_self": False,
            "online": True, "legacy_instance_id": device + "-instance", "capabilities": {}, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"operation": "shell", "command": "whoami"},
    {"operation": "status", "url": "https://example.com"},
    envelope({"operation": "claude-login"}),
    envelope({"operation": "claude-install", "command": "whoami"}),
    envelope({"operation": "claude-keep-installed", "keep_installed": "false"}),
    envelope({"operation": "release-channel", "channel": "edge"}),
    envelope({"operation": "always-on", "always_on": True}),
    envelope(expires_at=0), envelope(expires_at=float("nan")),
    envelope(expires_at=float("inf")), envelope(expires_at=time.time() + 1000),
    envelope(request_id="bad"), envelope(scope="all"),
])
async def test_receiver_refuses_unknown_operations_and_arguments(body, monkeypatch):
    start = Mock()
    monkeypatch.setattr(ws.claude_cli_install, "start", start)
    with pytest.raises(ValueError):
        await control.handle("worker-settings", body, from_device="sender")
    start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status,sender", [
    (membership.KIND_CONSUMER, membership.STATUS_ACTIVE, "sender"),
    (membership.KIND_WORKER, "revoked", "sender"),
    (membership.KIND_WORKER, membership.STATUS_ACTIVE, "receiver"),
])
async def test_receiver_independently_refuses_non_worker_callers(kind, status, sender, monkeypatch):
    ctx = SimpleNamespace(device_id="receiver", member=lambda _: SimpleNamespace(kind=kind, status=status))
    monkeypatch.setattr(engine, "context", lambda: ctx)
    with pytest.raises(PermissionError):
        await ws.peer_action({"operation": "status"}, sender)


def test_peer_http_requires_worker_identity(monkeypatch):
    from app.api.network import router
    from app.network import transport
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(tokens, "require_device", lambda _: "sender")
    monkeypatch.setattr(transport, "note_contact", lambda _: None)
    member = SimpleNamespace(kind=membership.KIND_CONSUMER, status=membership.STATUS_ACTIVE)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="receiver", member=lambda _: member))
    with TestClient(app) as client:
        response = client.post("/api/v1/network/peer/control/worker-settings", json={"operation": "status"})
    assert response.status_code == 403


def test_public_actions_require_owner_and_deny_preview(monkeypatch):
    from app.api.auth import require_admin
    from app.api.settings_workers import router
    from app.core import preview_access
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/settings")
    with TestClient(app) as client:
        assert client.post("/api/v1/settings/worker-settings/action", json={
            "target": "all", "action": {"operation": "claude-install"}}).status_code in (401, 403)
    app.dependency_overrides[require_admin] = lambda: "owner"
    monkeypatch.setattr(preview_access, "grant", lambda _: object())
    with TestClient(app) as client:
        assert client.post("/api/v1/settings/worker-settings/action", json={
            "target": "all", "action": {"operation": "claude-install"}}).status_code == 403


@pytest.mark.asyncio
async def test_validation_does_not_reflect_unknown_private_inputs():
    with pytest.raises(ValueError) as raised:
        await ws.peer_action({"operation": "status", "access_token": "never-return-this"}, "sender")
    assert "never-return-this" not in str(raised.value)


@pytest.mark.asyncio
async def test_status_uses_cli_output_and_discards_private_details(monkeypatch):
    cli = ClaudeCliStatus(installed=True, signed_in=True, email="person@example.com", plan="max",
                          cli_version="2.1.1", org_id="private-org", config_dir="private-dir")
    monkeypatch.setattr(ws.claude_cli_status, "get_status", lambda: cli)
    monkeypatch.setattr(ws.claude_cli_install, "payload", lambda: {
        "installed": True, "version": "2.1.1", "keep_installed": True, "installed_by_app": True,
        "can_install": True, "path": "private-path", "access_token": "private-token",
        "job": {"kind": "install", "state": "succeeded", "output_tail": "private-output"}})
    monkeypatch.setattr(ws.auth_service, "is_github_authenticated", lambda: True)
    monkeypatch.setattr(ws.application_updates, "channel_status", lambda: {
        "channel": "stable", "editable": False, "restart_required": False, "token": "private-channel"})
    state = await ws.local_status()
    assert state["claude"]["email"] == "person@example.com"
    assert state["claude"]["plan"] == "max"
    assert state["github_signed_in"] is True
    assert state["claude_sign_in_supported"] is False
    assert "private-" not in json.dumps(state)


@pytest.mark.asyncio
async def test_slow_reads_are_parallel_cached_and_share_inflight(monkeypatch):
    gate = asyncio.Event()
    calls = 0
    async def probe():
        nonlocal calls
        calls += 1
        await gate.wait()
        return "ready"
    monkeypatch.setattr(ws, "READ_TIMEOUT", 0.01)
    assert await asyncio.gather(ws._cached("one", probe), ws._cached("one", probe)) == [None, None]
    assert calls == 1
    gate.set()
    assert await ws._cached("one", probe) == "ready"
    assert calls == 1


@pytest.mark.parametrize("name", ["parse_request", "peer_action", "local_status", "apply_once", "snapshot", "dispatch"])
def test_public_functions_have_resolvable_parameter_and_return_annotations(name: str) -> None:
    function = getattr(ws, name)
    hints = get_type_hints(function)
    assert "return" in hints
    assert all(parameter in hints for parameter in inspect.signature(function).parameters)


@pytest.mark.asyncio
async def test_slow_read_cache_expires_after_completion_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ws, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(ws, "READ_TIMEOUT", 0.01)
    gate = asyncio.Event()
    calls = 0

    async def probe() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "ready"

    assert await ws._cached("slow", probe) is None
    pending = ws._readings["slow"][0]
    clock.now = 3.5
    gate.set()
    assert await pending == "ready"
    await asyncio.sleep(0)

    clock.now = 5.0
    assert await ws._cached("slow", probe) == "ready"
    assert calls == 1  # The next poll reuses the read that outlived its short timeout.
    clock.now = 3.5 + ws.CACHE_SECONDS
    assert await ws._cached("slow", probe) == "ready"
    assert calls == 2


@pytest.mark.asyncio
async def test_invalidated_read_cannot_replace_or_extend_a_new_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ws, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(ws, "READ_TIMEOUT", 0.01)
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "old"

    assert await ws._cached("claude", slow) is None
    pending = ws._readings["claude"][0]
    ws._invalidate()
    clock.now = 1.0
    fresh = AsyncMock(return_value="new")
    assert await ws._cached("claude", fresh) == "new"
    clock.now = 5.0
    gate.set()
    assert await pending == "old"
    await asyncio.sleep(0)
    assert await ws._cached("claude", fresh) == "new"
    assert fresh.await_count == 1
    clock.now = 1.0 + ws.CACHE_SECONDS
    assert await ws._cached("claude", fresh) == "new"
    assert fresh.await_count == 2


@pytest.mark.asyncio
async def test_receipts_prevent_duplicate_dispatch_and_id_reuse(monkeypatch):
    run = Mock(return_value=ws._result("accepted", "done"))
    monkeypatch.setattr(ws, "_local_action", run)
    body = envelope()
    first, second = await asyncio.gather(ws.peer_action(body, "sender"), ws.peer_action(body, "sender"))
    assert first == second and first["outcome"] == "accepted"
    assert run.call_count == 1
    body["action"] = {"operation": "claude-update"}
    with pytest.raises(ValueError, match="different action"):
        await ws.peer_action(body, "sender")


@pytest.mark.asyncio
async def test_uncertain_local_action_is_not_replayed(monkeypatch):
    run = Mock(side_effect=RuntimeError("private failure"))
    monkeypatch.setattr(ws, "_local_action", run)
    body = envelope()
    assert (await ws.peer_action(body, "sender"))["outcome"] == "uncertain"
    assert (await ws.peer_action(body, "sender"))["outcome"] == "uncertain"
    assert run.call_count == 1


@pytest.mark.parametrize("state,reason", [
    ({"can_install": False}, "platform"),
    ({"job": {"state": "running"}}, "busy"),
    ({"installed": True, "installed_by_app": False}, "not-owned"),
])
def test_installer_platform_ownership_and_busy_checks(state, reason, monkeypatch):
    data = {"can_install": True, "installed": False, "installed_by_app": False, "job": None, **state}
    monkeypatch.setattr(ws.claude_cli_install, "payload", lambda: data)
    start = Mock()
    monkeypatch.setattr(ws.claude_cli_install, "start", start)
    assert ws._local_action(ws.ClaudeInstall(operation="claude-install"))["reason"] == reason
    start.assert_not_called()


def test_installer_runs_existing_local_producer(monkeypatch):
    monkeypatch.setattr(ws.claude_cli_install, "payload", lambda: {
        "can_install": True, "installed": False, "installed_by_app": False, "job": None})
    start = Mock()
    monkeypatch.setattr(ws.claude_cli_install, "start", start)
    assert ws._local_action(ws.ClaudeInstall(operation="claude-install"))["outcome"] == "accepted"
    start.assert_called_once_with("install")


def test_update_requires_install_and_refuses_running_cli(monkeypatch):
    monkeypatch.setattr(ws.claude_cli_install, "payload", lambda: {
        "can_install": True, "installed": False, "installed_by_app": False, "job": None})
    assert ws._local_action(ws.ClaudeInstall(operation="claude-update"))["reason"] == "not-installed"
    monkeypatch.setattr(ws.claude_cli_install, "_in_use", lambda: [123])
    assert ws._local_action(ws.ClaudeInstall(operation="claude-install"))["reason"] == "busy"


@pytest.mark.asyncio
async def test_channel_extends_update_control_and_preserves_launcher_refusal(monkeypatch):
    channel = Mock(side_effect=ValueError("private launcher details"))
    monkeypatch.setattr(ws.application_updates, "set_channel", channel)
    result = await control.handle("application-update", envelope({"operation": "release-channel", "channel": "stable"}), from_device="sender")
    assert result["outcome"] == "refused" and result["reason"] == "channel-refused"
    channel.assert_called_once_with("stable")
    with pytest.raises(ValueError):
        await update_workers.peer_action(envelope(), "sender")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,outcome", [
    (control.RunnerUnreachable("gone"), "offline"),
    (control.ControlRefused("Not allowed"), "refused"),
    (control.ControlRefused("Unknown control action"), "unsupported"),
    (control.MaybeApplied("lost reply"), "uncertain"),
    (TimeoutError(), "uncertain"),
])
async def test_remote_outcomes(exc, outcome, monkeypatch):
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=exc))
    assert (await ws._remote(row(), {"operation": "status"}))["outcome"] == outcome


@pytest.mark.asyncio
async def test_fanout_targets_all_online_workers_once_with_no_recursion(monkeypatch):
    rows = [row("local", is_self=True), row("online"), row("offline", online=False)]
    monkeypatch.setattr(ws, "_directory", lambda: rows)
    seen = []
    async def controlled_peer(device, action, body, **kwargs):
        seen.append((device, action, body))
        # Drive the typed receiver, not a canned consumer-only fixture.
        return await ws.peer_action(body, "sender")
    produce = Mock(return_value=ws._result("accepted", "done"))
    monkeypatch.setattr(ws, "_local_action", produce)
    monkeypatch.setattr(control, "call", controlled_peer)
    result = await ws.dispatch(ws.Dispatch(target="all", action={"operation": "claude-install"}))
    assert [r["outcome"] for r in result["workers"]] == ["accepted", "accepted", "offline"]
    assert len(seen) == 1 and seen[0][:2] == ("online", "worker-settings")
    assert "target" not in seen[0][2] and "scope" not in seen[0][2]
    assert seen[0][2]["expires_at"] - time.time() <= 15
    assert produce.call_count == 2


@pytest.mark.asyncio
async def test_shared_availability_edits_offline_target_without_peer_execution(monkeypatch):
    rows = [row("off", online=False)]
    monkeypatch.setattr(ws, "_directory", lambda: rows)
    remote = AsyncMock()
    monkeypatch.setattr(control, "call", remote)
    result = await ws.dispatch(ws.Dispatch(target="off", action={"operation": "always-on", "always_on": True}))
    assert result["workers"][0]["reason"] == "saved"
    assert database.get_system_setting("worker_always_on:off-instance") == "true"
    assert ws._availability(rows[0])["always_on"] is True
    await ws.dispatch(ws.Dispatch(target="off", action={"operation": "always-on", "always_on": None}))
    assert database.get_system_setting("worker_always_on:off-instance") is None
    remote.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_or_unmapped_target_never_falls_back_to_self(monkeypatch):
    monkeypatch.setattr(ws, "_directory", lambda: [row(legacy_instance_id=None)])
    monkeypatch.setattr(engine, "instance_aliases", lambda: {})
    with pytest.raises(LookupError):
        await ws.dispatch(ws.Dispatch(target="typo", action={"operation": "claude-install"}))
    result = await ws.dispatch(ws.Dispatch(target="peer", action={"operation": "always-on", "always_on": False}))
    assert result["workers"][0]["reason"] == "unknown-instance"


@pytest.mark.asyncio
async def test_snapshot_lists_offline_workers_without_dialing_them(monkeypatch):
    monkeypatch.setattr(ws, "_directory", lambda: [row("off", online=False)])
    remote = AsyncMock()
    monkeypatch.setattr(control, "call", remote)
    assert (await ws.snapshot())["workers"][0]["outcome"] == "offline"
    remote.assert_not_called()


@pytest.mark.asyncio
async def test_channel_fanout_probes_support_before_existing_update_control(monkeypatch):
    monkeypatch.setattr(ws, "_directory", lambda: [row("new"), row("old")])
    seen = []
    async def peer(device, action, body, **kwargs):
        seen.append((device, action, body))
        if device == "old":
            raise control.ControlRefused("Unknown control action")
        if body["operation"] == "status":
            return ws._result("ok", "done", status=ws.MachineStatus())
        return await update_workers.peer_action(body, "sender")
    channel = Mock(return_value={"channel": "nightly", "editable": True, "restart_required": False})
    monkeypatch.setattr(ws.application_updates, "set_channel", channel)
    monkeypatch.setattr(control, "call", peer)
    result = await ws.dispatch(ws.Dispatch(target="all", action={"operation": "release-channel", "channel": "nightly"}))
    assert [r["outcome"] for r in result["workers"]] == ["accepted", "unsupported"]
    channel.assert_called_once_with("nightly")
    assert [(device, action) for device, action, _ in seen if action == "application-update"] == [("new", "application-update")]


@pytest.mark.asyncio
async def test_expiry_is_checked_again_after_waiting_for_the_dispatch_lock(monkeypatch):
    now = time.time()
    body = ws.ActionRequest.model_validate(envelope(expires_at=now + 10))
    times = iter([now, now + 11])
    monkeypatch.setattr(ws.time, "time", lambda: next(times))
    action = Mock()
    monkeypatch.setattr(ws, "_local_action", action)
    with pytest.raises(ValueError, match="expired"):
        await ws.apply_once(body, "sender")
    action.assert_not_called()


@pytest.mark.asyncio
async def test_peer_response_uses_nested_output_allowlist(monkeypatch):
    private = {"outcome": "ok", "reason": "done", "access_token": "private-top", "status": {
        "github_signed_in": True, "gh_token": "private-gh", "claude": {
            "installed": True, "keep_installed": True, "installed_by_app": True, "can_install": True,
            "access_token": "private-cli", "binary_path": "private-path", "signed_in": True,
            "email": "person@example.com", "job": {"kind": "install", "state": "succeeded", "output_tail": "private-output"}}}}
    monkeypatch.setattr(control, "call", AsyncMock(return_value=private))
    result = await ws._remote(row(), {"operation": "status"})
    assert result["status"]["claude"]["email"] == "person@example.com"
    assert "private-" not in json.dumps(result)


@pytest.mark.asyncio
async def test_status_reads_peers_in_parallel_with_bounded_fanout(monkeypatch):
    rows = [row(str(i)) for i in range(12)]
    monkeypatch.setattr(ws, "_directory", lambda: rows)
    active, peak = 0, 0
    async def peer(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return ws._result("ok", "done", status=ws.MachineStatus())
    monkeypatch.setattr(control, "call", peer)
    result = await ws.snapshot()
    assert len(result["workers"]) == 12
    assert 1 < peak <= ws.FANOUT


@pytest.mark.asyncio
async def test_keep_installed_uses_target_scoped_local_writer(monkeypatch):
    keep = Mock()
    monkeypatch.setattr(ws.claude_cli_install, "set_keep_installed", keep)
    result = await ws.peer_action(envelope({"operation": "claude-keep-installed", "keep_installed": False}), "sender")
    assert result["outcome"] == "accepted"
    keep.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_one_failed_shared_write_keeps_other_target_outcomes(monkeypatch):
    monkeypatch.setattr(ws, "_directory", lambda: [row("broken"), row("good")])
    def save(instance, value):
        if instance == "broken-instance":
            raise RuntimeError("private database error")
    monkeypatch.setattr(ws.worker_availability, "save_for_instance", save)
    result = await ws.dispatch(ws.Dispatch(target="all", action={"operation": "always-on", "always_on": True}))
    assert [r["outcome"] for r in result["workers"]] == ["uncertain", "accepted"]
    assert "private" not in json.dumps(result)
