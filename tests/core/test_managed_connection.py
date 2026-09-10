import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import mobile
from app.api.auth import require_admin
from app.core import account_hub, encryption, managed_connection as managed
from app.core.config import settings
from app.db import database, mobile_hub


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(managed, 'settings', SimpleNamespace(port=8000, effective_instance_id='this-install'))
    monkeypatch.setattr(settings, 'instance_id', 'this-install')
    monkeypatch.setattr(managed, '_generation', 0)
    monkeypatch.setattr(managed.cloudflared, 'find_executable', lambda: '/usr/local/bin/cloudflared')
    monkeypatch.setattr(managed, '_process', None)
    monkeypatch.setattr(managed, '_state', 'disconnected')
    monkeypatch.setattr(managed, '_error', None)
    monkeypatch.setattr(managed, '_url', None)
    monkeypatch.setattr(managed, '_protected_listener_ready', lambda: True)


def offer(monkeypatch):
    call = Mock(return_value={'ok': True})
    monkeypatch.setattr(account_hub, 'worker_link_call', call)
    managed.begin('Unraid')
    return managed._pending(), call


def test_locked_storage_is_reported_before_offer_even_without_pending_data(monkeypatch):
    monkeypatch.setattr(encryption, '_encryption_key', None)
    monkeypatch.setattr(encryption, '_credential_status', {'ready': False, 'state': 'recovery_required'})
    call = Mock()
    monkeypatch.setattr(account_hub, 'worker_link_call', call)
    assert managed.status()['status'] == 'locked'
    assert 'matching credential key' in managed.status()['error']
    with pytest.raises(HTTPException) as error:
        managed.begin('My worker')
    assert error.value.status_code == 503
    call.assert_not_called()
    assert database.get_system_setting(managed._key()) is None


def test_missing_connector_is_actionable_before_offer(monkeypatch):
    monkeypatch.setattr(managed.cloudflared, 'find_executable', lambda: None)
    assert managed.status()['status'] == 'unavailable'
    with pytest.raises(HTTPException) as error:
        managed.begin('My worker')
    assert error.value.status_code == 503
    assert 'python -m app.core.cloudflared' in error.value.detail
    assert database.get_system_setting(managed._key()) is None


def test_offer_is_encrypted_hash_only_and_retries_reuse_it(monkeypatch):
    pending, calls = offer(monkeypatch)
    first = managed.status()
    assert managed.begin('Changed name')['code'] == first['code']
    assert managed._pending() == pending
    stored = database.get_system_setting(managed._key())
    assert pending['secret'] not in stored and pending['code'] not in stored
    sent = calls.call_args.args[1]
    assert sent['secret_hash'] == hashlib.sha256(pending['secret'].encode()).hexdigest()
    assert sent['code_hash'] == hashlib.sha256(pending['code'].encode()).hexdigest()
    assert pending['secret'] not in json.dumps(sent)
    assert pending['secret'] not in json.dumps(first)


def test_no_connection_before_phone_approval(monkeypatch):
    pending, calls = offer(monkeypatch)
    calls.return_value = {'status': 'pending'}
    server_call = Mock()
    monkeypatch.setattr(account_hub, 'call', server_call)
    assert managed._credentials() is None
    assert mobile_hub.get_binding() is None
    server_call.assert_not_called()
    calls.return_value = {'status': 'approved', 'server_id': 'other-worker'}
    with pytest.raises(ValueError):
        managed._credentials()
    assert mobile_hub.get_binding() is None


def test_approved_connection_survives_restart_and_lost_activation(monkeypatch):
    pending, calls = offer(monkeypatch)
    calls.return_value = {'status': 'approved', 'server_id': pending['id']}
    server_call = Mock(side_effect=HTTPException(503, 'Retry'))
    monkeypatch.setattr(account_hub, 'call', server_call)
    with pytest.raises(HTTPException):
        managed._credentials()
    binding = mobile_hub.get_binding()
    assert not binding['active']
    assert managed.status()['signed_in'] is True
    assert managed.status()['profile_ready'] is False
    assert managed.status()['status'] == 'connecting'
    assert encryption.decrypt_value(binding['encrypted_secret']) == pending['secret']
    server_call.side_effect = None
    server_call.return_value = {'tunnel_token': 'x' * 64, 'url': 'https://worker.example.com', 'remote_port': 8001}
    assert managed._credentials() == ('x' * 64, 'https://worker.example.com')
    assert managed._pending() is None
    assert mobile_hub.get_binding()['active']
    assert managed.status()['profile_ready'] is True
    managed._credentials()
    assert server_call.call_args.args[2] == 'connector'


def test_shared_database_worker_cannot_reuse_foreign_legacy_binding_but_can_sign_in(monkeypatch, test_db):
    test_db.execute("""INSERT INTO mobile_hub_binding VALUES
        (1, 'https://hub.example.com', '11111111-1111-1111-1111-111111111111', 'managed:another-install', TRUE)""")
    database.set_system_setting('mobile_account_hub_secret', encryption.encrypt_value('a' * 64))
    server_call = Mock()
    monkeypatch.setattr(account_hub, 'call', server_call)
    assert managed._credentials() is None
    server_call.assert_not_called()
    assert managed.status()['status'] == 'disconnected'
    pending, calls = offer(monkeypatch)
    calls.return_value = {'status': 'approved', 'server_id': pending['id']}
    server_call.return_value = {'tunnel_token': 'x'*64, 'url': 'https://worker.example.com', 'remote_port': 8001}
    managed._credentials()
    assert mobile_hub.get_binding()['server_id'] == pending['id']
    assert managed.status()['signed_in'] is True
    assert test_db.execute('SELECT linked_by FROM mobile_hub_binding').fetchone()[0] == 'managed:another-install'
    assert encryption.decrypt_value(database.get_system_setting('mobile_account_hub_secret')) == 'a'*64


