"""Actual routes and isolated DB: replace one revoked key with local approval."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mobile, pairing as pairing_api
from app.api.auth import hash_api_key, require_admin
from app.core import account_hub, encryption, pairing
from app.core.config import settings
from app.core.models import DeviceInfo
from app.db import database, mobile_hub as hub_store

SERVER = '11111111-1111-1111-1111-111111111111'
OTHER_SERVER = '44444444-4444-4444-4444-444444444444'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
PHONE = '33333333-3333-3333-3333-333333333333'
INSTANCE = 'repair-test-worker'
HUB = 'https://hub.example'
CODE = 'ABCDEFGHIJKL'
OLD_KEY = 'aim_old-controlled-key'
OTHER_KEY = 'aim_other-controlled-key'


@pytest.fixture
def repair(monkeypatch):
    monkeypatch.setattr(settings, 'instance_id', INSTANCE)
    monkeypatch.setattr(settings, 'mobile_account_hub_url', HUB)
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    hub_store.bind(HUB, SERVER, encryption.encrypt_value('controlled-hub-secret'), 'managed:' + INSTANCE)
    hub_store.activate()
    monkeypatch.setattr(account_hub, 'call', lambda *a, **k: {
        'active': True, 'account_id': ACCOUNT, 'device_id': PHONE})
    info = DeviceInfo(device_id=PHONE, device_name='Controlled phone', platform='android', capabilities={})
    ids = []
    for server, key in ((SERVER, OLD_KEY), (OTHER_SERVER, OTHER_KEY)):
        ids.append(hub_store.enroll(
            {'server_id': server, 'account_id': ACCOUNT, 'device_id': PHONE, 'device_name': 'Controlled phone'},
            info, hash_api_key(key), datetime.now(timezone.utc) + timedelta(days=1),
            list(pairing.DEFAULT_MOBILE_SCOPES), scoped=True))
    owner = hub_store.ownership(ids[0])
    database.set_paired_client_session(owner['paired_client_id'], 'old-session-hash', datetime.now(timezone.utc) + timedelta(days=1))
    database.revoke_paired_client(owner['paired_client_id'])
    app = FastAPI()
    app.include_router(mobile.router)
    app.include_router(pairing_api.router)
    app.dependency_overrides[require_admin] = lambda: 'controlled-local-admin'
    client = TestClient(app)
    return client, ids[0], ids[1], owner


def grant(*, target=INSTANCE, purpose='controller', expires=1):
    return database.create_pairing_grant(
        token_hash=pairing.hash_pairing_code(CODE), purpose=purpose,
        scopes=list(pairing.DEFAULT_MOBILE_SCOPES), target_install_id=target,
        endpoint_url='https://worker.example', device_label='Controlled phone', created_by='fixture',
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires))


def payload(device, *, mode='scoped-v1'):
    value = {'pairing_code': CODE, 'device_info': {
        'device_id': device, 'device_name': 'Reconnected phone', 'platform': 'android', 'capabilities': {}}}
    if mode is not None:
        value['account_repair'] = mode
    return value


def unregister(repair):
    client, device, _, _ = repair
    assert client.delete('/api/v1/mobile/devices/' + device).status_code == 204


def unconsumed(test_db):
    row = test_db.execute('SELECT consumed_at, attempt_count FROM pairing_grants').fetchone()
    assert row['consumed_at'] is None
    return row['attempt_count']


def test_repair_issues_new_key_preserves_owner_and_other_worker(repair, test_db):
    client, device, other, owner = repair
    untouched = database.get_mobile_device(other)
    unregister(repair)
    grant()
    response = client.post('/api/v1/mobile/register', json=payload(device))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['device_id'] == device != PHONE
    assert body['api_key'].startswith('aim_') and body['api_key'] not in (OLD_KEY, OTHER_KEY)
    assert response.headers['cache-control'] == 'no-store'
    restored = database.get_mobile_device(device)
    assert restored['api_key_hash'] == hash_api_key(body['api_key'])
    assert restored['paired_client_id'] == owner['paired_client_id']
    assert restored['revoked_at'] is None
    assert hub_store.ownership(device) == owner
    assert hub_store.public_device_id(restored) == PHONE
    assert database.get_mobile_device(other) == untouched
    assert database.get_mobile_device_by_api_key_hash(hash_api_key(OLD_KEY)) is None
    assert database.get_paired_client_by_session_hash('old-session-hash') is None
    assert client.post('/api/v1/mobile/web-session', headers={'Authorization': 'Bearer ' + OLD_KEY}, json={'purpose': 'web'}).status_code == 401
    assert client.post('/api/v1/mobile/web-session', headers={'Authorization': 'Bearer ' + body['api_key']}, json={'purpose': 'web'}).status_code == 200
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code == 400
    assert test_db.execute('SELECT consumed_at FROM pairing_grants').fetchone()[0] is not None


def test_revoked_row_requires_explicit_admin_unregister_first(repair, test_db):
    client, device, _, owner = repair
    grant()
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code == 400
    assert database.get_mobile_device(device)['api_key_hash'] == hash_api_key(OLD_KEY)
    assert database.get_paired_client(owner['paired_client_id'])['revoked_at'] is not None
    assert unconsumed(test_db) == 0


@pytest.mark.parametrize('target', ['other-worker', None])
def test_other_worker_or_unbound_code_never_consumed_for_repair(repair, test_db, target):
    client, device, _, owner = repair
    unregister(repair)
    grant(target=target)
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code == 400
    assert unconsumed(test_db) == 0
    assert database.get_mobile_device(device) is None
    assert database.get_paired_client(owner['paired_client_id'])['revoked_at'] is not None


def test_generic_mobile_and_browser_cannot_bypass_managed_tombstone(repair, test_db):
    client, device, _, owner = repair
    unregister(repair)
    grant()
    assert client.post('/api/v1/mobile/register', json=payload(device, mode=None)).status_code == 409
    assert client.post('/api/v1/pairing/exchange', json={
        'code': CODE, 'device_id': device, 'device_name': 'Wrong client type', 'platform': 'browser', 'client_type': 'browser'}).status_code == 400
    assert unconsumed(test_db) == 0
    assert database.get_mobile_device(device) is None
    assert database.get_paired_client(owner['paired_client_id'])['revoked_at'] is not None


@pytest.mark.parametrize('mode', ['missing_owner', 'active_client', 'wrong_binding', 'inactive_binding', 'changed_account', 'hub_revoked'])
def test_changed_or_unapproved_authority_never_repaired(repair, test_db, monkeypatch, mode):
    client, device, _, owner = repair
    unregister(repair)
    grant()
    if mode == 'missing_owner':
        test_db.execute('DELETE FROM mobile_worker_authorizations WHERE device_id = ?', (device,))
    elif mode == 'active_client':
        test_db.execute('UPDATE paired_clients SET revoked_at = NULL WHERE id = ?', (owner['paired_client_id'],))
    elif mode == 'wrong_binding':
        test_db.execute('UPDATE mobile_worker_bindings SET server_id = ? WHERE instance_id = ?', (OTHER_SERVER, INSTANCE))
    elif mode == 'inactive_binding':
        test_db.execute('UPDATE mobile_worker_bindings SET active = FALSE WHERE instance_id = ?', (INSTANCE,))
    else:
        monkeypatch.setattr(account_hub, 'call', lambda *a, **k: {
            'active': mode != 'hub_revoked', 'account_id': OTHER_SERVER if mode == 'changed_account' else ACCOUNT, 'device_id': PHONE})
    test_db.commit()
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code in (401, 409)
    assert database.get_mobile_device(device) is None
    assert unconsumed(test_db) == 0


@pytest.mark.parametrize('purpose,expires', [('runner', 1), ('controller', -1)])
def test_wrong_purpose_or_expired_grant_cannot_restore_key(repair, test_db, purpose, expires):
    client, device, _, _ = repair
    unregister(repair)
    grant(purpose=purpose, expires=expires)
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code == 400
    assert database.get_mobile_device(device) is None
    unconsumed(test_db)


def test_explicit_repair_never_falls_back_to_fresh_direct_registration(repair, test_db):
    client, _, _, _ = repair
    grant()
    fresh = '55555555-5555-5555-5555-555555555555'
    assert client.post('/api/v1/mobile/register', json=payload(fresh)).status_code == 409
    assert database.get_mobile_device(fresh) is None
    assert unconsumed(test_db) == 0


@pytest.mark.parametrize('route', ['mobile', 'browser'])
def test_public_direct_exchange_also_rejects_another_worker_code(repair, test_db, route):
    client, _, _, _ = repair
    grant(target='other-worker')
    fresh = 'controlled-fresh-device'
    if route == 'mobile':
        response = client.post('/api/v1/mobile/register', json=payload(fresh, mode=None))
    else:
        response = client.post('/api/v1/pairing/exchange', json={
            'code': CODE, 'device_id': fresh, 'device_name': 'Controlled browser', 'platform': 'browser', 'client_type': 'browser'})
    assert response.status_code == 400
    assert unconsumed(test_db) == 0


def test_endpoint_advertises_versioned_repair_without_account_identity(repair):
    client, _, _, _ = repair
    result = client.get('/api/v1/mobile/hub/endpoint')
    assert result.status_code == 200
    assert result.json() == {'server_id': SERVER, 'ready': True, 'protected_ingress': False, 'account_repair': 'scoped-v1'}
    assert result.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('change', ['owner', 'binding'])
def test_authority_change_after_live_check_is_fenced_before_grant_consumption(repair, test_db, monkeypatch, change):
    client, device, _, owner = repair
    unregister(repair)
    grant()
    exchange = database.exchange_grant_for_mobile

    def changed_authority(**kwargs):
        if change == 'owner':
            test_db.execute('UPDATE mobile_worker_authorizations SET account_id = ? WHERE device_id = ?', (OTHER_SERVER, device))
        else:
            test_db.execute('UPDATE mobile_worker_bindings SET active = FALSE WHERE instance_id = ?', (INSTANCE,))
        test_db.commit()
        return exchange(**kwargs)

    monkeypatch.setattr(database, 'exchange_grant_for_mobile', changed_authority)
    assert client.post('/api/v1/mobile/register', json=payload(device)).status_code == 409
    assert unconsumed(test_db) == 0
    assert database.get_mobile_device(device) is None
    assert database.get_paired_client(owner['paired_client_id'])['revoked_at'] is not None


def test_failed_key_insert_rolls_back_grant_and_client_restore(repair, test_db):
    client, device, other, owner = repair
    unregister(repair)
    grant()
    untouched = database.get_mobile_device(other)
    test_db.execute("""CREATE TRIGGER reject_repair_insert BEFORE INSERT ON mobile_devices
        BEGIN SELECT RAISE(ABORT, 'controlled repair failure'); END""")
    test_db.commit()
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    assert failing_client.post('/api/v1/mobile/register', json=payload(device)).status_code == 500
    assert unconsumed(test_db) == 0
    assert database.get_mobile_device(device) is None
    assert database.get_paired_client(owner['paired_client_id'])['revoked_at'] is not None
    assert database.get_paired_client_by_session_hash('old-session-hash') is None
    assert database.get_mobile_device(other) == untouched


@pytest.mark.parametrize('purpose', ['web', 'native'])
@pytest.mark.parametrize('pause_at', ['ensure', 'persist'])
def test_old_key_request_cannot_mint_across_repair(repair, test_db, monkeypatch, purpose, pause_at):
    client, device, other, owner = repair
    # Start with a valid credential, then revoke/delete/repair after the request
    # has authenticated but before either of its awaited DB operations resumes.
    test_db.execute('UPDATE paired_clients SET revoked_at = NULL WHERE id = ?', (owner['paired_client_id'],))
    test_db.execute('UPDATE mobile_devices SET revoked_at = NULL WHERE device_id = ?', (device,))
    test_db.commit()
    untouched = database.get_mobile_device(other)
    name = 'ensure_paired_client_for_mobile' if pause_at == 'ensure' else 'set_paired_client_session'
    operation = getattr(database, name)
    fresh_keys = []

    def resume_after_repair(*args, **kwargs):
        unregister(repair)
        grant()
        result = client.post('/api/v1/mobile/register', json=payload(device))
        assert result.status_code == 200, result.text
        fresh_keys.append(result.json()['api_key'])
        return operation(*args, **kwargs)

    monkeypatch.setattr(database, name, resume_after_repair)
    response = client.post('/api/v1/mobile/web-session', headers={'Authorization': 'Bearer ' + OLD_KEY}, json={'purpose': purpose})
    assert response.status_code == 401
    assert 'set-cookie' not in response.headers
    assert database.get_paired_client(owner['paired_client_id'])['session_token_hash'] is None
    assert test_db.execute('SELECT * FROM paired_client_native_sessions').fetchall() == []
    monkeypatch.setattr(database, name, operation)
    assert client.post('/api/v1/mobile/web-session', headers={'Authorization': 'Bearer ' + fresh_keys[0]}, json={'purpose': purpose}).status_code == 200
    assert database.get_mobile_device(other) == untouched
