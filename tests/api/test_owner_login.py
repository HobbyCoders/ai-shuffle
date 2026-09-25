"""Owner password, trusted-LAN toggle and the desktop listener token.

Covers the access matrix of the local listener: loopback, a LAN browser with
"trusted LAN, no login" off (the default) and on, a password session as a
cookie or a Bearer, paired devices, wrong passwords and the login rate limit,
the desktop listener token present and absent, and WebSockets.
"""
import io
import secrets
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth, owner_login, ws_auth
from app.core import desktop_listener, encryption, owner_auth, pairing
from app.core.config import settings
from app.db import database
from app.db.settings import invalidate_system_setting_memo
from app.middleware.desktop_listener import DesktopListenerGate

PASSWORD = 'correct horse battery staple'
LAN = ('192.168.1.50', 'http://192.168.1.20:8000')
LOOPBACK = ('127.0.0.1', 'http://127.0.0.1:8000')
LISTENER = 'a' * 64


class Surface:
    """Test-only trusted listener boundary. HTTP headers cannot set this mark."""
    def __init__(self, app, ingress):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        if self.ingress is not None:
            scope['shuffle.ingress'] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def app(monkeypatch, mock_db):
    monkeypatch.setattr(settings, 'instance_id', 'owner-login-test')
    monkeypatch.setattr(settings, 'cookie_secure', False)
    monkeypatch.setattr(settings, 'local_mode', False)
    monkeypatch.setattr(owner_auth.settings, 'shuffle_local_access', True)
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    # Fast argon2id for tests; verification reads the parameters from each hash.
    monkeypatch.setattr(owner_auth, '_ARGON2_MEMORY_KIB', 1024)
    monkeypatch.setattr(owner_auth, '_ARGON2_ITERATIONS', 1)
    monkeypatch.setattr(owner_auth, '_ARGON2_LANES', 1)
    monkeypatch.setattr(auth, 'is_github_authenticated_cached', lambda *args: False)
    owner_login.limiter.reset()
    invalidate_system_setting_memo()
    desktop_listener.configure(None)

    result = FastAPI()
    result.include_router(auth.router)
    result.include_router(owner_login.router)

    @result.get('/health')
    def health():
        return {'status': 'healthy'}

    @result.api_route('/owner', methods=['GET', 'POST'])
    def admin(request: Request, identity=Depends(auth.require_admin)):
        return {'admin': request.state.is_admin}

    @result.get('/user')
    def user(request: Request, identity=Depends(auth.require_auth)):
        return {'principal': identity, 'admin': request.state.is_admin}

    @result.get('/page')
    def page():
        return {'page': True}

    @result.websocket('/ws')
    async def socket(websocket: WebSocket):
        valid, _ = await ws_auth.authenticate_websocket(websocket, websocket.query_params.get('token'))
        await websocket.accept()
        principal = getattr(websocket.state, 'mobile_tool_principal', None)
        await websocket.send_json({'authenticated': valid, 'kind': principal['kind'] if principal else None})
        await websocket.close()

    result.add_middleware(DesktopListenerGate)
    yield result
    desktop_listener.configure(None)
    invalidate_system_setting_memo()


def client(app, where=LAN, ingress='local', origin=True, headers=None):
    peer, base = where
    default = {'Origin': base} if origin else {}
    default.update(headers or {})
    return TestClient(Surface(app, ingress), base_url=base, client=(peer, 43210), headers=default)


def ws_url(base):
    return base.replace('http', 'ws') + '/ws'


def ws_result(c, base, **kwargs):
    with c.websocket_connect(ws_url(base), **kwargs) as ws:
        return ws.receive_json()


# ── Loopback and the LAN ─────────────────────────────────────────────────────

def test_loopback_is_the_owner_without_any_password(app):
    c = client(app, LOOPBACK)
    assert c.get('/owner').status_code == 200
    status = c.get('/api/v1/auth/status').json()
    assert status['authenticated'] and status['local_access']
    assert ws_result(c, LOOPBACK[1], headers={'Origin': LOOPBACK[1]}) == {'authenticated': True, 'kind': 'admin'}


def test_lan_browser_is_not_the_owner_by_default(app):
    c = client(app)
    assert c.get('/user').status_code == 401
    assert c.get('/owner').status_code == 403
    status = c.get('/api/v1/auth/status').json()
    assert not status['authenticated'] and not status['local_access']
    assert ws_result(c, LAN[1], headers={'Origin': LAN[1]}) == {'authenticated': False, 'kind': None}


