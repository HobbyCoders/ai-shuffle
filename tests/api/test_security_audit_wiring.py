"""Security actions must reach the durable audit sink, without secret material."""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request, Response

from app.api import auth, api_users
from app.core.models import ApiUserCreate, ApiUserUpdate


@pytest.fixture
def audit(monkeypatch):
    sink = MagicMock(return_value={})
    monkeypatch.setattr(auth.db, "create_audit_log", sink)
    monkeypatch.setattr(auth.db, "get_admin", lambda: {"username": "admin", "password_hash": "hash"})
    return sink


@pytest.fixture
def http_request():
    return Request({"type": "http", "headers": [(b"cookie", b"session=test-session")],
                    "client": ("127.0.0.1", 12)})


async def test_setup_audits_actor_without_password(audit, http_request, monkeypatch):
    monkeypatch.setattr(auth.auth_service, "is_setup_required", lambda: True)
    monkeypatch.setattr(auth.auth_service, "setup_admin", lambda *a: {"token": "secret", "admin": {"username": "new-admin"}})
    monkeypatch.setattr(auth.db, "set_system_setting", lambda *a: None)
    monkeypatch.setattr(auth.encryption, "set_encryption_key", lambda *a: None)
    monkeypatch.setattr(auth, "_seed_claude_account_safe", lambda: None)
    await auth.setup_admin(auth.SetupRequest(username="new-admin", password="secret-password"), Response(), http_request)
    assert audit.call_args.kwargs["event_type"] == "admin_setup"
    assert audit.call_args.kwargs["user_id"] == "new-admin"
    assert "secret" not in str(audit.call_args)


async def test_logout_records_valid_admin_session(audit, http_request, monkeypatch):
    monkeypatch.setattr(auth.db, "get_auth_session", lambda token: {"token": token})
    monkeypatch.setattr(auth.auth_service, "logout", lambda token: None)
    await auth.logout(http_request, Response())
    assert audit.call_args.kwargs["event_type"] == "logout"
    assert audit.call_args.kwargs["user_id"] == "admin"
    assert "test-session" not in str(audit.call_args)


@pytest.mark.parametrize("valid,new_password,expected", [
    (False, "new-password", "invalid_current_password"),
    (True, "old-password", "unchanged_password"),
])
async def test_rejected_password_change_is_audited(audit, http_request, monkeypatch, valid, new_password, expected):
    monkeypatch.setattr(auth.auth_service, "verify_password", lambda *a: valid)
    with pytest.raises(HTTPException):
        await auth.change_password(http_request, auth.ChangePasswordRequest(
            current_password="old-password", new_password=new_password), token="admin")
    assert audit.call_args.kwargs["event_type"] == "password_change_failed"
    assert audit.call_args.kwargs["details"] == {"reason": expected}
    assert "old-password" not in str(audit.call_args)


async def test_successful_password_change_is_audited(audit, http_request, monkeypatch):
    monkeypatch.setattr(auth.auth_service, "verify_password", lambda *a: True)
    monkeypatch.setattr(auth.encryption, "re_encrypt_all_secrets", lambda *a: True)
    monkeypatch.setattr(auth.db, "update_admin_password", lambda *a: None)
    await auth.change_password(http_request, auth.ChangePasswordRequest(
        current_password="old-password", new_password="new-password"), token="admin")
    assert audit.call_args.kwargs["event_type"] == "password_changed"
    assert "password" not in str(audit.call_args.kwargs["details"])


async def test_api_key_lifecycle_audits_identity_not_key(audit, http_request, monkeypatch):
    user = {"id": "key-id", "name": "test integration"}
    monkeypatch.setattr(api_users.database, "create_api_user", lambda **kw: user)
    monkeypatch.setattr(api_users.database, "get_api_user", lambda uid: user)
    monkeypatch.setattr(api_users.database, "update_api_user", lambda *a, **kw: user)
    monkeypatch.setattr(api_users.database, "delete_api_user", lambda uid: True)
    created = await api_users.create_api_user_endpoint(ApiUserCreate(name=user["name"]), token="admin", request=http_request)
    await api_users.update_api_user_endpoint("key-id", ApiUserUpdate(is_active=False), token="admin", request=http_request)
    rotated = await api_users.rotate_api_user_key("key-id", token="admin", request=http_request)
    await api_users.delete_api_user_endpoint("key-id", token="admin", request=http_request)
    assert [call.kwargs["event_type"] for call in audit.call_args_list] == [
        "api_key_created", "api_key_disabled", "api_key_rotated", "api_key_deleted"]
    for call in audit.call_args_list:
        assert call.kwargs["user_id"] == "admin"
        assert call.kwargs["details"] == {"api_key_id": "key-id", "api_key_name": user["name"]}
    assert created["api_key"] not in str(audit.call_args_list)
    assert rotated["api_key"] not in str(audit.call_args_list)


async def test_failed_audit_lookup_does_not_lose_rotated_key(http_request, monkeypatch):
    user = {"id": "key-id", "name": "test integration"}
    monkeypatch.setattr(api_users.database, "get_api_user", lambda uid: user)
    updated = MagicMock(return_value=user)
    monkeypatch.setattr(api_users.database, "update_api_user", updated)
    monkeypatch.setattr(api_users.database, "get_admin", MagicMock(side_effect=RuntimeError("audit unavailable")))
    rotated = await api_users.rotate_api_user_key("key-id", token="admin", request=http_request)
    updated.assert_called_once()
    assert rotated["api_key"].startswith("aih_")
