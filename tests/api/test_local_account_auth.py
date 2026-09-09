"""Local transport ownership is distinct from optional remote account access."""
import secrets
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Depends, Request, WebSocket
from fastapi.testclient import TestClient

from app.api import auth, ws_auth
from app.core import owner_auth, encryption, account_hub, pairing
from app.core.config import settings
from app.core.models import DeviceCapabilities
from app.db import database, mobile_hub

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


def hub_mock(monkeypatch):
    def call(binding, method, suffix, body=None):
        assert suffix == 'devices/' + DEVICE
        return {'active': True, 'account_id': ACCOUNT, 'device_id': DEVICE}
    mock = Mock(side_effect=call)
    monkeypatch.setattr(account_hub, 'call', mock)
    return mock


def paired_browser(app, monkeypatch):
    """A remote browser the account hub authorized as this worker's owner (a paired controller)."""
    bind()
    hub_mock(monkeypatch)
    token = pairing.generate_controller_session()
    mobile_hub.enroll({'account_id': ACCOUNT, 'device_id': DEVICE, 'server_id': SERVER, 'device_name': 'Web browser'},
                      SimpleNamespace(platform='browser', capabilities=DeviceCapabilities()),
                      owner_auth.digest(secrets.token_hex(32)), owner_auth.now() + timedelta(days=7),
                      ['controller', 'web:session'], session_token_hash=pairing.hash_controller_session(token),
                      scoped=True)
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    c.cookies.set('session', token)
    return c


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


def test_unlinked_local_owner_can_start_account_connection(app, monkeypatch):
    from app.api import mobile_hub as hub_api
    from app.core import managed_connection

    app.include_router(hub_api.router, prefix='/api/v1/mobile')
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
    assert not status['owner_linked']
    assert local.get('/api/v1/mobile/hub/connection').json()['status'] == 'disconnected'

    remote = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    assert remote.post('/api/v1/mobile/hub/connection', json={'name': 'Unraid'}).status_code == 403
    hub_call.assert_not_called()

    response = local.post('/api/v1/mobile/hub/connection', json={'name': 'Unraid'})
    assert response.status_code == 200
    connection = response.json()
    assert connection['status'] == 'waiting' and not connection['signed_in']
    assert connection['authorization_url'] == (
        'https://hobbycoders.com/browser/claim?code=' + connection['code'])
    assert len(connection['code']) == 12 and connection['qr_svg']
    assert hub_call.call_count == 1 and hub_call.call_args.args[0] == 'start'
    assert hub_call.call_args.args[1]['name'] == 'Unraid'
    assert mobile_hub.get_binding() is None
    assert local.get('/owner').status_code == 200


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
    ('local', '192.168.1.50', 'http://attacker.example:8000', {}),
    ('local', '100.101.102.103', 'http://tower.ts.net.attacker.example:8000', {}),
    ('local', '8.8.8.8', 'http://100.88.77.66:8000', {}),
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


@pytest.mark.parametrize('origin', ['http://100.88.77.66:8000', 'http://tower.tail1234.ts.net:8000'])
def test_tailscale_peer_is_a_local_owner(app, origin):
    """Shuffle is reached over the owner's own networks only; a tailnet is one of them."""
    c = client(app, 'local', '100.101.102.103', origin)
    status = c.get('/api/v1/auth/status').json()
    assert status['authenticated'] and status['is_admin'] and status['local_access']
    assert c.post('/owner').status_code == 200
    with c.websocket_connect(origin.replace('http', 'ws') + '/ws', headers={'Origin': origin}) as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'admin'}


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


def test_passwords_legacy_sessions_and_login_endpoints_are_gone(app):
    database.create_admin('old-owner', 'not-a-real-hash')
    database.create_auth_session('legacy-token', owner_auth.now() + timedelta(days=1))
    c = client(app, 'remote', '127.0.0.1', 'https://worker.example.com')
    c.cookies.set('session', 'legacy-token')
    assert c.get('/owner').status_code == 403
    # Shuffle never asks its owner to sign in: no password, account or recovery login route exists.
    for path in ('login', 'change-password', 'account/start', 'account/poll', 'recovery'):
        assert c.post('/api/v1/auth/' + path, json={}).status_code == 404
    assert database.get_auth_session('legacy-token') is None
    assert auth.auth_service.login('old-owner', 'anything') is None


def test_preference_identity_preserves_legacy_history_and_is_stable(app):
    database.create_admin('original-user', 'historical-hash')
    assert owner_auth.preference_identity() == 'original-user'
    assert not owner_auth.setup_required()
    with database.get_db() as conn:
        conn.cursor().execute("UPDATE admin SET username='changed-display'")
    assert owner_auth.preference_identity() == 'original-user'


def test_invalid_query_token_cannot_bypass_cookie_origin(app, monkeypatch):
    c = paired_browser(app, monkeypatch)
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
    c = paired_browser(app, monkeypatch)
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
    c = paired_browser(app, monkeypatch)
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
    c = paired_browser(app, monkeypatch)
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


def test_existing_schema_upgrade_creates_owner_table_and_drops_login_stores(test_db):
    test_db.execute('DROP TABLE owner_local_sessions')
    for table in ('owner_browser_requests', 'owner_console_grants'):
        test_db.execute('CREATE TABLE ' + table + ' (token_hash TEXT PRIMARY KEY)')
    database.set_system_setting(database.MIGRATIONS_FINGERPRINT_KEY, 'previous-build')
    database.init_database()
    assert test_db.execute('SELECT COUNT(*) FROM owner_local_sessions').fetchone()[0] == 0
    for table in ('owner_browser_requests', 'owner_console_grants'):
        with pytest.raises(sqlite3.OperationalError):
            test_db.execute('SELECT COUNT(*) FROM ' + table)
