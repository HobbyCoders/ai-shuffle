"""Local transport ownership is distinct from optional remote account access."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Depends, Request, WebSocket, HTTPException
from fastapi.testclient import TestClient

from app.api import auth, ws_auth
from app.core import owner_auth, encryption, account_hub, pairing
from app.core.config import settings
from app.db import database, mobile_hub, mobile, pairing as pairing_store

SERVER = '11111111-1111-1111-1111-111111111111'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
DEVICE = '33333333-3333-3333-3333-333333333333'
OTHER = '44444444-4444-4444-4444-444444444444'


class Surface:
    """Test-only trusted listener boundary. HTTP headers cannot set this mark."""
    def __init__(self, app, ingress):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        if self.ingress is not None:
            scope['shuffle.ingress'] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(settings, 'instance_id', 'local-auth-test')
    monkeypatch.setattr(settings, 'cookie_secure', False)
    monkeypatch.setattr(owner_auth.settings, 'shuffle_local_access', True)
    monkeypatch.setattr(account_hub.settings, 'mobile_account_hub_url', 'https://hub.example.com')
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(auth.direct_oauth, 'ensure_fresh_credentials', AsyncMock())
    monkeypatch.setattr(auth, '_sync_back_active_account_safe', lambda: None)
    monkeypatch.setattr(auth, 'is_github_authenticated_cached', lambda *args: False)
    monkeypatch.setattr(auth.auth_service, 'is_claude_authenticated', lambda: False)
    monkeypatch.setattr(auth.auth_service, 'get_claude_token_status', lambda: {'token_valid': False})
    from app.api.mobile_hub import _limiter
    _limiter.reset()
    result = FastAPI()
    result.include_router(auth.router)

    @result.api_route('/owner', methods=['GET', 'POST'])
    def admin(request: Request, identity=Depends(auth.require_admin)):
        return {'principal': identity, 'admin': request.state.is_admin}

    @result.get('/user')
    def user(request: Request, identity=Depends(auth.require_auth)):
        return {'principal': identity, 'admin': request.state.is_admin}

    @result.websocket('/ws')
    async def socket(websocket: WebSocket):
        valid, _ = await ws_auth.authenticate_websocket(websocket, websocket.query_params.get('token'))
        await websocket.accept()
        principal = getattr(websocket.state, 'mobile_tool_principal', None)
        await websocket.send_json({'authenticated': valid, 'kind': principal['kind'] if principal else None})
        await websocket.close()

    return result


def client(app, ingress='local', peer='192.168.1.50', origin='http://192.168.1.20:8000'):
    return TestClient(Surface(app, ingress), base_url=origin, client=(peer, 43210), headers={'Origin': origin})


def bind():
    mobile_hub.bind('https://hub.example.com', SERVER, encryption.encrypt_value('a' * 64),
                    'managed:' + settings.effective_instance_id)
    mobile_hub.activate()


def hub_mock(monkeypatch, identity=None):
    def call(binding, method, suffix, body=None):
        if suffix == 'browser-logins':
            assert body['device_name'] == 'Web browser'
            assert len(body['poll_challenge']) == 64
            return {'request_id': 'r' * 43,
                    'authorization_url': 'https://shuffle-connect.unraidhomeserver.com/browser/login?request_id='+'r'*43,
                    'expires_at': (owner_auth.now() + timedelta(minutes=10)).isoformat()}
        if suffix == 'browser-logins/poll':
            if 'completion_secret' not in body:
                assert set(body) == {'request_id', 'poll_secret'}
                return {'status': 'pending'}
            assert set(body) == {'request_id', 'poll_secret', 'completion_secret'}
            if body['completion_secret'] != 'c' * 43:
                raise HTTPException(401, 'Completion proof rejected')
            return {'status': 'approved', 'account_id': (identity or ACCOUNT), 'device_id': DEVICE,
                    'device_name': 'Owner browser'}
        assert suffix == 'devices/' + DEVICE
        return {'active': True, 'account_id': ACCOUNT, 'device_id': DEVICE}
    mock = Mock(side_effect=call)
    monkeypatch.setattr(account_hub, 'call', mock)
    return mock


def test_fresh_lan_setup_needs_no_password_or_account(app):
    c = client(app)
    before = c.get('/api/v1/auth/status').json()
    assert before['authenticated'] and before['is_admin'] and before['local_access']
    assert before['setup_required'] and not before['owner_linked']
    assert before['auth_mode'] == 'local'
    assert c.post('/api/v1/auth/setup', json={}).status_code == 200
    assert not c.get('/api/v1/auth/status').json()['setup_required']
    assert database.get_admin() is None
    assert mobile_hub.get_binding() is None
    assert c.post('/owner').status_code == 200


def test_unlinked_local_owner_can_start_account_connection_without_browser_signin(app, monkeypatch):
    from app.api import mobile_hub as hub_api
    from app.core import managed_connection

    app.include_router(hub_api.router, prefix='/api/v1/mobile')
    monkeypatch.setattr(settings, 'account_browser_origin', 'https://shuffle-connect.unraidhomeserver.com')
    monkeypatch.setattr(managed_connection.cloudflared, 'find_executable', lambda: '/test/cloudflared')
    monkeypatch.setattr(managed_connection, '_state', 'disconnected')
    monkeypatch.setattr(managed_connection, '_error', None)
    monkeypatch.setattr(managed_connection, '_url', None)
    hub_call = Mock(return_value={'ok': True})
    monkeypatch.setattr(account_hub, 'worker_link_call', hub_call)
    local = client(app)
    assert local.post('/api/v1/auth/setup', json={}).status_code == 200
    status = local.get('/api/v1/auth/status').json()
    assert status['authenticated'] and status['is_admin'] and status['local_access']
    assert not status['owner_linked'] and not status['account_sign_in_available']
    assert local.get('/api/v1/mobile/hub/connection').json()['status'] == 'disconnected'

    remote = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert remote.post('/api/v1/mobile/hub/connection', json={'name': 'Unraid'}).status_code == 403
    hub_call.assert_not_called()

    response = local.post('/api/v1/mobile/hub/connection', json={'name': 'Unraid'})
    assert response.status_code == 200
    connection = response.json()
    assert connection['status'] == 'waiting' and not connection['signed_in']
    assert connection['authorization_url'] == (
        'https://shuffle-connect.unraidhomeserver.com/browser/claim?code=' + connection['code'])
    assert len(connection['code']) == 12 and connection['qr_svg']
    assert hub_call.call_count == 1 and hub_call.call_args.args[0] == 'start'
    assert hub_call.call_args.args[1]['name'] == 'Unraid'
    assert mobile_hub.get_binding() is None
    assert local.get('/owner').status_code == 200
    assert not local.get('/api/v1/auth/status').json()['account_sign_in_available']


def test_workspace_metadata_needs_local_access_or_authenticated_remote(app, tmp_path, monkeypatch):
    from app.api import system
    app.include_router(system.router)
    monkeypatch.setattr(settings, 'workspace_dir', tmp_path)
    local = client(app)
    assert local.get('/api/v1/auth/status').json()['setup_required']
    response = local.get('/api/v1/workspace/config')
    assert response.status_code == 200 and not response.json()['is_configured']
    remote = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    denied = remote.get('/api/v1/workspace/config')
    assert denied.status_code in (401, 403)
    assert str(tmp_path) not in denied.text


@pytest.mark.parametrize('ingress,peer,origin,extra', [
    ('remote', '127.0.0.1', 'http://localhost:8001', {}),
    (None, '127.0.0.1', 'http://localhost:8000', {}),
    ('local', '8.8.8.8', 'http://192.168.1.20:8000', {}),
    ('local', '169.254.1.1', 'http://192.168.1.20:8000', {}),
    ('local', '100.64.1.1', 'http://192.168.1.20:8000', {}),
    ('local', '192.168.1.50', 'http://attacker.example:8000', {}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'X-Forwarded-For': '127.0.0.1'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'X-Forwarded-Host': 'localhost'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'Forwarded': 'for=127.0.0.1'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'CF-Connecting-IP': '192.168.1.50'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'Origin': 'https://attacker.example'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'Authorization': 'Bearer fake'}),
    ('local', '192.168.1.50', 'http://192.168.1.20:8000', {'Sec-Fetch-Site': 'cross-site'}),
])
def test_untrusted_requests_cannot_become_local_owner(app, ingress, peer, origin, extra):
    c = client(app, ingress, peer, origin)
    assert c.get('/owner', headers=extra).status_code == 403
    assert c.post('/api/v1/auth/setup', headers=extra, json={}).status_code == 403


def test_local_disable_flag_and_integration_key_are_respected(app, monkeypatch):
    c = client(app)
    database.create_api_user('integration', 'Connector', auth.hash_api_key('aih_test'))
    response = c.get('/user', headers={'Authorization': 'Bearer aih_test'})
    assert response.json() == {'principal': 'api_key:integration', 'admin': False}
    assert c.post('/owner', headers={'Authorization': 'Bearer aih_test'}).status_code == 403
    monkeypatch.setattr(owner_auth.settings, 'shuffle_local_access', False)
    assert c.get('/owner').status_code == 403


def test_local_websocket_requires_listener_and_matching_origin(app):
    for ingress, peer, origin, allowed in [
        ('local', '192.168.1.50', 'http://192.168.1.20:8000', True),
        ('remote', '127.0.0.1', 'http://localhost:8001', False),
        (None, '127.0.0.1', 'http://localhost:8000', False),
        ('local', '192.168.1.50', 'https://attacker.example', False),
    ]:
        c = client(app, ingress, peer)
        with c.websocket_connect('ws://192.168.1.20:8000/ws', headers={'Origin': origin}) as ws:
            assert ws.receive_json()['authenticated'] is allowed


def test_passwords_and_legacy_sessions_no_longer_authenticate(app):
    database.create_admin('old-owner', 'not-a-real-hash')
    database.create_auth_session('legacy-token', owner_auth.now() + timedelta(days=1))
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    c.cookies.set('session', 'legacy-token')
    assert c.get('/owner').status_code == 403
    for path in ('login', 'change-password'):
        assert c.post('/api/v1/auth/' + path, json={'username': 'old-owner', 'password': 'anything'}).status_code == 410
    assert database.get_auth_session('legacy-token') is None
    assert auth.auth_service.login('old-owner', 'anything') is None


def test_preference_identity_preserves_legacy_history_and_is_stable(app):
    database.create_admin('original-user', 'historical-hash')
    assert owner_auth.preference_identity() == 'original-user'
    assert not owner_auth.setup_required()
    with database.get_db() as conn:
        conn.cursor().execute("UPDATE admin SET username='changed-display'")
    assert owner_auth.preference_identity() == 'original-user'


def test_browser_signin_is_cookie_bound_one_use_and_uses_live_device_proof(app, monkeypatch, test_db):
    bind()
    hub_mock(monkeypatch)
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    start = c.post('/api/v1/auth/account/start', json={})
    assert start.status_code == 200
    assert set(start.json()) == {'request_id', 'authorization_url', 'expires_at'}
    assert 'HttpOnly' in start.headers['set-cookie']
    assert '; Secure' in start.headers['set-cookie']  # even with COOKIE_SECURE=false
    stored = test_db.execute('SELECT * FROM owner_browser_requests').fetchone()
    assert stored['encrypted_request'].startswith('enc:')
    assert len(stored['proof_hash']) == 64
    other = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert other.post('/api/v1/auth/account/poll', json={}).status_code == 401
    assert c.post('/api/v1/auth/account/poll', json={}).json() == {'status': 'pending'}
    assert c.post('/api/v1/auth/account/poll', json={'completion_secret': 'x' * 43}).status_code == 401
    assert c.post('/api/v1/auth/account/poll', json={'completion_secret': 'c' * 43}).json() == {'status': 'authenticated'}
    assert c.get('/owner').status_code == 200
    token = c.cookies.get('session')
    assert token.startswith('shc_')
    assert database.get_auth_session(token) is None
    row = mobile.get_mobile_device(mobile_hub.local_device_id(SERVER, DEVICE))
    assert row['platform'] == 'browser' and row['scopes'] == ['controller', 'web:session']
    assert test_db.execute('SELECT COUNT(*) FROM owner_browser_requests').fetchone()[0] == 0
    assert c.post('/api/v1/auth/account/poll', json={}).status_code == 401
    monkeypatch.setattr(account_hub, 'call', lambda *a, **k: {'active': False})
    assert c.get('/owner').status_code == 403


def browser_login(app, monkeypatch):
    bind()
    hub_mock(monkeypatch)
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert c.post('/api/v1/auth/account/start', json={}).status_code == 200
    assert c.post('/api/v1/auth/account/poll', json={'completion_secret': 'c' * 43}).status_code == 200
    return c


def test_invalid_query_token_cannot_bypass_cookie_origin(app, monkeypatch):
    c = browser_login(app, monkeypatch)
    with c.websocket_connect('wss://worker.example.com/ws?token=invalid', headers={'Origin': 'https://evil.example.com'}) as ws:
        assert ws.receive_json()['authenticated'] is False
    with c.websocket_connect('wss://worker.example.com/ws?token=invalid') as ws:
        assert ws.receive_json()['authenticated'] is True
    token = c.cookies.get('session')
    c.cookies.clear()
    with c.websocket_connect('/ws?token=' + token, headers={'Origin': 'https://elsewhere.example'}) as ws:
        assert ws.receive_json()['authenticated'] is True  # explicit possession


def test_recovery_cookie_with_invalid_token_still_requires_origin(app):
    c = client(app, peer='127.0.0.1', origin='http://localhost')
    with database.get_db() as conn:
        conn.cursor().execute('INSERT INTO owner_local_sessions VALUES (%s,%s,%s,%s)',
            (owner_auth.digest('shr_recovery'), settings.effective_instance_id, 'recovery', '2999-01-01'))
    c.cookies.set('session', 'shr_recovery')
    with c.websocket_connect('/ws?token=invalid', headers={'Origin': 'http://evil.localhost'}) as ws:
        assert ws.receive_json()['authenticated'] is False


def test_local_account_chat_continuation_preserves_provenance(app, monkeypatch, test_db):
    from app.core import mobile_tool_identity as identity
    from app.db import mobile_tool_identity as provenance
    c = browser_login(app, monkeypatch)
    token = c.cookies.get('session')
    browser = auth.get_paired_client_from_token(token)
    remote = identity.principal('paired', browser['id'], token)
    local = identity.principal('admin', 'install', 'local-transport:' + settings.effective_instance_id)
    for sid, original, arriving in [('local-chat', local, remote), ('remote-chat', remote, local)]:
        database.create_session(sid, 'default')
        identity.bind_session(sid, original, new=True)
        before = provenance.read(sid)
        identity.bind_session(sid, arriving)
        assert provenance.read(sid) == before
        test_db.execute('UPDATE sessions SET instance_id=? WHERE id=?', ('another-worker', sid))
        with pytest.raises(PermissionError):
            identity.bind_session(sid, arriving)


def test_local_and_account_preference_identity_match_without_device_or_worker_keys(app, monkeypatch):
    from app.core.preference_identity import principal_preference_identity, request_preference_identity
    from types import SimpleNamespace
    c = browser_login(app, monkeypatch)
    paired = auth.get_paired_client_from_token(c.cookies.get('session'))
    expected = ('account', 'https://hub.example.com#' + ACCOUNT)
    assert principal_preference_identity({'kind': 'admin'}) == expected
    assert request_preference_identity(SimpleNamespace(state=SimpleNamespace(paired_client=paired))) == expected
    assert principal_preference_identity({'kind': 'paired', 'principal_id': 'different-device-on-other-worker',
                                          'account_id': ACCOUNT}) == expected
    assert principal_preference_identity({'kind': 'paired', 'principal_id': 'other',
                                          'account_id': OTHER}) != expected
    assert principal_preference_identity({'kind': 'paired', 'principal_id': 'direct'}) == ('paired', 'direct')


def test_owner_browser_can_authorize_only_same_account_native_targets(app, monkeypatch):
    from app.core import mobile_tool_identity as identity
    from app.core.models import DeviceInfo
    c = browser_login(app, monkeypatch)
    token = c.cookies.get('session')
    browser = auth.get_paired_client_from_token(token)
    database.create_session('browser-chat', 'default')
    identity.bind_session('browser-chat', identity.principal('paired', browser['id'], token), new=True)
    mobile_hub.enroll({'device_id': OTHER, 'account_id': ACCOUNT, 'server_id': SERVER, 'device_name': 'Phone'},
        DeviceInfo(device_id=OTHER, device_name='Phone', platform='android', capabilities={}),
        'testhash', owner_auth.now() + timedelta(days=1), list(pairing.DEFAULT_MOBILE_SCOPES), scoped=True)
    monkeypatch.setattr(account_hub, 'call', lambda binding, method, suffix, body=None:
                        {'active': True, 'account_id': ACCOUNT, 'device_id': suffix.split('/')[-1]})
    phone = mobile_hub.local_device_id(SERVER, OTHER)
    assert identity.authorized_device_ids('browser-chat') == [phone]
    # Browser never becomes a native target itself.
    assert browser['device_id'] not in identity.authorized_device_ids('browser-chat')
    mobile_hub.disconnect()
    assert identity.authorized_device_ids('browser-chat') == []


@pytest.mark.parametrize('hub_result', ['denied', 'approved_without_completion_proof'])
def test_browser_status_only_poll_reports_denial_but_cannot_enroll(app, monkeypatch, test_db, hub_result):
    bind()
    hub_mock(monkeypatch)
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert c.post('/api/v1/auth/account/start', json={}).status_code == 200

    def status_only(binding, method, suffix, body):
        assert suffix == 'browser-logins/poll'
        assert set(body) == {'request_id', 'poll_secret'}
        if hub_result == 'denied':
            raise HTTPException(403, 'Account authorization denied.')
        return {'status': 'approved', 'account_id': ACCOUNT, 'device_id': DEVICE}

    monkeypatch.setattr(account_hub, 'call', status_only)
    response = c.post('/api/v1/auth/account/poll', json={})
    assert response.status_code == (403 if hub_result == 'denied' else 401)
    assert not c.cookies.get('session')
    assert test_db.execute('SELECT COUNT(*) FROM mobile_worker_authorizations').fetchone()[0] == 0
    assert test_db.execute('SELECT COUNT(*) FROM owner_browser_requests').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['wrong_account', 'expired', 'wrong_worker', 'wrong_instance', 'bad_origin'])
def test_browser_transaction_fails_closed(app, monkeypatch, test_db, failure):
    bind()
    hub_mock(monkeypatch, OTHER if failure == 'wrong_account' else None)
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert c.post('/api/v1/auth/account/start', json={}).status_code == 200
    if failure == 'expired':
        test_db.execute("UPDATE owner_browser_requests SET expires_at='2000-01-01'")
    elif failure == 'wrong_worker':
        test_db.execute('UPDATE owner_browser_requests SET server_id=?', (OTHER,))
    elif failure == 'wrong_instance':
        test_db.execute("UPDATE owner_browser_requests SET instance_id='other'")
    response = c.post('/api/v1/auth/account/poll', json={'completion_secret': 'c' * 43},
                      headers={'Origin': 'https://attacker.example'} if failure == 'bad_origin' else {})
    assert response.status_code in (401, 403)
    assert test_db.execute('SELECT COUNT(*) FROM mobile_worker_authorizations').fetchone()[0] == 0


def test_console_recovery_is_one_use_loopback_only_and_short_lived(app, monkeypatch, test_db):
    monkeypatch.setattr(owner_auth.settings, 'shuffle_local_access', False)
    code = owner_auth.issue_console_grant('recovery')
    remote = client(app, 'remote', '127.0.0.1', 'http://localhost:8001')
    assert remote.post('/api/v1/auth/recovery', json={'code': code}).status_code == 403
    local = client(app, 'local', '127.0.0.1', 'http://localhost:8000')
    response = local.post('/api/v1/auth/recovery', json={'code': code})
    assert response.status_code == 200
    assert local.get('/owner').status_code == 200
    assert local.post('/api/v1/auth/recovery', json={'code': code}).status_code == 401
    token = local.cookies.get('session')
    remote.cookies.set('session', token)
    assert remote.get('/owner').status_code == 403
    test_db.execute("UPDATE owner_local_sessions SET expires_at='2000-01-01'")
    assert local.get('/owner').status_code == 403


def test_api_user_web_login_cannot_be_enabled():
    from app.core.models import ApiUserCreate, ApiUserUpdate
    from pydantic import ValidationError
    for schema, body in [(ApiUserCreate, {'name': 'integration', 'web_login_allowed': True}),
                         (ApiUserUpdate, {'web_login_allowed': True})]:
        with pytest.raises(ValidationError):
            schema(**body)


def test_local_transport_does_not_promote_explicit_integration_or_controller(app):
    database.create_api_user('integration', 'Connector', auth.hash_api_key('aih_ws_key'))
    c = client(app)
    with c.websocket_connect('ws://192.168.1.20:8000/ws?token=aih_ws_key') as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'api_user'}
    database.create_pairing_grant(token_hash='direct-proof', purpose='controller', scopes=['controller'],
        target_install_id=None, endpoint_url=None, device_label=None, created_by='local',
        expires_at=owner_auth.now() + timedelta(minutes=2))
    token = 'shc_direct-controller'
    database.exchange_grant_for_client(token_hash='direct-proof', device_id='direct-browser',
        device_name='Direct controller', platform='web', client_type='browser',
        session_token_hash=pairing.hash_controller_session(token),
        session_expires_at=owner_auth.now() + timedelta(days=1))
    c.cookies.set('session', token)
    assert not c.get('/api/v1/auth/status').json()['is_admin']
    assert c.get('/owner').status_code == 403
    with c.websocket_connect('ws://192.168.1.20:8000/ws') as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'paired'}


def test_local_status_clears_retired_cookie_before_websocket(app):
    c = client(app)
    c.cookies.set('session', 'retired-password-token', domain='192.168.1.20', path='/')
    response = c.get('/api/v1/auth/status')
    assert response.json()['local_access'] and response.json()['authenticated']
    assert 'session=""' in response.headers['set-cookie']
    with c.websocket_connect('ws://192.168.1.20:8000/ws') as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'admin'}


def test_imported_owner_schema_changes_migration_fingerprint(monkeypatch):
    from pathlib import Path
    target = Path(owner_auth.__file__)
    original = Path.read_bytes
    before = database.migrations_fingerprint()
    monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) +
                        (b'\n# next owner schema migration' if path == target else b''))
    assert before and database.migrations_fingerprint() != before


def test_existing_schema_upgrade_creates_owner_tables(test_db):
    for table in ('owner_browser_requests', 'owner_local_sessions', 'owner_console_grants'):
        test_db.execute('DROP TABLE ' + table)
    database.set_system_setting(database.MIGRATIONS_FINGERPRINT_KEY, 'previous-build')
    database.init_database()
    for table in ('owner_browser_requests', 'owner_local_sessions', 'owner_console_grants'):
        assert test_db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
