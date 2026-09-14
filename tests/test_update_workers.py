import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core import application_updates as updates
from app.core import update_workers as fleet
from app.core import desktop_lifecycle


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet.progress, "_path", lambda: tmp_path / "progress.json")
    values = {}
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: values.get(key))
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES", raising=False)
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    monkeypatch.setattr(updates, "_desktop_request", None)
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: [])
    fleet._receipts.clear()
    rows = [{"device_id": "self", "device_name": "Local", "is_self": True, "online": True},
            {"device_id": "peer", "device_name": "Peer", "is_self": False, "online": True},
            {"device_id": "offline", "device_name": "Offline", "is_self": False, "online": False}]
    monkeypatch.setattr(fleet.runners, "directory", lambda: {"self_device_id": "self", "runners": rows})
    remote = AsyncMock(return_value={"accepted": True, "reason": "Request queued"})
    monkeypatch.setattr(fleet.control, "call", remote)
    updates.desktop_exchange({"phase": "ready"})
    return values, remote


def test_default_current_dispatch_never_calls_peers(setup):
    _, remote = setup
    result = asyncio.run(fleet.dispatch("install"))
    assert result["scope"] == "current" and len(result["results"]) == 1
    assert result["results"][0]["accepted"]
    remote.assert_not_called()
    assert updates.desktop_exchange({"phase": "ready"})["action"] == "install"
    assert updates.desktop_exchange({"phase": "ready"}) == {}


def test_all_scope_targets_online_once_and_local_last(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    async def sent(*args, **kwargs):
        assert updates._desktop_request is None  # requester must remain up through fanout
        assert args[0] == "peer" and args[1] == "application-update"
        if args[2]["operation"] == "status":
            return {"phase": "ready", "version": "b" * 12}
        assert args[2]["expires_at"] > time.time()
        return {"accepted": True, "reason": "Request queued"}
    remote.side_effect = sent
    result = asyncio.run(fleet.dispatch("install"))
    assert result["scope"] == "all"
    assert [r["device_id"] for r in result["results"]] == ["peer", "offline", "self"]
    assert result["results"][1]["reason"] == "Offline; not queued"
    assert updates._desktop_request["action"] == "install"
    assert remote.await_count == 2  # staged target, then action


def test_disabled_busy_and_unavailable_do_not_receive_commands(setup, monkeypatch):
    values, _ = setup
    values[updates.KEY] = "false"
    assert not fleet.local_action("install", "disabled")["accepted"]
    values.clear()
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: ["running"])
    assert "Busy" in fleet.local_action("install", "busy")["reason"]
    monkeypatch.setattr(updates, "_desktop_seen", time.monotonic() - 30)
    assert not fleet.local_action("install", "offline")["accepted"]
    assert updates._desktop_request is None


def test_all_scope_respects_individual_refusals_without_aborting_others(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"accepted": False, "reason": "Updates disabled"}
    result = asyncio.run(fleet.dispatch("install"))
    assert not result["results"][0]["accepted"]
    assert result["results"][-1]["accepted"]


def test_expired_native_requests_and_retries_are_not_replayed(setup, monkeypatch):
    first = fleet.local_action("install", "same-id")
    assert first["accepted"]
    assert updates.desktop_exchange({"phase": "ready"})["action"] == "install"
    assert fleet.local_action("install", "same-id") == first
    assert updates._desktop_request is None
    updates.enqueue("install")
    updates._desktop_request["expires_at"] = 0
    assert updates.desktop_exchange({"phase": "ready"}) == {}


def test_peer_boundary_rechecks_membership_expiry_and_target_policy(setup, monkeypatch):
    values, _ = setup
    member = SimpleNamespace(kind="worker", status="active")
    monkeypatch.setattr(fleet.engine, "context", lambda: SimpleNamespace(member=lambda _: member))
    body = {"operation": "action", "action": "install", "request_id": "one", "expires_at": time.time() + 10}
    values[updates.KEY] = "false"
    assert not asyncio.run(fleet.peer_action(body, "peer"))["accepted"]
    body["expires_at"] = 0
    with pytest.raises(ValueError, match="expired"):
        asyncio.run(fleet.peer_action(body, "peer"))
    member.kind = "hub"
    with pytest.raises(PermissionError):
        asyncio.run(fleet.peer_action({"operation": "status"}, "hub"))


