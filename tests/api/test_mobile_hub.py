import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from fastapi import FastAPI, HTTPException, WebSocketException
from fastapi.testclient import TestClient
from app.api import mobile, mobile_hub
from app.api.auth import get_paired_client_from_token, hash_api_key
from app.core import account_hub, encryption, pairing
from app.core.models import DeviceInfo
from app.db import mobile_hub as store, database

HUB = 'https://hub.example.com'
SERVER = '11111111-1111-1111-1111-111111111111'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
DEVICE = '33333333-3333-3333-3333-333333333333'
SECRET = 'a' * 64


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(account_hub.settings, 'mobile_account_hub_url', HUB)
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    mobile_hub._limiter.reset()
    app = FastAPI()
    app.include_router(mobile.router)
    # Deliberately no full application lifespan / pool / production services.
    return TestClient(app)


def bind():
    store.bind(HUB, SERVER, encryption.encrypt_value(SECRET), 'controller')
    store.activate()


def identity():
    return {'account_id': ACCOUNT, 'device_id': DEVICE, 'device_name': 'Phone', 'server_id': SERVER}


def info(device_id=DEVICE):
    return DeviceInfo(device_id=device_id, device_name='Untrusted label', platform='android', capabilities={})


def insert(key='aim_test'):
    return store.enroll(identity(), info(), hash_api_key(key), datetime.now(timezone.utc) + timedelta(days=1), list(pairing.DEFAULT_MOBILE_SCOPES))


def active(*args, **kwargs):
    return {'active': True, 'account_id': ACCOUNT, 'device_id': DEVICE}


@pytest.mark.parametrize('url', ['http://hub.example.com', 'https://user@hub.example.com',
    'https://hub.example.com?x=1', 'https://hub.example.com#x', 'https://hub.example.com/path',
    'https://127.0.0.1', 'https://169.254.169.254', 'https://[::1]', 'https://10.0.0.1',
    'https://localhost', 'https://host.local', 'https://hub.example.com:444'])
def test_unsafe_origin_rejected(setup, monkeypatch, url):
    monkeypatch.setattr(account_hub.settings, 'mobile_account_hub_url', url)
    with pytest.raises(ValueError):
        account_hub.trusted_origin(url)


def test_mobile_cannot_choose_trust_root(setup):
    with pytest.raises(ValueError):
        account_hub.trusted_origin('https://attacker.example.com')


def test_dns_private_or_mixed_rejected_before_connect(setup, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))])
    connect = Mock()
    monkeypatch.setattr(socket, 'create_connection', connect)
    with pytest.raises(ValueError):
        account_hub._PinnedHTTPS('hub.example.com').connect()
    connect.assert_not_called()


def test_dns_pinned_and_tls_hostname_preserved(setup, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    raw = Mock()
    connect = Mock(return_value=raw)
    monkeypatch.setattr(socket, 'create_connection', connect)
    conn = account_hub._PinnedHTTPS('hub.example.com', timeout=4)
    context = Mock()
    conn._context = context
    conn.connect()
    connect.assert_called_once_with(('8.8.8.8', 443), 4)
    context.wrap_socket.assert_called_once_with(raw, server_hostname='hub.example.com')


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 500])
def test_outbound_fail_closed_no_redirect(setup, monkeypatch, status):
    bind()
    conn = Mock()
    conn.getresponse.return_value.status = status
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', Mock(return_value=conn))
    with pytest.raises(HTTPException) as exc:
        account_hub.call(store.get_binding(), 'POST', 'exchange', {'ticket': 'secret-ticket'})
    assert exc.value.status_code == 503
    assert 'secret' not in exc.value.detail
    assert conn.request.call_count == 1
    conn.close.assert_called_once()


def test_link_requires_real_mobile_controller(setup):
    response = setup.post('/api/v1/mobile/hub/link', json={'hub_url': HUB, 'server_id': SERVER, 'server_secret': SECRET})
    assert response.status_code == 401
    assert 'error' in response.json()


