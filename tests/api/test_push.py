"""Owner-only write-only credentials and authenticated self-device token rotation."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from app.api import push
from app.core import encryption
from app.db import mobile, pairing, settings
from app.db.base import get_db

PATH = "/api/v1/settings/notifications"
TOKEN_PATH = "/api/v1/mobile/push-token"


def account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps({"type": "service_account", "project_id": "test-project", "client_email": "test@test-project.iam.gserviceaccount.com",
        "private_key": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        "token_uri": "https://oauth2.googleapis.com/token"})


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_sender_settings_require_owner(client, method):
    kwargs = {"json": {"service_account": "not-secret"}} if method == "put" else {}
    assert getattr(client, method)(PATH, **kwargs).status_code in (401, 403)


def test_sender_json_write_only_encrypted_and_removed(authenticated_client):
    raw = account()
    response = authenticated_client.put(PATH, json={"service_account": raw})
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": True, "devices_registered": 0}
    stored = settings.get_system_setting("fcm_service_account")
    assert encryption.is_encrypted(stored)
    assert json.loads(encryption.decrypt_value(stored)) == json.loads(raw)
    for result in (response, authenticated_client.get(PATH), authenticated_client.get("/api/settings")):
        assert "private_key" not in result.text and json.loads(raw)["private_key"] not in result.text
    assert authenticated_client.delete(PATH).json()["configured"] is False
    assert settings.get_system_setting("fcm_service_account") is None


@pytest.mark.parametrize("value", ["not json", "{}", '{"type":"service_account","token_uri":"https://evil.invalid"}'])
def test_bad_json_never_saved_or_echoed(authenticated_client, value):
    response = authenticated_client.put(PATH, json={"service_account": value})
    assert response.status_code == 400
    assert settings.get_system_setting("fcm_service_account") is None
    assert "evil.invalid" not in response.text


def test_validation_does_not_echo_pasted_secret(authenticated_client):
    response = authenticated_client.put(PATH, json={"service_account": {"private_key": "private-input-sentinel"}})
    assert response.status_code == 422
    assert "private-input-sentinel" not in response.text
    assert "no-store" in response.headers["cache-control"]


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
    assert client.put(PATH, headers=headers, json={"service_account": "not-secret"}).status_code == 403
    assert client.get("/api/v1/mobile/push-state").status_code == 401


def test_repair_or_unpair_after_auth_cannot_resurrect_token(native):
    _, _, stale = native
    pairing.revoke_paired_client("paired")
    assert not mobile.set_push_token(stale, "late-token")
    assert mobile.get_mobile_device("phone")["fcm_token"] is None