def test_peer_cannot_replace_target_identity_in_receipts(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"device_id": "self", "device_name": "spoofed", "accepted": True, "reason": "Request queued"}
    result = asyncio.run(fleet.dispatch("install"))
    assert result["results"][0]["device_id"] == "peer"
    assert result["results"][0]["device_name"] == "Peer"


def test_all_snapshot_handles_partial_failures_without_aborting(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.side_effect = RuntimeError("private endpoint detail must not escape")
    result = asyncio.run(fleet.snapshot())
    assert len(result["workers"]) == 3
    assert result["workers"][0]["supported"]
    assert "unavailable" in result["workers"][1]["reason"]
    assert result["workers"][2]["reason"] == "Offline; not queued"
    assert "private endpoint" not in str(result)

A, B = "a" * 40, "b" * 40


def test_install_survives_restart_and_waits_for_every_included_worker(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer_state = {"supported": True, "allow_updates": True, "phase": "ready", "version": B[:12], "running_version": A}
    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return dict(peer_state)
        # The target and cohort must already be durable when the command leaves.
        saved = fleet.progress.load()
        assert next(w for w in saved["workers"] if w["device_id"] == "peer")["target_version"] == B[:12]
        return {"accepted": True, "reason": "Request queued", "version": B[:12]}
    remote.side_effect = call
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    result = asyncio.run(fleet.dispatch("install"))
    assert result["results"][-1]["accepted"]
    saved = fleet.progress.load()
    assert {w["device_id"] for w in saved["workers"]} == {"self", "peer"}  # not offline workers
    assert updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})["action"] == "install"
    # Recreate volatile backend/launcher state; only the disk receipt survives.
    fleet._receipts.clear()
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["id"] == saved["id"]
    assert state["operation"]["phase"] == "waiting"
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    values[updates.ALL_KEY] = "false"  # a policy/scope change must not shrink the cohort
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "waiting"
    assert [w["status"] for w in state["operation"]["workers"]] == ["waiting", "verified"]
    peer_state.update(phase="idle", running_version=B, version=None)
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "complete"
    assert all(w["status"] == "verified" for w in state["operation"]["workers"])
    # Completion is terminal, even if a worker later disconnects.
    remote.side_effect = RuntimeError("offline")
    assert asyncio.run(fleet.snapshot())["operation"]["phase"] == "complete"


def test_missing_worker_and_stale_available_version_cannot_complete(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"accepted": True, "supported": True, "version": B[:12], "running_version": A}
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": B})
    monkeypatch.setattr(fleet.runners, "directory", lambda: {"self_device_id": "self", "runners": []})
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "waiting"
    assert state["operation"]["workers"][0]["status"] == "waiting"
    for action in ("install", "check", "download"):
        with pytest.raises(ValueError, match="previous update"):
            asyncio.run(fleet.dispatch(action))


def test_lost_action_reply_remains_unconfirmed_not_skipped(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return {"supported": True, "version": B[:12], "running_version": A}
        raise TimeoutError("reply lost after command delivery")
    remote.side_effect = call
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    result = asyncio.run(fleet.snapshot())["operation"]
    assert result["phase"] == "waiting"
    assert result["workers"][0]["accepted"] is None
    assert result["workers"][0]["status"] == "waiting"


def test_startup_timeout_and_launcher_errors_never_celebrate(setup, monkeypatch):
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A, "error": "Restart failed"})
    assert asyncio.run(fleet.snapshot())["operation"]["phase"] == "error"
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    saved = fleet.progress.load()
    monkeypatch.setattr(fleet.progress.time, "time", lambda: saved["started_at"] + 601)
    result = asyncio.run(fleet.snapshot())["operation"]
    assert result["phase"] == "error"
    assert "Startup not confirmed" in result["workers"][0]["message"]


@pytest.mark.parametrize("running,target", [(None, B), (B, None), ("", ""), (A, B), ("b", B), ("latest", "latest")])
def test_version_verification_requires_a_matching_commit(running, target):
    assert not fleet.progress.same_version(running, target)


def test_container_reports_running_code_not_staged_status(setup, monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    monkeypatch.setenv("GIT_SHA", A)
    (tmp_path / "status.json").write_text(json.dumps({"phase": "ready", "version": B, "running_version": B}))
    state = updates.status()
    assert state["running_version"] == A
    assert state["version"] == B