def test_native_enrollment_passes_csrf_but_admin_connection_does_not(setup, monkeypatch):
    from app.middleware.csrf import CSRFProtectionMiddleware
    from app.core.config import settings
    monkeypatch.setattr(settings, 'cors_origins', 'https://worker.example.com')
    app = FastAPI()
    app.include_router(mobile.router)
    app.add_middleware(CSRFProtectionMiddleware)
    client = TestClient(app, client=('203.0.113.20', 50123))
    bind()
    monkeypatch.setattr(account_hub, 'call', lambda b, m, p, body=None: identity() if p == 'exchange' else active())
    response = client.post('/api/v1/mobile/hub/enroll', json={'ticket': 'one-use', 'device_info': info().model_dump()})
    assert response.status_code == 200, response.text
    # The new administrator action retains CSRF protection.
    assert client.post('/api/v1/mobile/hub/connection', json={'name': 'Worker'}).status_code == 403


def test_link_encrypted_immutable_and_retryable(setup, monkeypatch, test_db):
    controller = {'device_id': 'local', 'paired_client_id': 'controller'}
    monkeypatch.setattr(mobile, 'get_device_with_scope', lambda *a: controller)
    calls = Mock(return_value={'ok': True})
    monkeypatch.setattr(account_hub, 'call', calls)
    body = {'hub_url': HUB, 'server_id': SERVER, 'server_secret': SECRET}
    assert setup.post('/api/v1/mobile/hub/link', json=body).status_code == 200
    binding = store.get_binding()
    assert binding['encrypted_secret'] != SECRET
    assert encryption.decrypt_value(binding['encrypted_secret']) == SECRET
    assert setup.post('/api/v1/mobile/hub/link', json=body).status_code == 200
    assert setup.post('/api/v1/mobile/hub/link', json={**body, 'server_id': ACCOUNT}).status_code == 409
    controller['paired_client_id'] = 'other-controller'
    assert setup.post('/api/v1/mobile/hub/link', json=body).status_code == 409
    assert store.get_binding() == binding