def test_trusted_lan_toggle_restores_passwordless_lan(app):
    owner, lan = client(app, LOOPBACK), client(app)
    # Only the owner can change it.
    assert lan.put('/api/v1/owner/trusted-lan', json={'enabled': True}).status_code == 403
    assert owner.put('/api/v1/owner/trusted-lan', json={'enabled': True}).json() == {'trusted_lan': True}
    assert lan.get('/owner').status_code == 200
    assert ws_result(lan, LAN[1], headers={'Origin': LAN[1]}) == {'authenticated': True, 'kind': 'admin'}
    status = lan.get('/api/v1/owner/status').json()
    assert status['trusted_lan'] and not status['required']
    assert owner.put('/api/v1/owner/trusted-lan', json={'enabled': False}).status_code == 200
    assert lan.get('/owner').status_code == 403
    # It is bound to this worker and never synced to other workers.
    from app.network.policy import is_shared_setting_key
    assert not is_shared_setting_key(owner_auth.TRUSTED_LAN_KEY)
    assert not is_shared_setting_key(owner_auth.PASSWORD_KEY)


def test_loopback_mutations_need_an_origin_or_a_session_token(app):
    bare = client(app, LOOPBACK, origin=False)
    assert bare.get('/owner').status_code == 200
    assert bare.post('/owner').status_code == 403
    assert client(app, LOOPBACK).post('/owner').status_code == 200
    owner_auth.set_password(PASSWORD, source='test')
    token = client(app, LOOPBACK).post('/api/v1/owner/login', json={'password': PASSWORD, 'bearer': True}).json()['token']
    assert bare.post('/owner', headers={'Authorization': 'Bearer ' + token}).status_code == 200


# ── First password ───────────────────────────────────────────────────────────

def test_upgrade_without_password_keeps_loopback_and_asks_lan_to_create_one(app):
    """An upgraded install: configured, no password, LAN trust off."""
    database.set_system_setting('installation_configured', 'true')
    lan = client(app)
    status = lan.get('/api/v1/owner/status').json()
    assert status == {**status, 'password_set': False, 'setup_allowed': True, 'required': True,
                      'trusted_lan': False, 'login_available': False}
    assert not lan.get('/api/v1/auth/status').json()['authenticated']
    assert client(app, LOOPBACK).get('/owner').status_code == 200
    # The LAN browser can create the password and nothing else.
    assert lan.post('/owner').status_code == 403
    response = lan.post('/api/v1/owner/password', json={'password': PASSWORD})
    assert response.status_code == 200 and 'session=' in response.headers['set-cookie']
    assert lan.get('/owner').status_code == 200
    assert lan.post('/owner').status_code == 200
    assert lan.get('/api/v1/auth/status').json()['is_admin']
    after = client(app).get('/api/v1/owner/status').json()
    assert after['password_set'] and not after['setup_allowed'] and not after['required'] and after['login_available']


def test_first_password_rules(app):
    lan = client(app)
    assert lan.post('/api/v1/owner/password', json={'password': 'short'}).status_code == 400
    assert not owner_auth.password_set(exact=True)
    # Not from the remote listener, not cross-site, not from a public address.
    remote = client(app, ('127.0.0.1', 'https://worker.example.com'), ingress='remote')
    assert remote.post('/api/v1/owner/password', json={'password': PASSWORD}).status_code == 403
    assert client(app, ('8.8.8.8', LAN[1])).post('/api/v1/owner/password', json={'password': PASSWORD}).status_code == 403
    evil = client(app, headers={'Origin': 'https://evil.example'})
    assert evil.post('/api/v1/owner/password', json={'password': PASSWORD}).status_code == 403
    assert lan.post('/api/v1/owner/password', json={'password': PASSWORD}).status_code == 200
    # Once set, a second browser cannot "create" it again.
    other = client(app, ('192.168.1.77', LAN[1]))
    assert other.post('/api/v1/owner/password', json={'password': 'another long password'}).status_code == 403
    assert owner_auth.verify_password(PASSWORD)
    stored = database.get_system_setting(owner_auth.PASSWORD_KEY)
    assert stored.startswith('$argon2id$v=19$') and PASSWORD not in stored


def test_desktop_installs_do_not_require_a_password(app, monkeypatch):
    monkeypatch.setattr(settings, 'local_mode', True)
    assert not client(app, LOOPBACK).get('/api/v1/owner/status').json()['required']


# ── Sign-in ──────────────────────────────────────────────────────────────────

