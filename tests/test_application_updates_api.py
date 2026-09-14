from fastapi import FastAPI
from fastapi.testclient import TestClient
import json
import pytest

from app.api import settings_updates as api
from app.api.auth import require_auth, require_admin
from app.core import application_updates as updates


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.core import update_workers
    monkeypatch.setattr(update_workers.runners, "directory", lambda: {"self_device_id": None, "runners": []})
    update_workers._receipts.clear()
    app = FastAPI()
    app.include_router(api.updates_router, prefix="/settings")
    app.dependency_overrides[require_auth] = lambda: "owner"
    app.dependency_overrides[require_admin] = lambda: "owner"
    values = {}
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: values.get(key))
    monkeypatch.setattr(updates.database, "set_system_setting", lambda key, value: values.__setitem__(key, value))
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(tmp_path / "allow-updates.json"))
    monkeypatch.setattr(api.preview_access, "grant", lambda request: None)
    with TestClient(app) as client:
        yield client, tmp_path, values, app


def test_default_enabled_write_readback_and_real_queue(client):
    client, root, values, _ = client
    assert client.get("/settings/application-updates").json()["allow_updates"] is True
    assert values == {}, "reading the default must not overwrite synced policy"
    assert client.get("/settings/application-updates").json()["update_all_workers"] is False
    assert client.put("/settings/application-updates", json={"update_all_workers": True}).json()["update_all_workers"] is True
    assert client.put("/settings/application-updates", json={"update_all_workers": False}).status_code == 200
    assert client.post("/settings/application-updates", json={"action": "check"}).status_code == 200
    queued = json.loads((root / "request.json").read_text())
    assert queued["action"] == "check" and queued["expires_at"] > 0
    assert client.put("/settings/application-updates", json={"allow_updates": False}).json()["allow_updates"] is False
    assert values == {"allow_updates": "false", "update_all_workers": "false"}
    assert client.get("/settings/application-updates").json()["allow_updates"] is False
    assert json.loads((root / "allow-updates.json").read_text()) == {"allow_updates": False}
    assert client.post("/settings/application-updates", json={"action": "install"}).status_code == 409
    assert client.put("/settings/application-updates", json={"allow_updates": True}).status_code == 200
    assert client.post("/settings/application-updates", json={"action": "install"}).status_code == 200


def test_preview_cannot_change_policy_or_request_code_execution(client, monkeypatch):
    client, root, values, _ = client
    monkeypatch.setattr(api.preview_access, "grant", lambda request: object())
    assert client.put("/settings/application-updates", json={"allow_updates": False}).status_code == 403
    assert client.post("/settings/application-updates", json={"action": "download"}).status_code == 403
    assert not values and not (root / "request.json").exists()


def test_schema_rejects_coerced_boolean_and_arbitrary_commands(client):
    client, _, _, _ = client
    assert client.put("/settings/application-updates", json={"allow_updates": "false"}).status_code == 422
    assert client.post("/settings/application-updates", json={"action": "shell"}).status_code == 422


def test_only_owner_can_mutate(client):
    client, _, _, app = client
    app.dependency_overrides.pop(require_admin)
    assert client.put("/settings/application-updates", json={"allow_updates": False}).status_code == 403
