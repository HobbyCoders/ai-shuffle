"""Isolated queue/API/runtime tests: never connect to a real Postgres database."""
import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import mobile_tools as queue
from app.services import mobile_tools as service
from app.api import mobile_tools as api


@pytest.fixture
def isolated(monkeypatch):
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lock = threading.RLock()
    class Cursor:
        def __init__(self):
            self.inner = conn.cursor()
        def execute(self, sql, args=()):
            sql = sql.replace('%s', '?').replace(' FOR UPDATE', '')
            self.inner.execute(sql, tuple(x.isoformat() if isinstance(x, datetime) else x for x in args))
            return self
        def fetchone(self):
            return self.inner.fetchone()
        def fetchall(self):
            return self.inner.fetchall()
    class Connection:
        def cursor(self):
            return Cursor()
    @contextmanager
    def db():
        with lock:
            try:
                yield Connection()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    monkeypatch.setattr(queue, 'get_db', db)
    conn.execute('CREATE TABLE mobile_devices(device_id TEXT PRIMARY KEY)')
    conn.executemany('INSERT INTO mobile_devices VALUES (?)', [('a',), ('b',)])
    queue.init_schema()
    devices = {key: {'device_id': key, 'device_name': key, 'scopes': ['mobile:actions'],
                     'api_key_expires_at': datetime.now(timezone.utc) + timedelta(days=1)} for key in ('a', 'b')}
    monkeypatch.setattr(service.mobile, 'get_mobile_device', devices.get)
    monkeypatch.setattr(service, 'device_authorizer', lambda session: {'sa': ['a'], 'sb': ['b']}.get(session, []))
    yield conn, devices
    conn.close()


def enable(device='a'):
    queue.capabilities(device, True, ['open_url', 'read_screen'])
    queue.claim(device)


def enqueue():
    return queue.enqueue('a', 'sa', 'open_url', '{"url":"https://example.com"}', 20)['id']


@pytest.mark.parametrize('name,args', [
    ('tap_node', {'ref': 'snapshot:node'}),
    ('scroll', {'ref': 'snapshot:node', 'direction': 'down'}),
    ('go_back', {'ref': 'snapshot:node'}),
])
def test_accessibility_requires_android_snapshot_reference(name, args):
    assert json.loads(service.validate_arguments(name, args)) == args
    with pytest.raises(service.CommandError):
        service.validate_arguments(name, {k: v for k, v in args.items() if k != 'ref'})
    with pytest.raises(service.CommandError):
        service.validate_arguments(name, {**args, 'node_id': 'untrusted'})


def test_claim_target_and_replay(isolated):
    enable()
    enable('b')
    cmd = enqueue()
    assert queue.claim('b') == []
    assert queue.submit_result('a', cmd, '{}') == 'not_claimed'
    assert [r['id'] for r in queue.claim('a')] == [cmd]
    assert queue.claim('a') == []
    assert queue.submit_result('b', cmd, '{}') == 'not_found'
    assert queue.submit_result('a', cmd, '{"success":true}') is None
    assert queue.submit_result('a', cmd, '{"success":true}') is None
    assert queue.submit_result('a', cmd, '{}') == 'result_conflict'
    assert queue.outcome(cmd, 'sb') is None


def test_expiry_scrubs_payload(isolated, monkeypatch):
    enable()
    cmd = enqueue()
    queue.claim('a')
    future = queue.now() + timedelta(seconds=21)
    monkeypatch.setattr(queue, 'now', lambda: future)
    assert queue.submit_result('a', cmd, '{}') == 'expired'
    row = isolated[0].execute('SELECT * FROM mobile_tool_commands WHERE id=?', (cmd,)).fetchone()
    assert row['arguments'] == '{}' and row['result'] is None


def test_disabled_stale_and_unsupported(isolated, monkeypatch):
    assert queue.enqueue('a', 'sa', 'read_screen', '{}', 20)['error'] == 'device_disabled'
    queue.capabilities('a', True, ['read_screen'])
    assert queue.enqueue('a', 'sa', 'read_screen', '{}', 20)['error'] == 'device_offline'
    queue.claim('a')
    assert queue.enqueue('a', 'sa', 'open_app', '{}', 20)['error'] == 'unsupported_tool'
    future = queue.now() + timedelta(seconds=31)
    monkeypatch.setattr(queue, 'now', lambda: future)
    assert queue.enqueue('a', 'sa', 'read_screen', '{}', 20)['error'] == 'device_offline'


def test_unchanged_capabilities_preserve_pending_and_claimed_commands(isolated):
    enable()
    cmd = enqueue()
    heartbeat = queue.state('a')['heartbeat_at']
    queue.capabilities('a', True, ['open_url', 'read_screen'])
    assert queue.state('a')['heartbeat_at'] == heartbeat
    assert [r['id'] for r in queue.claim('a')] == [cmd]
    queue.capabilities('a', True, ['open_url', 'read_screen'])
    assert queue.submit_result('a', cmd, '{}') is None


