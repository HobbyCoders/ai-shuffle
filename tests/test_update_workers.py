import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core import application_updates as updates
from app.core import update_workers as fleet
from app.core import desktop_lifecycle


@pytest.fixture
def setup(monkeypatch):
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
        assert args[2]["expires_at"] > time.time()
        return {"accepted": True, "reason": "Request queued"}
    remote.side_effect = sent
    result = asyncio.run(fleet.dispatch("install"))
    assert result["scope"] == "all"
    assert [r["device_id"] for r in result["results"]] == ["peer", "offline", "self"]
    assert result["results"][1]["reason"] == "Offline; not queued"
    assert updates._desktop_request["action"] == "install"
    remote.assert_awaited_once()


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
