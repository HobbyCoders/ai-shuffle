"""Real WS ingress/handler/service bridge with isolated DB and transport.

The suite's SQLite adapter tests persistence and rollback, not PostgreSQL row
locking. A serialized adapter test additionally asserts FOR UPDATE is emitted.
"""
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
from app.core import mobile_tool_identity as identity, owner_auth
from app.db import mobile_tool_identity as store, database
from app.services import mobile_tools
from app.api import websocket as route
from app.api import chat_ws_handler as cwh
from app.api.chat_ws_handler import ChatWebSocketHandler
from tests.api.test_chat_ws_v2_turns import _Patches


@pytest.fixture
def world(monkeypatch):
    clients = {n: {'id': n, 'scopes': ['controller']} for n in ('one', 'two', 'foreign', 'desktop', 'local')}
    owners = {n: dict(device_id=n, paired_client_id=n, account_id='B' if n == 'foreign' else 'A',
                      server_id='server', hub_device_id=n) for n in ('one', 'two', 'foreign')}
    devices = {n: dict(device_id=n, device_name=n, paired_client_id=n,
                       scopes=['mobile:actions'], api_key_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
               for n in ('one', 'two', 'foreign', 'local')}
    revoked = set()
    def ownership(device_id=None, *, paired_client_id=None):
        return owners.get(paired_client_id or device_id)
    def active(owner):
        if owner and owner['device_id'] in revoked:
            raise PermissionError('revoked')
    def client_by_hash(h):
        return next((c for n, c in clients.items() if identity.digest('shc_' + n) == h and n not in revoked), None)
    monkeypatch.setattr(identity.pairing, 'get_paired_client_by_session_hash', client_by_hash)
    monkeypatch.setattr(identity.pairing, 'get_paired_client', clients.get)
    monkeypatch.setattr(identity.mobile_hub, 'ownership', ownership)
    monkeypatch.setattr(identity.account_hub, 'require_active', active)
    monkeypatch.setattr(identity.account_hub, 'check_paired_client', lambda c: active(ownership(paired_client_id=c['id'])))
    monkeypatch.setattr(identity.mobile, 'get_mobile_devices', lambda: list(devices.values()))
    monkeypatch.setattr(identity.mobile, 'get_mobile_device', devices.get)
    monkeypatch.setattr(store, 'admin_active', lambda h: h == identity.digest('shr_admin-token') and 'admin' not in revoked)
    monkeypatch.setattr(database, 'get_auth_session', lambda t: {'token': t} if t == 'shr_admin-token' and 'admin' not in revoked else None)
    monkeypatch.setattr(database, 'get_api_key_session', lambda t: None)
    monkeypatch.setattr(database, 'get_api_user_by_key_hash', lambda h: None)
    monkeypatch.setattr(database, 'get_paired_client_by_session_hash', client_by_hash)
    monkeypatch.setattr(database, 'touch_paired_client', lambda _: None)
    monkeypatch.setattr(mobile_tools.queue, 'state', lambda _: None)
    return clients, owners, devices, revoked


def create(sid, principal=None):
    database.create_session(sid, 'default')
    identity.bind_session(sid, principal, new=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('token,expected', [('shc_one', {'one', 'two'}), ('shr_admin-token', {'local'})])
async def test_real_route_create_handler_to_tool(world, monkeypatch, token, expected):
    monkeypatch.setattr(owner_auth.settings, 'shuffle_local_access', False)
    ws = SimpleNamespace(state=SimpleNamespace(), cookies={}, accept=AsyncMock(), close=AsyncMock(),
                         scope={'shuffle.ingress': 'local'}, client=SimpleNamespace(host='127.0.0.1'),
                         headers={'host': 'localhost'})
    captured = {}
    async def run(handler):
        handler.send_json = AsyncMock(return_value=True)
        with _Patches(), patch.object(cwh.turn_manager,
                                      'submit_async', AsyncMock(return_value='turn')):
            await handler.handle_query({'prompt': 'hello', 'profile': 'default'})
        frames = [x.args[0] for x in handler.send_json.call_args_list if x.args[0].get('type') == 'session_created']
        assert len(frames) == 1
        sid = frames[0]['session_id']
        captured['sid'] = sid
        assert set(identity.authorized_device_ids(sid)) == expected
        # Exercise the real service with the handler's persisted runtime key.
        listed = mobile_tools.list_devices(sid)
        assert set(d['device_id'] for d in listed['devices']) == expected
    monkeypatch.setattr(ChatWebSocketHandler, 'run', run)
    await route.chat_websocket(ws, token=token, device_id='foreign-query-hint')
    row = store.read(captured['sid'])
    assert row['credential_hash'] == identity.digest(token)
    assert token not in str(row)
    world[3].add('one' if token.startswith('shc_') else 'admin')
    assert identity.authorized_device_ids(captured['sid']) == []


def test_account_resume_cross_account_revocation_and_scope(world):
    one = identity.principal('paired', 'one', 'shc_one')
    create('session', one)
    two = identity.principal('paired', 'two', 'shc_two')
    identity.bind_session('session', two)
    assert store.read('session')['principal_id'] == 'two'
    foreign = identity.principal('paired', 'foreign', 'shc_foreign')
    with pytest.raises(PermissionError):
        identity.bind_session('session', foreign)
    assert store.read('session')['principal_id'] == 'two'
    world[2]['two']['scopes'] = []
    assert identity.authorized_device_ids('session') == []
    world[2]['two']['scopes'] = ['mobile:actions']
    world[3].add('one')
    assert identity.authorized_device_ids('session') == []  # conservative failed live check


def test_direct_desktop_and_api_never_gain_global_access(world):
    create('desktop', identity.principal('paired', 'desktop', 'shc_desktop'))
    assert identity.authorized_device_ids('desktop') == []
    create('api', identity.principal('api_user', 'api', 'key'))
    assert identity.authorized_device_ids('api') == []
    world[1].pop('one')
    create('direct', identity.principal('paired', 'one', 'shc_one'))
    assert identity.authorized_device_ids('direct') == ['one']


def test_phone_on_other_worker_does_not_disable_this_workers_tools(world):
    create('current-worker', identity.principal('paired', 'one', 'shc_one'))
    world[1]['two']['server_id'] = 'another-worker'
    world[3].add('two')
    assert identity.authorized_device_ids('current-worker') == ['one']


def test_legacy_missing_and_explicit_admin_claim(world):
    create('old')
    assert identity.authorized_device_ids('old') == []
    admin = identity.principal('admin', 'install', 'shr_admin-token')
    identity.bind_session('old', admin)
    assert store.read('old') is None
    with pytest.raises(PermissionError):
        identity.bind_session('old', identity.principal('paired', 'one', 'shc_one'), claim=True)
    identity.bind_session('old', admin, claim=True)
    assert identity.authorized_device_ids('old') == ['local']
    with pytest.raises(PermissionError):
        identity.bind_session('old', None)
    world[3].add('admin')
    assert identity.authorized_device_ids('old') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('method,data', [('handle_query', {'prompt': 'stolen'}),
                                        ('handle_queue_message', {'content': 'stolen'}),
                                        ('handle_load_session', {})])
@pytest.mark.parametrize('caller', [None, 'foreign'])
async def test_public_paths_cannot_reuse_authorized_session(world, method, data, caller):
    create('owned', identity.principal('paired', 'one', 'shc_one'))
    principal = identity.principal('paired', caller, 'shc_' + caller) if caller else None
    handler = ChatWebSocketHandler(None, 'spoofed-device', 'admin', None, trusted_principal=principal)
    handler.send_json = AsyncMock()
    with patch('app.api.chat_ws_handler.turn_manager.submit_async', AsyncMock()) as submit:
        await getattr(handler, method)(dict(data, session_id='owned'))
    submit.assert_not_called()
    assert handler.send_json.call_args.args[0]['type'] == 'error'
    assert store.read('owned')['principal_id'] == 'one'


def test_startup_schema_order():
    import inspect
    from app.core.startup import init_core
    source = inspect.getsource(init_core)
    assert source.index('init_database()') < source.index('run_migrations()') < source.index('mobile_tools.init_schema()') < source.index('mobile_tool_identity.init_schema()')
    assert mobile_tools.device_authorizer is identity.authorized_device_ids


def test_recovery_digest_rechecks_actual_hashed_session_row(test_db):
    from app.core import owner_auth
    row = (identity.digest('shr_synthetic'), owner_auth.settings.effective_instance_id, 'recovery', '2999-01-01')
    test_db.execute("INSERT INTO owner_local_sessions VALUES (?, ?, ?, ?)", row)
    assert store.admin_active(identity.digest('shr_synthetic'))
    assert not store.admin_active(identity.digest('other'))
    test_db.execute("DELETE FROM owner_local_sessions WHERE token_hash = ?", (row[0],))
    assert not store.admin_active(row[0])
    test_db.execute("INSERT INTO owner_local_sessions VALUES (?, ?, ?, ?)", (*row[:3], '2000-01-01'))
    assert not store.admin_active(row[0])


def test_concurrent_first_bind_no_rebind(world, monkeypatch):
    # SQLite cannot exercise PG locking: serialize complete transactions here,
    # and assert production SQL locks the parent before reading the absent row.
    create('race')
    original = store.get_db
    lock = threading.RLock()
    statements = []
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def cursor(self):
            cursor = self.conn.cursor()
            class Cursor:
                def execute(self, sql, args=()):
                    statements.append(sql)
                    cursor.execute(sql, args)
                def fetchone(self):
                    return cursor.fetchone()
            return Cursor()
    @contextmanager
    def db():
        with lock, original() as conn:
            yield Connection(conn)
    monkeypatch.setattr(store, 'get_db', db)
    principals = [identity.principal('paired', n, 'shc_' + n) for n in ('one', 'foreign')]
    def bind(principal):
        try:
            store.bind('race', principal, new=True)
            return True
        except PermissionError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, principals))
    assert sorted(results) == [False, True]
    assert sum('FOR UPDATE' in sql for sql in statements) == 2
    assert store.read('race')['owner'] in {p['owner'] for p in principals}