def test_wrong_password_is_refused_and_right_one_signs_in(app):
    owner_auth.set_password(PASSWORD, source='test')
    lan = client(app)
    assert lan.post('/api/v1/owner/login', json={'password': 'wrong password!!'}).status_code == 401
    assert lan.get('/user').status_code == 401
    response = lan.post('/api/v1/owner/login', json={'password': PASSWORD})
    assert response.status_code == 200 and 'token' not in response.json()
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
    assert lan.get('/owner').json() == {'admin': True}
    status = lan.get('/api/v1/auth/status').json()
    assert status['authenticated'] and status['is_admin'] and not status['local_access']
    # The cookie is ambient: mutations still need a same-origin page.
    assert lan.post('/owner', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert ws_result(lan, LAN[1], headers={'Origin': LAN[1]}) == {'authenticated': True, 'kind': 'admin'}
    # Signing out ends the session.
    assert lan.post('/api/v1/auth/logout').status_code == 200
    assert lan.get('/owner').status_code == 403


def test_bearer_session_works_for_http_and_websocket(app):
    owner_auth.set_password(PASSWORD, source='test')
    token = client(app).post('/api/v1/owner/login', json={'password': PASSWORD, 'bearer': True}).json()['token']
    assert token.startswith(owner_auth.SESSION_PREFIX)
    script = client(app, origin=False, headers={'Authorization': 'Bearer ' + token})
    assert script.get('/owner').status_code == 200
    assert script.post('/owner').status_code == 200
    assert script.get('/api/v1/auth/status').json()['is_admin']
    plain = client(app)
    assert ws_result(plain, LAN[1], headers={'Origin': LAN[1]})['authenticated'] is False
    with plain.websocket_connect(ws_url(LAN[1]) + '?token=' + token) as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'admin'}
    # Owner sessions never count on the protected remote listener.
    remote = client(app, ('127.0.0.1', 'https://worker.example.com'), ingress='remote', origin=False,
                    headers={'Authorization': 'Bearer ' + token})
    assert remote.get('/owner').status_code == 403
    assert remote.post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 403



def test_logout_revokes_the_owner_token_server_side(app):
    owner_auth.set_password(PASSWORD, source='test')
    # A Bearer session: a script signs out without an Origin, and the token is dead.
    token = client(app).post('/api/v1/owner/login', json={'password': PASSWORD, 'bearer': True}).json()['token']
    script = client(app, origin=False, headers={'Authorization': 'Bearer ' + token})
    assert script.get('/owner').status_code == 200
    assert script.post('/api/v1/auth/logout').status_code == 200
    assert script.get('/owner').status_code == 403
    assert script.get('/user').status_code == 401
    assert owner_auth.local_session(token) is None
    # A cookie session: the same token replayed as a Bearer after sign-out is refused.
    lan = client(app)
    assert lan.post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 200
    cookie = lan.cookies.get('session')
    assert cookie and cookie.startswith(owner_auth.SESSION_PREFIX)
    assert lan.post('/api/v1/auth/logout').status_code == 200
    replay = client(app, origin=False, headers={'Authorization': 'Bearer ' + cookie})
    assert replay.get('/owner').status_code == 403
    assert owner_auth.local_session(cookie) is None
    # The cookie path still needs a same-origin page.
    assert client(app, origin=False).post('/api/v1/auth/logout', cookies={'session': 'x'}).status_code == 403

def test_login_before_any_password_is_a_conflict(app):
    assert client(app).post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 409


def test_login_rate_limit(app):
    owner_auth.set_password(PASSWORD, source='test')
    lan = client(app)
    for _ in range(owner_login.limiter.per_client):
        assert lan.post('/api/v1/owner/login', json={'password': 'not the password'}).status_code == 401
    blocked = lan.post('/api/v1/owner/login', json={'password': PASSWORD})
    assert blocked.status_code == 429 and int(blocked.headers['Retry-After']) > 0
    # Another address is not held back by this one.
    assert client(app, ('192.168.1.77', LAN[1])).post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 200


def test_login_rate_limit_counts_all_addresses_together(app, monkeypatch):
    monkeypatch.setattr(owner_login, 'limiter', owner_login.FailureLimiter(per_client=100, total=3))
    owner_auth.set_password(PASSWORD, source='test')
    for i in range(3):
        c = client(app, (f'192.168.1.{60 + i}', LAN[1]))
        assert c.post('/api/v1/owner/login', json={'password': 'not the password'}).status_code == 401
    assert client(app, ('192.168.1.99', LAN[1])).post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 429