def test_enroll_fresh_key_hash_and_duplicate_no_overwrite(setup, monkeypatch, test_db):
    bind()
    monkeypatch.setattr(account_hub, 'call', lambda b, m, p, body=None: identity() if p == 'exchange' else active())
    body = {'ticket': 'single-use-ticket', 'device_info': info().model_dump()}
    response = setup.post('/api/v1/mobile/hub/enroll', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['api_key'].startswith('aim_')
    row = database.get_mobile_device(store.local_device_id(SERVER, DEVICE))
    assert row['api_key_hash'] == hash_api_key(result['api_key'])
    assert row['device_name'] == 'Phone'
    assert store.ownership(row["device_id"])['account_id'] == ACCOUNT
    assert setup.post('/api/v1/mobile/hub/enroll', json=body).status_code == 409
    assert database.get_mobile_device(store.local_device_id(SERVER, DEVICE))['api_key_hash'] == row['api_key_hash']


def test_enroll_identity_mismatch_consumed_but_not_saved(setup, monkeypatch):
    bind()
    call = Mock(return_value=identity())
    monkeypatch.setattr(account_hub, 'call', call)
    response = setup.post('/api/v1/mobile/hub/enroll', json={'ticket': 'ticket', 'device_info': info(ACCOUNT).model_dump()})
    assert response.status_code == 403
    assert database.get_mobile_device(DEVICE) is None
    assert call.call_count == 1


def test_existing_different_authorization_never_overwritten(setup, test_db):
    database.create_mobile_device(DEVICE, 'Old owner', 'android', 'old-hash', {})
    with pytest.raises(ValueError):
        insert()
    assert database.get_mobile_device(DEVICE)['api_key_hash'] == 'old-hash'
    assert store.ownership(DEVICE) is None
    assert test_db.execute('SELECT count(*) FROM paired_clients').fetchone()[0] == 0


@pytest.mark.parametrize('hub_status', [401, 503])
def test_hub_revocation_rechecked_on_every_aim_and_web_request(setup, monkeypatch, hub_status):
    bind()
    insert()
    call = Mock(side_effect=[active(), active(), HTTPException(hub_status, 'authorization failed'), HTTPException(hub_status, 'authorization failed')])
    monkeypatch.setattr(account_hub, 'call', call)
    request = Mock()
    request.headers = {'Authorization': 'Bearer aim_test'}
    request.cookies = {}
    assert mobile.get_device_from_api_key(request)['device_id'] == DEVICE
    owner = store.ownership(DEVICE)
    token = pairing.generate_controller_session()
    database.set_paired_client_session(owner['paired_client_id'], pairing.hash_controller_session(token), datetime.now(timezone.utc) + timedelta(days=1))
    assert get_paired_client_from_token(token)
    with pytest.raises(HTTPException) as failure:
        mobile.get_device_from_api_key(request)
    assert failure.value.status_code == hub_status
    if hub_status == 503:
        # An unavailable hub must not turn a valid session into a revoked login.
        with pytest.raises(HTTPException) as failure:
            get_paired_client_from_token(token)
        assert failure.value.status_code == 503
    else:
        assert get_paired_client_from_token(token) is None
    assert call.call_count == 4
    if hub_status == 503:
        call.side_effect = None
        call.return_value = active()
        assert get_paired_client_from_token(token)


def test_deleted_local_mobile_revokes_derived_session(setup, monkeypatch):
    bind()
    insert()
    owner = store.ownership(DEVICE)
    token = pairing.generate_controller_session()
    database.set_paired_client_session(owner['paired_client_id'], pairing.hash_controller_session(token), datetime.now(timezone.utc) + timedelta(days=1))
    database.delete_mobile_device(DEVICE)
    assert get_paired_client_from_token(token) is None


@pytest.mark.parametrize('result', [{'active': False}, {'active': True, 'account_id': SERVER, 'device_id': DEVICE},
                                  {'active': True, 'account_id': ACCOUNT, 'device_id': ACCOUNT}])
def test_authoritative_owner_checked(setup, monkeypatch, result):
    bind()
    insert()
    monkeypatch.setattr(account_hub, 'call', lambda *a: result)
    with pytest.raises(HTTPException):
        account_hub.mobile_hub_ownership(database.get_mobile_device(DEVICE))


def test_controller_cannot_become_admin(setup, monkeypatch):
    from app.core.auth import auth_service
    lookup = Mock(return_value={'token': 'injected'})
    monkeypatch.setattr(database, 'get_auth_session', lookup)
    assert not auth_service.validate_session('shc_injected')
    assert not auth_service.validate_session('aim_injected')
    lookup.assert_not_called()


def test_enroll_requires_link_and_limited(setup):
    body = {'ticket': 'ticket', 'device_info': info().model_dump()}
    assert setup.post('/api/v1/mobile/hub/enroll', json=body).status_code == 403
    for _ in range(29):
        setup.post('/api/v1/mobile/hub/enroll', json=body)
    assert setup.post('/api/v1/mobile/hub/enroll', json=body).status_code == 429

def test_password_rotation_discovers_hub_secret(setup):
    bind()
    old_key = encryption._encryption_key
    new_key = encryption.Fernet.generate_key()
    plan, failures = encryption.build_reencryption_plan(old_key, new_key, database)
    assert not failures
    items = [item for item in plan if item.ref == store.secret_key()]
    assert len(items) == 1
    encryption.apply_reencryption_plan(items, database)
    encryption._encryption_key = new_key
    assert encryption.decrypt_value(store.get_binding()['encrypted_secret']) == SECRET


def test_two_installs_get_independent_keys_and_owner_bindings(setup, monkeypatch):
    bind()
    current = identity()
    def call(binding, method, suffix, body=None):
        if suffix == 'exchange':
            return dict(current)
        return {'active': True, 'account_id': current['account_id'], 'device_id': current['device_id']}
    monkeypatch.setattr(account_hub, 'call', call)
    first = setup.post('/api/v1/mobile/hub/enroll', json={'ticket': 'first', 'device_info': info().model_dump()}).json()
    current['device_id'] = '44444444-4444-4444-4444-444444444444'
    second = setup.post('/api/v1/mobile/hub/enroll', json={'ticket': 'second', 'device_info': info(current['device_id']).model_dump()}).json()
    assert first['api_key'] != second['api_key']
    assert first['device_id'] != second['device_id']
    assert database.get_mobile_device_by_api_key_hash(hash_api_key(first['api_key']))['device_id'] == store.local_device_id(SERVER, first['device_id'])
    assert database.get_mobile_device_by_api_key_hash(hash_api_key(second['api_key']))['device_id'] == store.local_device_id(SERVER, second['device_id'])
    assert store.ownership(store.local_device_id(SERVER, first['device_id']))['account_id'] == store.ownership(store.local_device_id(SERVER, second['device_id']))['account_id']


def test_account_key_cannot_list_all_devices_or_relink(setup, monkeypatch):
    bind()
    insert()
    monkeypatch.setattr(account_hub, 'call', active)
    headers = {'Authorization': 'Bearer aim_test'}
    assert setup.get('/api/v1/mobile/devices', headers=headers).status_code in (401, 403)
    response = setup.post('/api/v1/mobile/hub/link', headers=headers,
                          json={'hub_url': HUB, 'server_id': SERVER, 'server_secret': SECRET})
    assert response.status_code == 403


def test_direct_pairing_not_replaced_by_hub(setup):
    response = setup.post('/api/v1/mobile/register', json={'device_info': info().model_dump()})
    assert response.status_code == 403


def test_activation_outage_leaves_locked_inactive_binding(setup, monkeypatch):
    monkeypatch.setattr(mobile, 'get_device_with_scope', lambda *a: {'device_id': 'local', 'paired_client_id': 'controller'})
    monkeypatch.setattr(account_hub, 'call', Mock(side_effect=HTTPException(503, 'unavailable')))
    body = {'hub_url': HUB, 'server_id': SERVER, 'server_secret': SECRET}
    assert setup.post('/api/v1/mobile/hub/link', json=body).status_code == 503
    assert not store.get_binding()['active']
    assert setup.post('/api/v1/mobile/hub/enroll', json={'ticket': 'ticket', 'device_info': info().model_dump()}).status_code == 403
    assert setup.post('/api/v1/mobile/hub/link', json={**body, 'server_id': ACCOUNT}).status_code == 409


def test_schema_migration_idempotent(setup, test_db):
    from tests.pg_shim import ShimConnection
    # Use the same adapter as production module tests, never a live pool.
    with database.get_db() as conn:
        store.create_schema(conn.cursor())
        store.create_schema(conn.cursor())
    bind()
    assert store.get_binding()['server_id'] == SERVER


@pytest.mark.asyncio
@pytest.mark.parametrize('hub_status', [401, 503])
async def test_revoked_hub_websocket_handshake_denied(setup, monkeypatch, hub_status):
    from app.api.ws_auth import authenticate_websocket
    bind()
    insert()
    owner = store.ownership(DEVICE)
    token = pairing.generate_controller_session()
    database.set_paired_client_session(owner['paired_client_id'], pairing.hash_controller_session(token), datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr(account_hub, 'call', Mock(side_effect=HTTPException(hub_status, 'authorization failed')))
    websocket = Mock(cookies={})
    if hub_status == 503:
        with pytest.raises(WebSocketException) as failure:
            await authenticate_websocket(websocket, token)
        assert failure.value.code == 1013
    else:
        assert await authenticate_websocket(websocket, token) == (False, None)


def test_direct_phone_deletion_revokes_derived_cookie(setup):
    grant = database.create_pairing_grant(
        token_hash='direct-grant', purpose='controller',
        scopes=list(pairing.DEFAULT_MOBILE_SCOPES), target_install_id=None,
        endpoint_url=None, device_label=None, created_by='test',
        expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    result = database.exchange_grant_for_mobile(
        token_hash='direct-grant', device_id=DEVICE, device_name='Direct',
        platform='android', capabilities={}, api_key_hash='direct-key-hash',
        api_key_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    token = pairing.generate_controller_session()
    database.set_paired_client_session(result['client']['id'], pairing.hash_controller_session(token),
                                      datetime.now(timezone.utc) + timedelta(days=1))
    assert get_paired_client_from_token(token)
    database.delete_mobile_device(DEVICE)
    assert get_paired_client_from_token(token) is None


