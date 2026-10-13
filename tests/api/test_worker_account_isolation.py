"""Account registration belongs to a worker, not to its shared LAN database."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import mobile
from app.api.auth import hash_api_key, require_admin, get_paired_client_from_token
from app.core import account_hub, encryption, managed_connection as managed, pairing
from app.core.config import settings
from app.core.models import DeviceInfo
from app.db import database, mobile_hub as store

HUB = 'https://hub.example.com'
PHONE = '33333333-3333-3333-3333-333333333333'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
FIRST = '11111111-1111-1111-1111-111111111111'
SECOND = '44444444-4444-4444-4444-444444444444'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'instance_id', 'first')
    monkeypatch.setattr(settings, 'mobile_account_hub_url', HUB)
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(managed.cloudflared, 'find_executable', lambda: 'cloudflared')
    monkeypatch.setattr(managed, '_process', None)
    monkeypatch.setattr(managed, '_generation', 0)
    monkeypatch.setattr(managed, '_state', 'disconnected')
    monkeypatch.setattr(managed, '_error', None)
    monkeypatch.setattr(managed, '_url', None)
    monkeypatch.setattr(account_hub, 'secret_for', lambda b: encryption.decrypt_value(b['encrypted_secret']))
    def hub(binding, method, suffix, body=None, **kwargs):
        if suffix == 'exchange':
            return {'account_id': ACCOUNT, 'device_id': PHONE, 'device_name': 'Phone'}
        if suffix == 'connection':
            return {'ok': True}
        return {'active': True, 'account_id': ACCOUNT, 'device_id': PHONE}
    monkeypatch.setattr(account_hub, 'call', Mock(side_effect=hub))
    from app.api import mobile_hub
    mobile_hub._limiter.reset()
    app = FastAPI()
    app.include_router(mobile.router)
    return TestClient(app)


def bind(server):
    store.bind(HUB, server, encryption.encrypt_value('a'*64), 'managed:' + settings.effective_instance_id)
    store.activate(server)


def enroll(client):
    result = client.post('/api/v1/mobile/hub/enroll', json={'ticket': 'one-use-ticket',
        'device_info': DeviceInfo(device_id=PHONE, device_name='Phone', platform='android', capabilities={}).model_dump()})
    assert result.status_code == 200, result.text
    return result.json()['api_key']


def session(key):
    device = database.get_mobile_device_by_api_key_hash(hash_api_key(key))
    token = pairing.generate_controller_session()
    database.set_paired_client_session(device['paired_client_id'], pairing.hash_controller_session(token),
                                      datetime.now(timezone.utc) + timedelta(days=1))
    return token


def request(key):
    return SimpleNamespace(headers={'Authorization': 'Bearer ' + key}, cookies={})


def test_same_phone_enrolls_both_workers_without_replacing_credentials(client, monkeypatch):
    bind(FIRST)
    first_key = enroll(client)
    first_session = session(first_key)
    monkeypatch.setattr(settings, 'instance_id', 'second')
    assert store.get_binding() is None
    bind(SECOND)
    second_key = enroll(client)
    second_session = session(second_key)
    assert first_key != second_key
    second_device = mobile.get_device_from_api_key(request(second_key))
    assert store.public_device_id(second_device) == PHONE
    assert get_paired_client_from_token(second_session)
    assert get_paired_client_from_token(first_session) is None
    with pytest.raises(HTTPException):
        mobile.get_device_from_api_key(request(first_key))
    monkeypatch.setattr(settings, 'instance_id', 'first')
    assert get_paired_client_from_token(first_session)
    assert mobile.get_device_from_api_key(request(first_key))['device_id'] != second_device['device_id']


def test_logout_rejects_old_phone_and_browser_credentials_and_leaves_other_worker(client, monkeypatch):
    bind(FIRST)
    first_key = enroll(client)
    first_session = session(first_key)
    monkeypatch.setattr(settings, 'instance_id', 'second')
    bind(SECOND)
    second_key = enroll(client)
    assert client.delete('/api/v1/mobile/hub/connection').status_code == 403
    client.app.dependency_overrides[require_admin] = lambda: 'local-owner'
    response = client.delete('/api/v1/mobile/hub/connection')
    assert response.status_code == 200 and response.json()['signed_in'] is False
    assert response.headers['cache-control'] == 'no-store'
    assert 'a'*64 not in response.text
    assert account_hub.call.call_args.args[0]['server_id'] == SECOND
    assert store.get_binding() is None
    assert database.get_system_setting(store.secret_key()) is None
    with pytest.raises(HTTPException):
        mobile.get_device_from_api_key(request(second_key))
    # Relinking cannot resurrect the old server's credentials.
    bind('55555555-5555-5555-5555-555555555555')
    with pytest.raises(HTTPException):
        mobile.get_device_from_api_key(request(second_key))
    monkeypatch.setattr(settings, 'instance_id', 'first')
    assert store.get_binding()['server_id'] == FIRST
    assert mobile.get_device_from_api_key(request(first_key))
    assert get_paired_client_from_token(first_session)


def test_legacy_owner_reconnects_and_logout_cannot_resurrect_or_delete_foreign_binding(client, monkeypatch, test_db):
    test_db.execute('INSERT INTO mobile_hub_binding VALUES(1, ?, ?, ?, TRUE)', (HUB, FIRST, 'managed:first'))
    database.set_system_setting('mobile_account_hub_secret', encryption.encrypt_value('a'*64))
    assert store.get_binding()['server_id'] == FIRST
    monkeypatch.setattr(settings, 'instance_id', 'second')
    assert store.get_binding() is None
    bind(SECOND)
    store.disconnect()
    assert test_db.execute('SELECT server_id FROM mobile_hub_binding').fetchone()[0] == FIRST
    monkeypatch.setattr(settings, 'instance_id', 'first')
    assert store.get_binding()['server_id'] == FIRST
    store.disconnect()
    assert store.get_binding() is None
    # Even an old process restoring an obsolete row cannot bypass the tombstone.
    test_db.execute('INSERT INTO mobile_hub_binding VALUES(1, ?, ?, ?, TRUE)', (HUB, FIRST, 'managed:first'))
    assert store.get_binding() is None


@pytest.mark.asyncio
async def test_offline_signout_stops_tunnel_and_clears_only_local_keys(client, monkeypatch):
    bind(FIRST)
    process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock(), kill=Mock())
    monkeypatch.setattr(managed, '_process', process)
    database.set_system_setting('mobile_managed_credential:first', 'local-credential-copy')
    database.set_system_setting('mobile_managed_credential:second', 'keep-other-worker')
    monkeypatch.setattr(account_hub, 'call', Mock(side_effect=HTTPException(503, 'offline')))
    result = await managed.disconnect()
    assert result['signed_in'] is False
    assert 'Signed out on this worker' in result['error']
    process.terminate.assert_called_once()
    assert managed._process is None and store.get_binding() is None
    assert database.get_system_setting('mobile_managed_credential:first') is None
    assert database.get_system_setting('mobile_managed_credential:second') == 'keep-other-worker'


@pytest.mark.asyncio
async def test_cancel_signin_removes_offer_and_cannot_activate_after_restart(client, monkeypatch):
    monkeypatch.setattr(account_hub, 'worker_link_call', Mock(return_value={'ok': True}))
    managed.begin('first')
    offered = managed._pending()
    result = await managed.disconnect()
    assert result['status'] == 'disconnected' and result['code'] is None
    assert account_hub.call.call_args.args[0]['server_id'] == offered['id']
    assert managed._credentials() is None
    managed.begin('first')
    assert managed._pending()['id'] != offered['id']


def test_same_account_phone_coexists_with_legacy_unraid_authorization(client, monkeypatch, test_db):
    test_db.execute('INSERT INTO mobile_hub_binding VALUES(1, ?, ?, ?, TRUE)', (HUB, FIRST, 'managed:first'))
    database.set_system_setting('mobile_account_hub_secret', encryption.encrypt_value('a'*64))
    identity = {'device_id': PHONE, 'server_id': FIRST, 'account_id': ACCOUNT, 'device_name': 'Phone'}
    store.enroll(identity, DeviceInfo(device_id=PHONE, device_name='Phone', platform='android', capabilities={}),
                 hash_api_key('aim_legacy'), datetime.now(timezone.utc) + timedelta(days=1), list(pairing.DEFAULT_MOBILE_SCOPES))
    store.confirm_device(store.ownership(PHONE), True)
    monkeypatch.setattr(settings, 'instance_id', 'second')
    bind(SECOND)
    key = enroll(client)
    assert mobile.get_device_from_api_key(request(key))['device_id'] != PHONE
    monkeypatch.setattr(settings, 'instance_id', 'first')
    assert mobile.get_device_from_api_key(request('aim_legacy'))['device_id'] == PHONE