def test_disable_cancels_claimed(isolated):
    enable()
    cmd = enqueue()
    queue.claim('a')
    queue.capabilities('a', False, [])
    assert queue.submit_result('a', cmd, '{}') == 'not_claimed'
    assert queue.outcome(cmd, 'sa')['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_cross_account_and_no_identity(isolated, monkeypatch):
    enable()
    assert service.list_devices('sa')['devices'][0]['device_id'] == 'a'
    assert (await service.execute('sb', 'a', 'read_screen', {}))['error'] == 'device_not_authorized'
    monkeypatch.setattr(service, 'device_authorizer', None)
    assert 'controller_context_missing' in (await service.execute('sa', 'a', 'read_screen', {}))['error']


@pytest.mark.parametrize('field,value', [('revoked_at', 'now'), ('api_key_expires_at', datetime.now(timezone.utc) - timedelta(seconds=1))])
def test_revoked_expired_device_not_listed(isolated, field, value):
    isolated[1]['a'][field] = value
    assert service.list_devices('sa') == {'devices': [], 'error': 'no_devices'}


@pytest.mark.asyncio
async def test_timeout_and_cancellation(isolated):
    enable()
    start = asyncio.get_running_loop().time()
    assert (await service.execute('sa', 'a', 'read_screen', {}, timeout=0.03))['error'] == 'device_timeout'
    assert asyncio.get_running_loop().time() - start < 1
    task = asyncio.create_task(service.execute('sa', 'a', 'read_screen', {}))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.claim('a') == []


@pytest.mark.asyncio
async def test_actual_result_and_handoff(isolated, monkeypatch):
    enable()
    monkeypatch.setattr(queue, 'outcome', lambda *args: {'status': 'completed', 'result': '{"success":true,"detail":"intent opened"}'})
    result = await service.execute('sa', 'a', 'open_url', {'url': 'https://example.com'})
    assert result['result'] == {'success': True, 'detail': 'intent opened', 'status': 'launched_confirmation'}


@pytest.mark.parametrize('name,args', [
    ('unknown', {}), ('read_screen', {'confirmed': True}), ('open_url', {'url': 'javascript:alert(1)'}),
    ('set_timer', {'seconds': True}), ('set_timer', {'seconds': 0}), ('set_alarm', {'hour': 24, 'minute': 0}),
    ('scroll', {'direction': 'diagonal'}), ('open_app', {'package_name': '../evil'}),
    ('create_calendar_event', {'title': 'x', 'start_time': '2026-01-01'}),
])
def test_argument_validation(name, args):
    with pytest.raises(service.CommandError):
        service.validate_arguments(name, args)


def test_payload_bounds():
    for payload in ({'x': 'x' * 65536}, {'x': float('nan')}):
        with pytest.raises(service.CommandError):
            service.bounded_json(payload, service.MAX_RESULT_BYTES)
    assert service.bounded_json({'b': 1, 'a': 2}, 100) == service.bounded_json({'a': 2, 'b': 1}, 100)


@pytest.fixture
def client(isolated):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.device_auth] = lambda: isolated[1]['a']
    with TestClient(app) as client:
        yield client


def test_api_contract(client, isolated):
    base = '/api/v1/mobile/tools'
    assert client.post(base + '/capabilities', json={'enabled': True, 'tools': ['root_shell']}).status_code == 422
    assert client.post(base + '/capabilities', json={'enabled': True, 'tools': ['open_url']}).json() == {'ok': True}
    assert client.get(base + '/pending').json() == {'commands': []}
    cmd = enqueue()
    commands = client.get(base + '/pending').json()['commands']
    assert set(commands[0]) == {'id', 'name', 'arguments', 'expires_at'}
    assert commands[0]['arguments'] == {'url': 'https://example.com'}
    assert client.get(base + '/pending').json()['commands'] == []
    assert client.post(base + '/' + cmd + '/result', json={'result': {'success': True}}).json() == {'ok': True}
    assert client.post(base + '/' + cmd + '/result', json={'result': {'success': False}}).status_code == 409
    assert client.post(base + '/capabilities', json={}).json() == {'ok': True}
    assert not queue.state('a')['enabled']


def test_claim_rechecks_requesting_account(client, isolated, monkeypatch):
    enable()
    cmd = enqueue()
    monkeypatch.setattr(service, 'device_authorizer', lambda session: [])
    assert client.get('/api/v1/mobile/tools/pending').json() == {'commands': []}
    assert queue.outcome(cmd, 'sa')['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_runtime_registration_discovery(monkeypatch):
    import claude_agent_sdk
    from app.mcp import builtin_tools
    captured = []
    def server(**kwargs):
        captured.extend(kwargs['tools'])
        return {'name': kwargs['name']}
    monkeypatch.setattr(claude_agent_sdk, 'create_sdk_mcp_server', server)
    assert builtin_tools.create_builtin_tools_server(session_id='sa', is_admin=False)
    tools = {t.name: t for t in captured}
    assert {'AndroidDevices', 'AndroidAction'} <= tools.keys()
    assert 'mcp__builtins__AndroidAction' in builtin_tools.get_builtin_tool_names()
    assert len(tools['AndroidAction'].input_schema['oneOf']) == len(service.TOOLS)
    monkeypatch.setattr(service, 'device_authorizer', None)
    response = await tools['AndroidDevices'].handler({})
    assert 'controller_context_missing' in response['content'][0]['text']


@pytest.mark.asyncio
async def test_legacy_first_device_disabled(monkeypatch):
    from app.mcp import phone_tools
    assert await phone_tools._get_default_device() is None
