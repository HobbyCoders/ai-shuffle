"""Controller renewal uses real pairing and chat-binding producers, no inference."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from app.core import mobile_tool_identity as identity, session_orchestration as orch
from app.db import pairing, mobile_tool_identity as store, database, sessions
from app.db.base import get_db
from tests.core.test_session_orchestration import chat


def client(name):
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    pairing.create_pairing_grant(
        token_hash='grant-' + name, purpose='controller', scopes=['controller'],
        target_install_id=None, endpoint_url='https://worker.example',
        device_label=None, created_by='test', expires_at=expiry)
    return pairing.exchange_grant_for_client(
        token_hash='grant-' + name, device_id='device-' + name,
        device_name=name, platform='browser', client_type='browser',
        session_token_hash=identity.digest('initial-' + name),
        session_expires_at=expiry)['client']


@pytest.fixture(params=['web', 'native'])
def bound(request, mock_db, monkeypatch):
    purpose = request.param
    device = client('controller')
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    pairing.set_paired_client_session(device['id'], identity.digest('old'), expiry, purpose=purpose)
    principal = identity.principal('paired', device['id'], 'old')
    database.create_profile('agent', 'Agent', None, {'model': 'sonnet'})
    for sid in ('caller', 'peer'):
        chat(sid)
        identity.bind_session(sid, principal, new=True)
    # A long-running chat can predate the browser credential's entire lifetime.
    with get_db() as conn:
        conn.cursor().execute('UPDATE sessions SET created_at=%s WHERE id=%s',
                              ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), 'caller'))
    monkeypatch.setattr(orch, '_wake_peer', AsyncMock())
    return device, purpose, expiry, principal


def rotate(bound, token='renewed'):
    device, purpose, expiry, _ = bound
    return pairing.set_paired_client_session(device['id'], identity.digest(token), expiry, purpose=purpose)


async def invoke(action, data):
    from fluid.tools.registry import ToolCtx
    return await orch.make_handler(lambda: 'caller')(action, data, ToolCtx())


async def test_rotation_keeps_all_session_builtins_working(bound):
    before = await invoke('list', {'scope': 'all'})
    assert not before.is_error, before.text
    assert rotate(bound)
    assert store.read('caller')['credential_hash'] == identity.digest('renewed')
    assert store.read('caller')['owner'] == bound[3]['owner']
    # A socket or request holding the old credential does NOT inherit renewal.
    with pytest.raises(PermissionError):
        identity.revalidate(bound[3])
    assert pairing.get_paired_client_by_session_hash(identity.digest('old')) is None
    listed = await invoke('list', {'scope': 'all'})
    assert not listed.is_error, listed.text
    assert [r['id'] for r in json.loads(listed.text)['sessions']] == ['peer']
    started = await invoke('start', {'title': 'Review', 'prompt': 'Review code', 'request_id': 'start'})
    assert not started.is_error, started.text
    child = json.loads(started.text)['session_id']
    assert store.read(child)['credential_hash'] == identity.digest('renewed')
    sent = await invoke('message', {'session_id': 'peer', 'message': 'Coordinate', 'request_id': 'message'})
    assert not sent.is_error, sent.text
    archived = await invoke('archive', {'session_id': child})
    assert not archived.is_error, archived.text
    assert sessions.get_session(child)['status'] == 'archived'


@pytest.mark.parametrize('action,data', [
    ('list', {'scope': 'all'}),
    ('start', {'title': 'Review', 'prompt': 'Review', 'request_id': 'start'}),
    ('message', {'session_id': 'peer', 'message': 'Review', 'request_id': 'message'}),
    ('archive', {'session_id': 'peer'}),
])
async def test_revocation_refuses_every_builtin(bound, action, data):
    assert rotate(bound)
    assert pairing.revoke_paired_client(bound[0]['id'])
    assert rotate(bound, 'after-revoke') is None
    result = await invoke(action, data)
    assert result.is_error
    assert 'paired controller credential' in result.text
    assert 'reopen/continue this chat' in result.text


def test_logout_then_remint_does_not_resurrect_binding(bound):
    assert pairing.clear_paired_client_session(bound[0]['id'], identity.digest('old'))
    assert rotate(bound)
    assert store.read('caller')['credential_hash'] == identity.digest('old')
    with pytest.raises(PermissionError):
        identity.validate(store.read('caller'))
    # Recovery needs a fresh, authenticated bind, not a lookup by device id.
    identity.bind_session('caller', identity.principal('paired', bound[0]['id'], 'renewed'))
    identity.validate(store.read('caller'))


def test_expiry_refuses_until_authenticated_renewal(bound):
    device, purpose, _, _ = bound
    with get_db() as conn:
        c = conn.cursor()
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if purpose == 'web':
            c.execute('UPDATE paired_clients SET session_expires_at=%s WHERE id=%s', (expired, device['id']))
        else:
            c.execute('UPDATE paired_client_native_sessions SET expires_at=%s WHERE client_id=%s',
                      (expired, device['id']))
    with pytest.raises(PermissionError):
        identity.validate(store.read('caller'))
    assert rotate(bound)
    identity.validate(store.read('caller'))


def test_other_device_or_slot_cannot_refresh_binding(bound):
    other = client('other')
    pairing.set_paired_client_session(other['id'], identity.digest('other-new'), bound[2], purpose=bound[1])
    assert store.read('caller')['credential_hash'] == identity.digest('old')
    forged = dict(store.read('caller'), credential_hash=identity.digest('other-new'))
    with pytest.raises(PermissionError):
        identity.validate(forged)
    with pytest.raises(PermissionError, match='principal mismatch'):
        identity.bind_session('caller', identity.principal('paired', other['id'], 'other-new'))
    other_slot = 'native' if bound[1] == 'web' else 'web'
    pairing.set_paired_client_session(bound[0]['id'], identity.digest('other-slot'), bound[2], purpose=other_slot)
    assert store.read('caller')['credential_hash'] == identity.digest('old')
    identity.validate(store.read('caller'))


def test_changed_owner_never_inherits_authority(bound, monkeypatch):
    # A membership change must still fail the stored account comparison after renewal.
    monkeypatch.setattr(identity.account_hub, 'check_paired_client', lambda c: None)
    monkeypatch.setattr(identity.account_hub, 'require_active', lambda o: None)
    monkeypatch.setattr(identity.mobile_hub, 'ownership', lambda **kw: {'account_id': 'different-owner'})
    assert rotate(bound)
    assert store.read('caller')['account_id'] is None
    with pytest.raises(PermissionError, match='Account membership changed'):
        identity.validate(store.read('caller'))


def test_renewal_failure_rolls_back_slot_and_chat_together(bound, monkeypatch):
    renew = pairing._renew_chat_controllers

    def fail_after_rebind(*args):
        renew(*args)
        raise RuntimeError('interrupted renewal')

    monkeypatch.setattr(pairing, '_renew_chat_controllers', fail_after_rebind)
    with pytest.raises(RuntimeError, match='interrupted renewal'):
        rotate(bound)
    assert store.read('caller')['credential_hash'] == identity.digest('old')
    identity.validate(store.read('caller'))
    assert pairing.get_paired_client_by_session_hash(identity.digest('renewed')) is None


def test_repeated_renewals_keep_durable_binding_and_reject_old_principals(bound):
    assert rotate(bound)
    first = identity.principal('paired', bound[0]['id'], 'renewed')
    assert rotate(bound, 'newest')
    # Read fresh from persistence, with no live socket or in-memory principal.
    identity.validate(store.read('caller'))
    assert store.read('caller')['credential_hash'] == identity.digest('newest')
    with pytest.raises(PermissionError):
        identity.bind_session('caller', first)
    assert store.read('caller')['credential_hash'] == identity.digest('newest')


def test_rotation_never_updates_another_device_even_with_same_old_hash(bound):
    other = client('unrelated')
    forged = dict(bound[3], principal_id=other['id'], owner='paired:' + other['id'])
    chat('mismatched')
    # Deliberately seed an invalid persisted binding, not an authenticated claim.
    store.bind('mismatched', forged, new=True)
    assert rotate(bound)
    assert store.read('mismatched')['credential_hash'] == identity.digest('old')
    with pytest.raises(PermissionError):
        identity.validate(store.read('mismatched'))


def test_removed_controller_scope_is_still_refused(bound):
    with get_db() as conn:
        conn.cursor().execute('UPDATE paired_clients SET scopes=%s WHERE id=%s',
                              (json.dumps(['observer']), bound[0]['id']))
    assert rotate(bound)
    with pytest.raises(PermissionError):
        identity.validate(store.read('caller'))


@pytest.mark.parametrize('new', [False, True])
def test_rotation_between_validation_and_bind_cannot_store_stale_hash(bound, monkeypatch, new):
    validate = identity.validate

    def rotate_after_validation(value):
        validate(value)
        assert rotate(bound)

    monkeypatch.setattr(identity, 'validate', rotate_after_validation)
    sid = 'fresh' if new else 'caller'
    if new:
        chat(sid)
    with pytest.raises(PermissionError, match='paired controller credential'):
        identity.bind_session(sid, bound[3], new=new)
    assert store.read('fresh') is None
    assert store.read('caller')['credential_hash'] == identity.digest('renewed')


async def test_rotation_during_start_recheck_refuses_then_retry_succeeds(bound, monkeypatch):
    recheck = orch._recheck

    def rotate_before_recheck(cursor, snapshots, principals):
        assert rotate(bound)
        return recheck(cursor, snapshots, principals)

    args = {'title': 'Review', 'prompt': 'Review', 'request_id': 'racing-start'}
    monkeypatch.setattr(orch, '_recheck', rotate_before_recheck)
    failed = await invoke('start', args)
    assert failed.is_error and 'paired controller credential' in failed.text
    monkeypatch.setattr(orch, '_recheck', recheck)
    retried = await invoke('start', args)
    assert not retried.is_error, retried.text
    sid = json.loads(retried.text)['session_id']
    assert store.read(sid)['credential_hash'] == identity.digest('renewed')


def test_credential_recheck_takes_device_lock_before_reading_slot(bound):
    statements = []
    with get_db() as conn:
        cursor = conn.cursor()

        class RecordingCursor:
            def execute(self, sql, params):
                statements.append(sql)
                return cursor.execute(sql, params)

            def fetchone(self):
                return cursor.fetchone()

        store.lock_paired_credential(RecordingCursor(), bound[3])
    assert 'paired_clients' in statements[0] and 'FOR SHARE' in statements[0]
    assert 'session_token_hash' in statements[1]


def test_revoked_controller_has_short_socket_reason_and_detailed_tool_guidance(bound):
    assert pairing.revoke_paired_client(bound[0]['id'])
    with pytest.raises(PermissionError) as tool_error:
        identity.validate(bound[3])
    assert 'reopen/continue this chat' in str(tool_error.value)
    with pytest.raises(PermissionError) as socket_error:
        identity.revalidate(bound[3])
    reason = str(socket_error.value)
    assert 'revoked' in reason
    assert len(reason.encode('utf-8')) <= 123
    assert 'reopen/continue this chat' not in reason