@pytest.mark.parametrize('status,body,expected', [
    (200, {'ready': True, 'server_id': 'expected', 'protected_ingress': True}, True),
    (200, {'ready': True, 'server_id': 'expected'}, False),
    (200, {'ready': True, 'server_id': 'expected', 'protected_ingress': False}, False),
    (200, {'ready': True, 'server_id': 'wrong'}, False),
    (200, {'ready': False, 'server_id': 'expected'}, False),
    (302, {'ready': True, 'server_id': 'expected'}, False),
    (503, {}, False),
])
def test_readiness_checks_actual_route_identity_without_credentials(monkeypatch, status, body, expected):
    conn = Mock()
    conn.getresponse.return_value.status = status
    conn.getresponse.return_value.read.return_value = json.dumps(body).encode()
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', Mock(return_value=conn))
    assert managed._reachable({'server_id': 'expected'}, 'https://worker.example.com') is expected
    assert 'Authorization' not in conn.request.call_args.kwargs['headers']
    conn.close.assert_called_once()


def test_connection_api_requires_admin_and_omits_secrets(monkeypatch):
    _, calls = offer(monkeypatch)
    app = FastAPI()
    app.include_router(mobile.router)
    client = TestClient(app)
    assert client.get('/api/v1/mobile/hub/connection').status_code == 403
    assert client.post('/api/v1/mobile/hub/connection', json={'name': 'Unraid'}).status_code == 403
    app.dependency_overrides[require_admin] = lambda: 'admin'
    response = client.get('/api/v1/mobile/hub/connection')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert '<svg' in response.json()['qr_svg']
    assert 'secret' not in response.text and 'tunnel_token' not in response.text
    assert client.post('/api/v1/mobile/hub/connection', json={'name': '  '}).status_code == 400


@pytest.mark.asyncio
async def test_supervisor_stops_on_revocation_and_keeps_token_out_of_arguments(monkeypatch):
    process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock(), kill=Mock())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(managed.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(managed, '_credentials', Mock(return_value=('private-token' * 8, 'https://worker.example.com')))
    monkeypatch.setattr(managed, '_check_authorized', Mock(side_effect=ValueError('revoked')))
    async def inline(fn, *args): return fn(*args)
    monkeypatch.setattr(managed, 'run_sync', inline)
    sleeps = 0
    async def tick(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            assert managed._state == 'reconnecting'
            process.terminate.assert_called_once()
            raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    monkeypatch.setenv('PROVIDER_SECRET', 'never-inherit')
    with pytest.raises(asyncio.CancelledError): await managed.supervise()
    assert 'private-token' not in str(spawn.call_args.args)
    assert spawn.call_args.kwargs['env']['TUNNEL_TOKEN'] == 'private-token' * 8
    assert 'PROVIDER_SECRET' not in spawn.call_args.kwargs['env']
    assert managed._process is None


def test_tunnel_upgrade_requires_live_protected_socket_before_hub_mutation(monkeypatch):
    pending, calls = offer(monkeypatch)
    calls.return_value = {'status': 'approved', 'server_id': pending['id']}
    server_call = Mock()
    monkeypatch.setattr(account_hub, 'call', server_call)
    monkeypatch.setattr(managed, '_protected_listener_ready', lambda: False)
    with pytest.raises(ValueError, match='listener'):
        managed._credentials()
    server_call.assert_not_called()
    assert not mobile_hub.get_binding()['active']


@pytest.mark.parametrize('port', [None, 8000, '8001', 9000])
def test_tunnel_upgrade_rejects_missing_or_wrong_origin_acknowledgement(monkeypatch, port):
    pending, calls = offer(monkeypatch)
    calls.return_value = {'status': 'approved', 'server_id': pending['id']}
    server_call = Mock(return_value={'remote_port': port, 'tunnel_token': 'x'*64, 'url': 'https://worker.example.com'})
    monkeypatch.setattr(account_hub, 'call', server_call)
    with pytest.raises(ValueError, match='verified'):
        managed._credentials()
    assert not mobile_hub.get_binding()['active']
    assert server_call.call_args.args[1:] == ('POST', 'activate', {'remote_port': 8001})
    assert managed._pending()['id'] == pending['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('during_spawn', [False, True])
async def test_signout_wins_over_credentials_and_connector_start(monkeypatch, during_spawn):
    process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock(), kill=Mock())
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(managed, 'run_sync', inline)
    def credentials():
        if not during_spawn:
            managed._disconnect_local()
        return 'private-token'*8, 'https://worker.example.com'
    monkeypatch.setattr(managed, '_credentials', credentials)
    async def spawn(*args, **kwargs):
        await managed.disconnect()
        return process
    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(managed.asyncio, 'create_subprocess_exec', spawn_mock)
    async def stop_after_tick(delay):
        raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', stop_after_tick)
    if during_spawn:
        # Stop the loop after the stale child was terminated, rather than let a
        # synthetic credentials stub continue creating children forever.
        original = managed._stop
        stops = 0
        async def stop():
            nonlocal stops
            await original()
            stops += 1
            if stops == 2:
                raise asyncio.CancelledError()
        monkeypatch.setattr(managed, '_stop', stop)
    with pytest.raises(asyncio.CancelledError):
        await managed.supervise()
    assert managed._process is None
    if during_spawn:
        process.terminate.assert_called_once()
    else:
        spawn_mock.assert_not_called()
