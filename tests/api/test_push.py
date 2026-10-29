"""Owner relay status and authenticated self-device token rotation."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import push
from app.db import mobile, pairing
from app.db.base import get_db

PATH = "/api/v1/settings/notifications"
TOKEN_PATH = "/api/v1/mobile/push-token"


def test_notification_status_requires_owner(client):
    assert client.get(PATH).status_code in (401, 403)


@pytest.mark.parametrize("linked", [True, False])
def test_status_returns_link_devices_and_enabled(native, authenticated_client, monkeypatch, linked):
    client, headers, _ = native
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "test-token"}).status_code == 200
    monkeypatch.setattr(push.push_notification_service, "is_linked", lambda: linked)
    response = authenticated_client.get(PATH)
    assert response.status_code == 200
    assert response.json() == {"linked": linked, "devices": 1, "enabled": linked}
    assert "no-store" in response.headers["cache-control"]
    assert "test-token" not in response.text


@pytest.mark.parametrize("method", ["put", "delete"])
def test_sender_key_routes_are_removed(authenticated_client, method):
    response = getattr(authenticated_client, method)(PATH)
    assert response.status_code == 405


@pytest.fixture
def native(mock_db):
    app = FastAPI()
    app.include_router(push.router)
    client = TestClient(app)
    key = "aim_test-device-key"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.cursor().execute("""INSERT INTO paired_clients (id, device_id, device_name, client_type, platform, scopes,
            created_at, updated_at) VALUES ('paired', 'phone', 'Phone', 'mobile', 'android', %s, NOW(), NOW())""",
            (json.dumps(["controller", "mobile:notifications"]),))
    device = mobile.create_mobile_device("phone", "Phone", "android", key_hash, {},
        paired_client_id="paired", api_key_expires_at=datetime.fromisoformat(expiry), scopes=["mobile:notifications"])
    return client, {"Authorization": "Bearer " + key}, device


def test_token_self_registration_rotation_delete_and_revocation(native):
    client, headers, device = native
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "first-token"}).json() == {"registered": True}
    assert mobile.get_mobile_device("phone")["fcm_token"] == "first-token"
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "rotated-token"}).status_code == 200
    mobile.clear_push_token(device["id"], "first-token")
    assert mobile.get_mobile_device("phone")["fcm_token"] == "rotated-token"
    assert client.delete(TOKEN_PATH, headers=headers).json() == {"registered": False}
    assert mobile.get_mobile_device("phone")["fcm_token"] is None
    client.put(TOKEN_PATH, headers=headers, json={"token": "current-token"})
    assert pairing.revoke_paired_client("paired")
    assert mobile.get_mobile_device("phone")["fcm_token"] is None
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "stale-token"}).status_code == 401


@pytest.mark.parametrize("token", ["test-push-token", None])
def test_device_list_exposes_registration_status_not_token(native, authenticated_client, token):
    client, headers, _ = native
    if token:
        assert client.put(TOKEN_PATH, headers=headers, json={"token": token}).status_code == 200

    response = authenticated_client.get("/api/v1/mobile/devices")

    assert response.status_code == 200, response.text
    device = response.json()[0]
    assert device["device_id"] == "phone"
    assert "fcm_token" not in device
    assert "test-push-token" not in response.text
    assert device["push_registered"] is bool(token)


@pytest.mark.parametrize("method", ["put", "delete"])
def test_tokens_require_native_device_key(native, method):
    client, headers, _ = native
    kwargs = {"json": {"token": "test-token"}} if method == "put" else {}
    for unauthorized in ({}, {"Authorization": "Bearer aim_wrong"}, {"Authorization": "Bearer api_user"}):
        assert getattr(client, method)(TOKEN_PATH, headers=unauthorized, **kwargs).status_code == 401


def test_scope_expiry_and_foreign_device_cannot_register(native):
    client, headers, device = native
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "token", "device_id": "another-phone"}).status_code == 422
    with get_db() as conn:
        conn.cursor().execute("UPDATE mobile_devices SET scopes=%s WHERE id=%s", (json.dumps(["controller"]), device["id"]))
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "token"}).status_code == 403
    with get_db() as conn:
        conn.cursor().execute("UPDATE mobile_devices SET api_key_expires_at=%s WHERE id=%s", ("2020-01-01T00:00:00+00:00", device["id"]))
    assert client.put(TOKEN_PATH, headers=headers, json={"token": "token"}).status_code == 401


def test_native_key_cannot_write_sender_and_unpaired_snapshot_is_refused(native):
    client, headers, _ = native
    assert client.get(PATH, headers=headers).status_code == 403
    assert client.put(PATH, headers=headers, json={"service_account": "not-secret"}).status_code == 405
    assert client.get("/api/v1/mobile/push-state").status_code == 401


def test_repair_or_unpair_after_auth_cannot_resurrect_token(native):
    _, _, stale = native
    pairing.revoke_paired_client("paired")
    assert not mobile.set_push_token(stale, "late-token")
    assert mobile.get_mobile_device("phone")["fcm_token"] is None