def test_change_password_needs_the_current_one_and_signs_everyone_out(app):
    owner_auth.set_password(PASSWORD, source='test')
    first, second = client(app), client(app, ('192.168.1.77', LAN[1]))
    for c in (first, second):
        assert c.post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 200
    new = 'a different long password'
    assert first.post('/api/v1/owner/password', json={'password': new}).status_code == 400
    assert first.post('/api/v1/owner/password', json={'password': new, 'current_password': 'wrong one'}).status_code == 401
    assert first.post('/api/v1/owner/password', json={'password': new, 'current_password': PASSWORD}).status_code == 200
    assert first.get('/owner').status_code == 200  # this browser got a fresh session
    assert second.get('/owner').status_code == 403  # every other browser was signed out
    assert owner_auth.verify_password(new) and not owner_auth.verify_password(PASSWORD)


# ── Paired devices ───────────────────────────────────────────────────────────

def _paired_controller():
    database.create_pairing_grant(token_hash='proof', purpose='controller', scopes=['controller'],
        target_install_id=None, endpoint_url=None, device_label=None, created_by='local',
        expires_at=owner_auth.now() + timedelta(minutes=2))
    token = 'shc_' + secrets.token_hex(16)
    database.exchange_grant_for_client(token_hash='proof', device_id='phone', device_name='Phone',
        platform='android', client_type='browser', session_token_hash=pairing.hash_controller_session(token),
        session_expires_at=owner_auth.now() + timedelta(days=1))
    return token


def test_paired_devices_keep_working_without_a_password(app):
    token = _paired_controller()
    phone = client(app, origin=False, headers={'Authorization': 'Bearer ' + token})
    assert phone.get('/user').json()['principal'].startswith('paired_client:')
    with client(app).websocket_connect(ws_url(LAN[1]) + '?token=' + token) as ws:
        assert ws.receive_json() == {'authenticated': True, 'kind': 'paired'}
    # Setting a password does not touch them.
    owner_auth.set_password(PASSWORD, source='test')
    assert phone.get('/user').status_code == 200


# ── OWNER_PASSWORD and the reset command ─────────────────────────────────────

def test_owner_password_env_seeds_once_and_leaves_the_environment(app):
    env = {'OWNER_PASSWORD': PASSWORD}
    assert owner_auth.seed_password_from_env(env) == 'seeded' and 'OWNER_PASSWORD' not in env
    assert owner_auth.verify_password(PASSWORD)
    # Changed in Settings afterwards: the variable never wins again.
    owner_auth.set_password('changed in settings!', source='settings')
    assert owner_auth.seed_password_from_env({'OWNER_PASSWORD': PASSWORD}) == 'ignored'
    assert owner_auth.verify_password('changed in settings!')
    # Not even after a reset.
    owner_auth.clear_password()
    assert owner_auth.seed_password_from_env({'OWNER_PASSWORD': PASSWORD}) == 'ignored'
    assert not owner_auth.password_set(exact=True)


def test_owner_password_env_rules(app):
    assert owner_auth.seed_password_from_env({}) == 'absent'
    env = {'OWNER_PASSWORD': 'short'}
    assert owner_auth.seed_password_from_env(env) == 'invalid' and env == {}
    assert not owner_auth.password_set(exact=True)
    # An existing password (set in the wizard) is kept.
    owner_auth.set_password(PASSWORD, source='setup')
    assert owner_auth.seed_password_from_env({'OWNER_PASSWORD': 'some other password'}) == 'ignored'
    assert owner_auth.verify_password(PASSWORD)


def test_reset_command(app, monkeypatch, capsys):
    from app import owner_password
    monkeypatch.setattr(owner_password.base, 'init_pool', lambda **kwargs: None)
    monkeypatch.setattr(owner_password.base, 'close_pool', lambda: None)
    monkeypatch.setattr(owner_password.database, 'init_database', lambda: None)
    owner_auth.set_password(PASSWORD, source='test')
    lan = client(app)
    assert lan.post('/api/v1/owner/login', json={'password': PASSWORD}).status_code == 200
    monkeypatch.setattr('sys.stdin', io.StringIO('a brand new password\n'))
    assert owner_password.main(['set', '--stdin']) == 0
    assert owner_auth.verify_password('a brand new password')
    assert lan.get('/owner').status_code == 403
    assert owner_password.main(['reset']) == 0
    assert not owner_auth.password_set(exact=True)
    assert owner_password.main(['status']) == 0
    assert 'Owner password: not set' in capsys.readouterr().out
    monkeypatch.setattr('sys.stdin', io.StringIO('short\n'))
    assert owner_password.main(['set', '--stdin']) == 2
    assert owner_password.main(['trusted-lan', 'on']) == 0 and owner_auth.trusted_lan()


