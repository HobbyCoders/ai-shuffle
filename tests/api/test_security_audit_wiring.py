"""Security actions must reach the durable audit sink, without secret material."""
from unittest.mock import MagicMock
from datetime import timedelta

import pytest
from fastapi import HTTPException, Request, Response

from app.api import auth, api_users
from app.core import owner_auth
from app.db import pairing as pairing_store, mobile_hub
from app.core.models import ApiUserCreate, ApiUserUpdate


@pytest.fixture
def audit(monkeypatch):
    sink = MagicMock(return_value={})
    monkeypatch.setattr(auth.db, "create_audit_log", sink)
    monkeypatch.setattr(auth.db, "get_admin", lambda: {"username": "admin", "password_hash": "hash"})
    monkeypatch.setattr(owner_auth, 'preference_identity', lambda: 'admin')
    return sink


@pytest.fixture
def http_request():
    return Request({"type": "http", "method": "POST", "scheme": "http", "path": "/api/v1/auth/logout",
                    "headers": [(b"cookie", b"session=test-session"), (b"host", b"localhost:8000"),
                                (b"origin", b"http://localhost:8000")],
                    "shuffle.ingress": "local", "server": ("127.0.0.1", 8000),
                    "client": ("127.0.0.1", 12)})


async def test_passwordless_setup_audits_stable_actor(audit, http_request, monkeypatch):
    configure = MagicMock()
    monkeypatch.setattr(owner_auth, 'configure_installation', configure)
    await auth.configure_local_install(http_request, 'local-owner')
    configure.assert_called_once()
    assert audit.call_args.kwargs['event_type'] == 'installation_setup'
    assert audit.call_args.kwargs['user_id'] == 'admin'
    assert audit.call_args.kwargs['details'] == {'method': 'local_setup'}
    assert 'test-session' not in str(audit.call_args)


async def test_logout_records_valid_admin_session(audit, http_request, monkeypatch):
    monkeypatch.setattr(auth.db, "get_auth_session", lambda token: {"token": token})
    monkeypatch.setattr(auth.auth_service, "logout", lambda token: None)
    await auth.logout(http_request, Response())
    assert audit.call_args.kwargs["event_type"] == "logout"
    assert audit.call_args.kwargs["user_id"] == "admin"
    assert "test-session" not in str(audit.call_args)


async def test_retired_password_endpoints_do_not_mutate_or_audit_success(audit, monkeypatch):
    mutate = MagicMock()
    monkeypatch.setattr(auth.db, 'update_admin_password', mutate)
    monkeypatch.setattr(auth.encryption, 'set_encryption_key', mutate)
    with pytest.raises(HTTPException) as rejected:
        await auth.retired_password_login()
    assert rejected.value.status_code == 410
    mutate.assert_not_called()
    audit.assert_not_called()


async def test_account_login_audits_immutable_account_without_proofs(audit, http_request, monkeypatch):
    secret = 'shc_' + 's' * 43
    monkeypatch.setattr(owner_auth, 'poll_browser', lambda *a: (secret, owner_auth.now() + timedelta(days=1)))
    monkeypatch.setattr(pairing_store, 'get_paired_client_by_session_hash', lambda *a: {'id': 'paired-browser'})
    monkeypatch.setattr(mobile_hub, 'ownership', lambda **kw: {'account_id': 'immutable-account-id'})
    result = await auth.poll_account_login(auth.BrowserPollRequest(completion_secret='c' * 43), http_request, Response())
    assert result == {'status': 'authenticated'}
    assert audit.call_args.kwargs['event_type'] == 'login_success'
    assert audit.call_args.kwargs['user_id'] == 'immutable-account-id'
    assert audit.call_args.kwargs['user_type'] == 'account'
    assert audit.call_args.kwargs['details'] == {'method': 'account'}
    assert secret not in str(audit.call_args) and 'c' * 43 not in str(audit.call_args)


async def test_pending_account_login_does_not_audit_success(audit, http_request, monkeypatch):
    monkeypatch.setattr(owner_auth, 'poll_browser', lambda *a: None)
    assert await auth.poll_account_login(auth.BrowserPollRequest(), http_request, Response()) == {'status': 'pending'}
    audit.assert_not_called()


async def test_local_recovery_audits_identity_without_code(audit, http_request, monkeypatch):
    code = 'r' * 43
    secret = 'shr_' + 's' * 43
    monkeypatch.setattr(owner_auth, 'consume_console_grant', lambda *a: (secret, owner_auth.now() + timedelta(minutes=15)))
    result = await auth.local_recovery(auth.LocalRecoveryRequest(code=code), http_request, Response())
    assert result['local_recovery']
    assert audit.call_args.kwargs['event_type'] == 'login_success'
    assert audit.call_args.kwargs['user_id'] == 'admin'
    assert audit.call_args.kwargs['details'] == {'method': 'local_recovery'}
    assert code not in str(audit.call_args) and secret not in str(audit.call_args)


@pytest.mark.parametrize('method', ['account', 'local_recovery'])
async def test_rejected_owner_login_audits_only_fixed_reason(audit, http_request, monkeypatch, method):
    proof = 'p' * 43
    rejected = MagicMock(side_effect=HTTPException(401, 'Unsafe provider detail ' + proof))
    if method == 'account':
        monkeypatch.setattr(owner_auth, 'poll_browser', rejected)
        operation = auth.poll_account_login(auth.BrowserPollRequest(completion_secret=proof), http_request, Response())
    else:
        monkeypatch.setattr(owner_auth, 'consume_console_grant', rejected)
        operation = auth.local_recovery(auth.LocalRecoveryRequest(code=proof), http_request, Response())
    with pytest.raises(HTTPException):
        await operation
    assert audit.call_args.kwargs['event_type'] == 'login_failure'
    assert audit.call_args.kwargs['user_id'] is None
    assert audit.call_args.kwargs['details'] == {'method': method, 'reason': 'rejected'}
    assert proof not in str(audit.call_args) and 'Unsafe provider' not in str(audit.call_args)


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
