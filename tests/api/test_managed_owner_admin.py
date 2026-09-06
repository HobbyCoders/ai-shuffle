"""Managed owner administration uses live device proof, never a shared admin token."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import auth, mobile, mobile_hub, rate_limits, settings as settings_api
from app.core import account_hub, encryption, pairing
from app.core.config import settings
from app.core.models import DeviceInfo
from app.db import database, mobile_hub as hub_store, pairing as pairing_store

HUB = 'https://hub.example.com'
SERVER = '11111111-1111-1111-1111-111111111111'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
DEVICE = '33333333-3333-3333-3333-333333333333'
OTHER = '44444444-4444-4444-4444-444444444444'


@pytest.fixture
def owner_app(monkeypatch):
    monkeypatch.setenv('MOBILE_ACCOUNT_HUB_URL', HUB)
    monkeypatch.setattr(settings, 'instance_id', 'owner-test-install')
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(auth.direct_oauth, 'ensure_fresh_credentials', AsyncMock())
    monkeypatch.setattr(auth, '_sync_back_active_account_safe', lambda: None)
    monkeypatch.setattr(auth, 'is_github_authenticated_cached', lambda *args: False)
    monkeypatch.setattr(auth.auth_service, 'is_claude_authenticated', lambda: False)
    monkeypatch.setattr(auth.auth_service, 'get_claude_token_status', lambda: {'token_valid': False})
    monkeypatch.setattr(auth.auth_service, 'get_admin_username', lambda: 'Local owner')
    monkeypatch.setattr(auth.auth_service, 'is_setup_required', lambda: False)
    monkeypatch.setattr(auth.auth_service, 'is_onboarding_complete', lambda: True)
    monkeypatch.setattr(auth.audit_service, 'log_logout', lambda *args: None)
    mobile_hub._limiter.reset()
    app = FastAPI()
    for router in (auth.router, mobile.router, settings_api.router, rate_limits.router):
        app.include_router(router)

    @app.get('/principal')
    def principal(request: Request, token=Depends(auth.require_auth)):
        return {'admin': request.state.is_admin, 'principal': token,
                'device_id': request.state.paired_client['device_id']}

    return app


def bind_and_enroll(monkeypatch, linked_by='managed:owner-test-install'):
    hub_store.bind(HUB, SERVER, encryption.encrypt_value('a' * 64), linked_by)
    hub_store.activate()
    hub_store.enroll(
        {'device_id': DEVICE, 'account_id': ACCOUNT, 'server_id': SERVER, 'device_name': 'Owner phone'},
        DeviceInfo(device_id=DEVICE, device_name='Owner phone', platform='android', capabilities={}),
        auth.hash_api_key('aim_owner-test'), datetime.now(timezone.utc) + timedelta(days=1),
        list(pairing.DEFAULT_MOBILE_SCOPES))
    backchannel = Mock(return_value={'active': True, 'account_id': ACCOUNT, 'device_id': DEVICE})
    monkeypatch.setattr(account_hub, 'call', backchannel)
    return backchannel


def web_session(app):
    client = TestClient(app)
    response = client.post('/api/v1/mobile/web-session', headers={'Authorization': 'Bearer aim_owner-test'})
    assert response.status_code == 200, response.text
    token = response.json()['session_token']
    assert token.startswith('shc_')
    client.cookies.set('session', token)
    return client, token


def change_voice(client, **kwargs):
    return client.put('/api/v1/settings/voice', json={'voice_max_call_minutes': 37}, **kwargs)


def test_existing_managed_owner_gets_all_admin_settings_and_keeps_device_identity(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    client, token = web_session(owner_app)
    status = client.get('/api/v1/auth/status').json()
    assert status['authenticated'] and status['is_admin'] and status['paired_client']
    assert change_voice(client).status_code == 200
    assert client.get('/api/v1/settings/voice').json()['voice_max_call_minutes'] == 37
    assert client.get('/api/v1/mobile/devices').status_code == 200
    assert client.get('/api/v1/rate-limits').status_code == 200
    owner = hub_store.ownership(DEVICE)
    assert client.get('/principal').json() == {
        'admin': True, 'principal': 'paired_client:' + owner['paired_client_id'], 'device_id': DEVICE}
    # No normal admin session or stored role is created; a fresh client re-derives
    # authority from the same durable binding and live proof.
    assert database.get_auth_session(token) is None
    raw_client = pairing_store.get_paired_client_by_session_hash(pairing.hash_controller_session(token))
    assert 'account_owner_admin' not in raw_client
    fresh = TestClient(owner_app)
    fresh.cookies.set('session', token)
    assert change_voice(fresh).status_code == 200
    # Explicit bearer transport for the same device-bound session uses the same
    # owner check; aim_ and integration keys do not take this path.
    fresh.cookies.clear()
    assert change_voice(fresh, headers={'Authorization': 'Bearer ' + token}).status_code == 200


@pytest.mark.parametrize('linked_by', ['pcl_direct-controller', 'managed:another-install'])
def test_manual_link_or_other_install_never_promotes(owner_app, monkeypatch, linked_by):
    bind_and_enroll(monkeypatch, linked_by)
    client, _ = web_session(owner_app)
    status = client.get('/api/v1/auth/status').json()
    assert status['authenticated'] and not status['is_admin']
    assert client.get('/api/v1/settings/voice').status_code == 200
    assert change_voice(client).status_code == 403
    assert client.get('/principal').json()['admin'] is False


@pytest.mark.parametrize('failure', [
    'hub_revoked', 'wrong_account', 'wrong_hub_device', 'hub_unavailable',
    'local_device_revoked', 'local_device_expired', 'paired_revoked', 'session_expired',
    'inactive_binding', 'wrong_server', 'wrong_client_device', 'wrong_authorization_device', 'wrong_mobile_client',
])
def test_copied_owner_session_loses_admin_on_any_failed_ownership_proof(owner_app, monkeypatch, test_db, failure):
    call = bind_and_enroll(monkeypatch)
    client, _ = web_session(owner_app)
    assert change_voice(client).status_code == 200
    if failure == 'hub_revoked':
        call.return_value = {'active': False}
    elif failure == 'wrong_account':
        call.return_value = {'active': True, 'account_id': OTHER, 'device_id': DEVICE}
    elif failure == 'wrong_hub_device':
        call.return_value = {'active': True, 'account_id': ACCOUNT, 'device_id': OTHER}
    elif failure == 'hub_unavailable':
        call.side_effect = HTTPException(503, 'Unavailable')
    else:
        statement = {
            'local_device_revoked': "UPDATE mobile_devices SET revoked_at='2026-01-01'",
            'local_device_expired': "UPDATE mobile_devices SET api_key_expires_at='2020-01-01T00:00:00+00:00'",
            'paired_revoked': "UPDATE paired_clients SET revoked_at='2026-01-01'",
            'session_expired': "UPDATE paired_clients SET session_expires_at='2020-01-01T00:00:00+00:00'",
            'inactive_binding': 'UPDATE mobile_hub_binding SET active=FALSE',
            'wrong_server': f"UPDATE mobile_hub_authorizations SET server_id='{OTHER}'",
            'wrong_client_device': f"UPDATE paired_clients SET device_id='{OTHER}'",
            'wrong_authorization_device': f"UPDATE mobile_hub_authorizations SET hub_device_id='{OTHER}'",
            'wrong_mobile_client': "UPDATE mobile_devices SET paired_client_id='pcl_other'",
        }[failure]
        test_db.execute(statement)
    assert change_voice(client).status_code == 403
    status = client.get('/api/v1/auth/status').json()
    assert not status['authenticated'] and not status['is_admin']
    assert client.get('/api/v1/mobile/devices').status_code == 403


def test_removing_web_grant_drops_admin_without_minting_a_replacement(owner_app, monkeypatch, test_db):
    bind_and_enroll(monkeypatch)
    client, token = web_session(owner_app)
    assert change_voice(client).status_code == 200
    test_db.execute("UPDATE mobile_devices SET scopes='[\"controller\"]'")
    assert change_voice(client).status_code == 403
    assert client.get('/api/v1/auth/status').json()['is_admin'] is False
    assert database.get_auth_session(token) is None


def test_direct_pairing_and_integration_credentials_cannot_claim_owner(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    database.create_pairing_grant(token_hash='direct-grant', purpose='controller',
        scopes=list(pairing.DEFAULT_MOBILE_SCOPES), target_install_id=None, endpoint_url=None,
        device_label=None, created_by='administrator', expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    result = database.exchange_grant_for_mobile(token_hash='direct-grant', device_id=OTHER,
        device_name='Direct phone', platform='android', capabilities={}, api_key_hash=auth.hash_api_key('aim_direct'),
        api_key_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    direct_token = pairing.generate_controller_session()
    database.set_paired_client_session(result['client']['id'], pairing.hash_controller_session(direct_token),
        datetime.now(timezone.utc) + timedelta(days=1))
    database.create_api_user('integration', 'Integration', auth.hash_api_key('aih_test'))
    client = TestClient(owner_app)
    client.cookies.set('session', direct_token)
    assert client.get('/api/v1/auth/status').json()['is_admin'] is False
    assert change_voice(client).status_code == 403
    client.cookies.clear()
    for token in (direct_token, 'aim_owner-test', 'aim_direct', 'aih_test'):
        assert change_voice(client, headers={'Authorization': 'Bearer ' + token,
            'X-Account-Id': ACCOUNT, 'X-Is-Admin': 'true'}).status_code == 403


@pytest.mark.asyncio
async def test_owner_keeps_account_scoped_websocket_principal_and_revocation(owner_app, monkeypatch):
    from app.api.ws_auth import authenticate_websocket
    from app.core import mobile_tool_identity
    call = bind_and_enroll(monkeypatch)
    _, token = web_session(owner_app)
    socket = Mock(cookies={})
    assert await authenticate_websocket(socket, token) == (True, None)
    principal = socket.state.mobile_tool_principal
    assert principal['kind'] == 'paired'
    assert principal['account_id'] == ACCOUNT
    assert principal['owner'] == 'account:' + ACCOUNT
    call.return_value = {'active': False}
    with pytest.raises((HTTPException, PermissionError)):
        mobile_tool_identity.validate(principal)


def test_owner_web_logout_invalidates_the_derived_session(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    client, token = web_session(owner_app)
    assert client.post('/api/v1/auth/logout').status_code == 200
    client.cookies.set('session', token)
    assert change_voice(client).status_code == 403
    assert not client.get('/api/v1/auth/status').json()['authenticated']


def test_owner_admin_cookie_retains_same_origin_csrf_protection(owner_app, monkeypatch):
    from app.middleware.csrf import CSRFProtectionMiddleware
    monkeypatch.setattr(settings, 'cors_origins', 'https://worker.example.com')
    owner_app.add_middleware(CSRFProtectionMiddleware)
    bind_and_enroll(monkeypatch)
    _, token = web_session(owner_app)
    client = TestClient(owner_app, base_url='https://worker.example.com', client=('203.0.113.5', 50123))
    client.cookies.set('session', token)
    assert change_voice(client, headers={'Origin': 'https://attacker.example.com'}).status_code == 403
    assert change_voice(client, headers={'Origin': 'https://worker.example.com'}).status_code == 200