@pytest.mark.asyncio
async def test_revocation_during_action_wait_cancels(world, monkeypatch):
    create('running', identity.principal('paired', 'one', 'shc_one'))
    monkeypatch.setattr(mobile_tools.queue, 'state', lambda _: {'enabled': True, 'tools': '["read_screen"]'})
    def enqueue(*args):
        world[3].add('one')
        return {'id': 'cmd'}
    monkeypatch.setattr(mobile_tools.queue, 'enqueue', enqueue)
    cancel = Mock()
    monkeypatch.setattr(mobile_tools.queue, 'cancel', cancel)
    result = await mobile_tools.execute('running', 'one', 'read_screen', {})
    assert result == {'error': 'device_not_authorized'}
    cancel.assert_called_once_with('cmd', 'running')


def test_schema_idempotent_and_delete_cascades(world):
    store.init_schema()
    store.init_schema()
    create('deleted', identity.principal('paired', 'one', 'shc_one'))
    with store.get_db() as conn:
        conn.cursor().execute('DELETE FROM sessions WHERE id = %s', ('deleted',))
    assert store.read('deleted') is None


def test_startup_runs_extension_initializers_in_order(monkeypatch):
    from app.core import startup
    from app.db import base
    calls = []
    class EndOfSetup(Exception):
        pass
    for name in ('_reject_legacy_sqlite', 'ensure_directories', 'init_database', 'run_migrations'):
        monkeypatch.setattr(startup, name, lambda n=name: calls.append(n))
    monkeypatch.setattr(base, 'init_pool', lambda **_: calls.append('pool'))
    monkeypatch.setattr(mobile_tools.queue, 'init_schema', lambda: calls.append('queue'))
    monkeypatch.setattr(store, 'init_schema', lambda: calls.append('identity'))
    def stop():
        raise EndOfSetup
    monkeypatch.setattr(startup, '_adopt_orphaned_instance_data', stop)
    with pytest.raises(EndOfSetup):
        startup.init_core()
    assert calls == ['_reject_legacy_sqlite', 'ensure_directories', 'pool', 'init_database', 'run_migrations', 'queue', 'identity']
