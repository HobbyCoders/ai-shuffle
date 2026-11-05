"""Status/API compatibility is exercised with the container's real producer."""
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings_updates as api
from app.api.auth import require_admin, require_auth
from app.core import application_updates as policy
from docker.application_updates import ContainerUpdates

A, B = "a" * 40, "b" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(tmp_path / "allow.json"))
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    monkeypatch.setattr(policy.database, "get_system_setting", lambda _: None)
    monkeypatch.setattr(api.preview_access, "grant", lambda _: None)
    app = FastAPI()
    app.include_router(api.updates_router, prefix="/settings")
    app.dependency_overrides[require_admin] = lambda: "owner"
    app.dependency_overrides[require_auth] = lambda: "owner"
    with TestClient(app) as client:
        yield client, tmp_path


def test_legacy_status_does_not_prove_outdated_updater(client, monkeypatch):
    client, root = client
    monkeypatch.setenv("GIT_SHA", B)
    (root / "status.json").write_text(json.dumps({"phase": "idle", "version": B[:12]}))
    result = client.get("/settings/application-updates").json()
    assert result["running_version"] == B
    assert result["updater"] == {"revision": None, "capabilities": [], "refresh": "unknown"}
    assert result["diagnostics"] is None
    assert client.post("/settings/application-updates", json={"action": "refresh-updater", "scope": "current"}).status_code == 409
    assert not (root / "request.json").exists()


def test_actual_producer_status_refuses_deferred_refresh(client, monkeypatch):
    client, root = client
    supervisor = ContainerUpdates(root, {"revision": A, "sequence": 1, "runtime": "fixture"}, b"", lambda: True, lambda _: {})
    supervisor.refresh_mode = "available"
    supervisor.state = {"active": B}
    supervisor.begin_diagnostics(B)
    with supervisor.stage("tools_build") as stage:
        stage["outcome"] = "hit"
    supervisor.publish("idle")
    result = client.get("/settings/application-updates").json()
    assert result["updater"]["revision"] == A
    assert result["updater"]["refresh"] == "container_restart_required"
    assert "refresh-updater-v1" not in result["updater"]["capabilities"]
    assert result["diagnostics"]["stages"][0]["outcome"] == "hit"
    for body in ({"action": "refresh-updater"}, {"action": "refresh-updater", "scope": "current", "mode": "stop"}):
        assert client.post("/settings/application-updates", json=body).status_code == 409
    body = {"action": "refresh-updater", "scope": "current"}
    response = client.post("/settings/application-updates", json=body)
    assert response.status_code == 409
    assert "separately approved container restart" in response.json()["detail"]
    assert not supervisor.refresh_requested.is_set()
    assert not (root / "request.json").exists()
    monkeypatch.setattr(policy, "allowed", lambda: False)
    assert client.post("/settings/application-updates", json=body).status_code == 409
    monkeypatch.setattr(api.preview_access, "grant", lambda _: object())
    assert client.post("/settings/application-updates", json=body).status_code == 403


@pytest.mark.parametrize("legacy", [False, True])
def test_core_and_owner_api_refuse_even_legacy_refresh_capability(client, monkeypatch, legacy):
    client, root = client
    updater = {"revision": A, "capabilities": ["refresh-updater-v1"], "refresh": "available"} if legacy else None
    (root / "status.json").write_text(json.dumps({"phase": "idle", "version": B[:12], "updater": updater}))
    monkeypatch.setattr(policy, "allowed", lambda: True)
    # Neither caller may replace an unrelated, already queued normal action.
    pending = {"action": "check", "expires_at": 9999999999}
    (root / "request.json").write_text(json.dumps(pending))
    with pytest.raises(ValueError, match="separately approved container restart"):
        policy.enqueue("refresh-updater")
    for body in ({"action": "refresh-updater"}, {"action": "refresh-updater", "scope": "current"},
                 {"action": "refresh-updater", "scope": "current", "mode": "stop"},
                 {"action": "refresh-updater", "scope": "current", "mode": "when_idle"}):
        response = client.post("/settings/application-updates", json=body)
        assert response.status_code == 409
        assert "separately approved container restart" in response.json()["detail"]
    assert json.loads((root / "request.json").read_text()) == pending