# ── Desktop listener token ───────────────────────────────────────────────────

def test_listener_token_is_taken_out_of_the_environment():
    env = {'SHUFFLE_DESKTOP_LISTENER_TOKEN': LISTENER, 'PATH': '/bin'}
    try:
        desktop_listener.load(env)
        assert env == {'PATH': '/bin'}
        assert desktop_listener.configured() and desktop_listener.matches(LISTENER)
        assert not desktop_listener.matches('b' * 64) and not desktop_listener.matches(None)
        desktop_listener.load({'SHUFFLE_DESKTOP_LISTENER_TOKEN': 'not-hex'})
        assert desktop_listener.configured() and not desktop_listener.matches('not-hex')
    finally:
        desktop_listener.configure(None)
    from fluid.tools.child_env import is_host_secret
    assert is_host_secret('SHUFFLE_DESKTOP_LISTENER_TOKEN') and is_host_secret('OWNER_PASSWORD')


def test_listener_token_absent_leaves_loopback_as_before(app):
    assert client(app, LOOPBACK).get('/page').status_code == 200
    assert client(app, LOOPBACK).get('/owner').status_code == 200


def test_listener_token_required_on_every_local_request_but_health(app):
    desktop_listener.configure(LISTENER)
    stranger = client(app, LOOPBACK)
    for path in ('/page', '/owner', '/user', '/api/v1/auth/status', '/api/v1/owner/status'):
        response = stranger.get(path)
        assert response.status_code == 403 and response.json() == {'detail': 'Desktop listener token required'}, path
    assert stranger.post('/api/v1/owner/handoff').status_code == 403
    assert stranger.get('/health').status_code == 200
    with pytest.raises(WebSocketDisconnect):
        ws_result(stranger, LOOPBACK[1], headers={'Origin': LOOPBACK[1]})
    desktop = client(app, LOOPBACK, headers={desktop_listener.HEADER: LISTENER})
    assert desktop.get('/page').status_code == 200
    assert desktop.get('/owner').json() == {'admin': True}
    assert ws_result(desktop, LOOPBACK[1], headers={'Origin': LOOPBACK[1], desktop_listener.HEADER: LISTENER}) == {
        'authenticated': True, 'kind': 'admin'}
    # The desktop's own process sends no Origin; the token stands in for it.
    main_process = client(app, LOOPBACK, origin=False, headers={desktop_listener.HEADER: LISTENER})
    assert main_process.post('/owner').status_code == 200
    wrong = client(app, LOOPBACK, headers={desktop_listener.HEADER: 'b' * 64})
    assert wrong.get('/owner').status_code == 403
    # The protected remote listener is not gated by it (devices authenticate there).
    remote = client(app, ('127.0.0.1', 'https://worker.example.com'), ingress='remote')
    assert remote.get('/page').status_code == 200 and remote.get('/owner').status_code == 403


def test_listener_gate_admits_standalone_credentials_only(app):
    desktop_listener.configure(LISTENER)
    token = _paired_controller()
    assert client(app, LOOPBACK, headers={'Authorization': 'Bearer ' + token}).get('/user').status_code == 200
    assert client(app, LOOPBACK, headers={'Authorization': 'Bearer made-up'}).get('/page').status_code == 403
    forged = client(app, LOOPBACK)
    forged.cookies.set('session', 'shr_made-up')
    assert forged.get('/page').status_code == 403


def test_open_in_browser_handoff(app):
    desktop_listener.configure(LISTENER)
    desktop = client(app, LOOPBACK, origin=False, headers={desktop_listener.HEADER: LISTENER})
    path = desktop.post('/api/v1/owner/handoff').json()['path']
    browser = client(app, LOOPBACK)
    response = browser.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/'
    # The default browser has no listener token, only the session it was handed.
    assert browser.get('/page').status_code == 200
    assert browser.get('/owner').json() == {'admin': True}
    assert browser.post('/owner').status_code == 200
    # One use only, and only on this computer.
    assert client(app, LOOPBACK).get(path, follow_redirects=False).status_code == 403
    second = desktop.post('/api/v1/owner/handoff').json()['path']
    assert client(app, LAN).get(second, follow_redirects=False).status_code == 403
    # Without a listener token (Docker) there is no handoff at all.
    desktop_listener.configure(None)
    assert client(app, LOOPBACK).post('/api/v1/owner/handoff').status_code == 404
