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


def test_actual_producer_status_queues_only_explicit_local_refresh(client, monkeypatch):
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
    assert result["updater"]["refresh"] == "available"
    assert result["diagnostics"]["stages"][0]["outcome"] == "hit"
    for body in ({"action": "refresh-updater"}, {"action": "refresh-updater", "scope": "current", "mode": "stop"}):
        assert client.post("/settings/application-updates", json=body).status_code == 409
    body = {"action": "refresh-updater", "scope": "current"}
    assert client.post("/settings/application-updates", json=body).json() == {"queued": True}
    assert json.loads((root / "request.json").read_text())["action"] == "refresh-updater"
    supervisor.service()
    assert supervisor.refresh_requested.is_set()
    assert not (root / "request.json").exists()
    monkeypatch.setattr(policy, "allowed", lambda: False)
    assert client.post("/settings/application-updates", json=body).status_code == 409
    monkeypatch.setattr(api.preview_access, "grant", lambda _: object())
    assert client.post("/settings/application-updates", json=body).status_code == 403


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