def test_deferred_refresh_keeps_policy_preview_and_owner_checks(client, monkeypatch):
    client, root = client
    body = {"action": "refresh-updater", "scope": "current"}
    monkeypatch.setattr(policy, "allowed", lambda: False)
    response = client.post("/settings/application-updates", json=body)
    assert response.status_code == 409
    assert "disabled in Account settings" in response.json()["detail"]
    with pytest.raises(ValueError, match="disabled in Account settings"):
        policy.enqueue("refresh-updater")
    monkeypatch.setattr(api.preview_access, "grant", lambda _: object())
    assert client.post("/settings/application-updates", json=body).status_code == 403
    monkeypatch.setattr(api.preview_access, "grant", lambda _: None)
    client.app.dependency_overrides.pop(require_admin)
    assert client.post("/settings/application-updates", json=body).status_code == 403
    assert not (root / "request.json").exists()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_real_service_refuses_stale_direct_refresh_request(client, monkeypatch, enabled, pending):
    import time
    client, root = client
    lifecycle = Mock()
    supervisor = ContainerUpdates(root, {"revision": A, "sequence": 1, "runtime": "fixture"},
                                  b"", lambda: enabled, lifecycle, refresh_mode="available")
    supervisor.state = {"active": B, **({"pending": B} if pending else {})}
    expected = dict(supervisor.state)
    (root / "request.json").write_text(json.dumps({"action": "refresh-updater", "expires_at": time.time() + 10}))
    supervisor.service()
    assert supervisor.state == expected
    assert not supervisor.refresh_requested.is_set()
    assert not supervisor.install_requested.is_set()
    lifecycle.assert_not_called()
    assert not (root / "request.json").exists()
    status = json.loads((root / "status.json").read_text())
    assert status["phase"] == ("ready" if pending else "error")
    assert "separately approved container restart" in status["error"]
    assert "refresh-updater-v1" not in status["updater"]["capabilities"]
    assert status["updater"]["refresh"] != "available"


@pytest.mark.parametrize("mode,active,transient,expected", [
    ("available", B, None, "container_restart_required"),
    ("container_restart_required", B, None, "container_restart_required"),
    ("available", A, None, "current"),
    ("container_restart_required", A, None, "current"),
    ("available", None, None, "unknown"),
    ("container_restart_required", B, "booting", "unknown"),
    ("available", B, "blocked", "unknown"),
    ("available", B, "pending", "unknown"),
    ("disabled", B, None, "disabled"),
    ("image_update_required", B, None, "image_update_required"),
    ("unknown", B, None, "unknown"),
])
def test_producer_metadata_never_offers_deferred_action(client, mode, active, transient, expected):
    client, root = client
    supervisor = ContainerUpdates(root, {"revision": A, "sequence": 1, "runtime": "fixture"},
                                  b"", lambda: True, lambda _: {}, refresh_mode=mode)
    supervisor.state = {**({"active": active} if active else {}),
                        **({transient: True if transient == "blocked" else B} if transient else {})}
    supervisor.publish("idle")
    result = client.get("/settings/application-updates").json()
    assert result["updater"]["revision"] == A
    assert result["updater"]["refresh"] == expected
    assert result["updater"]["capabilities"] == ["stage-diagnostics-v1", "dependency-cache-v1", "prebuilt-ui-v1", "node-output-cache-v1"]


@pytest.mark.parametrize("mode", ["available", "container_restart_required"])
def test_unknown_resident_does_not_infer_restart_requirement(client, monkeypatch, mode):
    client, root = client
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", A)
    supervisor = ContainerUpdates(root, {"revision": A, "sequence": 1, "runtime": "fixture"},
                                  b"", lambda: True, lambda _: {}, refresh_mode=mode)
    assert supervisor.updater_revision is None
    supervisor.state = {"active": B}
    supervisor.publish("idle")
    result = client.get("/settings/application-updates").json()
    assert result["updater"]["revision"] is None
    assert result["updater"]["refresh"] == "unknown"
    assert "refresh-updater-v1" not in result["updater"]["capabilities"]


def test_desktop_bridge_optional_metadata_is_bounded_and_redacted(monkeypatch):
    from app.api.internal import DesktopUpdateState
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES", raising=False)
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(policy.database, "get_system_setting", lambda _: None)
    for name, value in (("_desktop_state", {}), ("_desktop_seen", 0), ("_desktop_request", None),
                        ("_desktop_followup", None), ("_desktop_channel_request", None)):
        monkeypatch.setattr(policy, name, value)
    old = DesktopUpdateState(phase="idle").model_dump()
    policy.desktop_exchange(old)
    assert policy.status()["updater"]["revision"] is None
    new = DesktopUpdateState(phase="idle", updater={"revision": A, "capabilities": ["stage-diagnostics-v1"]},
                             diagnostics={"revision": B, "total_elapsed_ms": 1, "stages": [
                                 {"stage": "command", "elapsed_ms": 1, "outcome": "completed", "command": "private"}]})
    policy.desktop_exchange(new.model_dump())
    assert policy.status()["updater"]["revision"] == A
    assert "private" not in json.dumps(policy.status())
    for invalid in ({"stage": "private", "elapsed_ms": 1, "outcome": "completed"},
                    {"stage": "command", "elapsed_ms": float("nan"), "outcome": "completed"}):
        assert policy.safe_update_metadata({"diagnostics": {"stages": [invalid], "total_elapsed_ms": 1}})["diagnostics"] is None
