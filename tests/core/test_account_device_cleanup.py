"""One-time cleanup of the duplicate phone entries left by weekly re-sign-ins."""
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.auth import hash_api_key
from app.core import account_device_cleanup as cleanup
from app.core import account_hub, encryption, pairing
from app.core.models import DeviceInfo
from app.db import database
from app.db import mobile_hub as store

HUB = 'https://hub.example.com'
SERVER = '11111111-1111-1111-1111-111111111111'
ACCOUNT = '22222222-2222-2222-2222-222222222222'
T0 = datetime(2026, 9, 29, 20, 35, tzinfo=timezone.utc)


def row(n, created, seen=None, name='samsung SM-F956U', decision='approved', **extra):
    return {'device_id': f'dev-{n}', 'paired_client_id': f'pc-{n}', 'hub_device_id': f'hub-{n}',
            'server_id': SERVER, 'account_id': ACCOUNT, 'device_name': name, 'platform': 'android',
            'created_at': created, 'device_seen': created, 'client_seen': seen, 'decision': decision, **extra}


def ids(rows):
    return sorted(r['device_id'] for r in rows)


def test_owner_rows_keep_the_phone_in_use_and_revoke_weekly_copies():
    # The real shape on the owner's worker: one identity per weekly sign-in,
    # each used until the next sign-in, the newest in use now.
    days = [0, 7, 15, 22, 29]
    rows = [row(i, T0 + timedelta(days=d), T0 + timedelta(days=(days[i + 1] if i + 1 < len(days) else 30), minutes=-4))
            for i, d in enumerate(days)]
    rows.append(row('emulator', T0, T0, name='Google sdk_gphone64_x86_64'))
    assert ids(cleanup.stale_entries(rows)) == ['dev-0', 'dev-1', 'dev-2', 'dev-3']


def test_unused_newer_enrollment_and_concurrent_install_are_kept():
    rows = [row('old', T0, T0 + timedelta(days=6)),
            # Used after the keeper was enrolled: a second install with the same name.
            row('concurrent', T0 + timedelta(days=1), T0 + timedelta(days=9)),
            row('keeper', T0 + timedelta(days=7), T0 + timedelta(days=10)),
            # Enrolled after the keeper and not used yet: when in doubt, keep.
            row('fresh', T0 + timedelta(days=9, hours=1))]
    assert ids(cleanup.stale_entries(rows)) == ['dev-old']


def test_never_used_copies_keep_the_newest_enrollment():
    rows = [row(i, T0 + timedelta(days=7 * i)) for i in range(3)]
    assert ids(cleanup.stale_entries(rows)) == ['dev-0', 'dev-1']


@pytest.mark.parametrize('variant', ['other-account', 'other-name', 'denied', 'unknown-time'])
def test_only_same_account_same_name_live_entries_are_grouped(variant):
    newer = row('new', T0 + timedelta(days=7), T0 + timedelta(days=8))
    older = row('old', T0, T0 + timedelta(days=1))
    if variant == 'other-account':
        older['account_id'] = '55555555-5555-5555-5555-555555555555'
    elif variant == 'other-name':
        older['device_name'] = 'Pixel 9'
    elif variant == 'denied':
        older['decision'] = 'denied'
    else:
        older['created_at'] = None
    assert cleanup.stale_entries([older, newer]) == []


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(account_hub.settings, 'mobile_account_hub_url', HUB)
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    store.bind(HUB, SERVER, encryption.encrypt_value('a' * 64), 'controller')
    store.activate()


def enroll(n, enrolled_at, proof_hash=None):
    hub_device = f'3333333{n}-3333-3333-3333-333333333333'
    identity = {'account_id': ACCOUNT, 'device_id': hub_device, 'device_name': 'samsung SM-F956U', 'server_id': SERVER}
    info = DeviceInfo(device_id=hub_device, device_name='x', platform='android', capabilities={})
    device_id = store.enroll(identity, info, hash_api_key(f'aim_{n}'), datetime.now(timezone.utc) + timedelta(days=90),
                             list(pairing.DEFAULT_MOBILE_SCOPES), scoped=True, install_proof_hash=proof_hash)
    store.confirm_device(store.ownership(device_id), True)
    with database.get_db() as conn:
        conn.cursor().execute('UPDATE mobile_devices SET created_at=%s, last_seen=%s WHERE device_id=%s',
                              (enrolled_at.isoformat(), (enrolled_at + timedelta(days=6)).isoformat(), device_id))
    return store.ownership(device_id)


def test_run_once_revokes_on_the_account_then_locally_and_only_once(worker, monkeypatch):
    old, current = enroll(1, T0), enroll(2, T0 + timedelta(days=7))
    calls = []
    monkeypatch.setattr(account_hub, 'call', lambda b, method, suffix, body=None: calls.append((method, suffix)) or {'ok': True})
    assert cleanup.run_once() == 1
    assert calls == [('DELETE', 'account/devices/' + old['hub_device_id'])]
    assert database.get_paired_client(old['paired_client_id'])['revoked_at'] is not None
    assert database.get_paired_client(current['paired_client_id'])['revoked_at'] is None
    assert cleanup.run_once() is None  # Done marker: never runs again.


def test_hub_failure_keeps_local_entry_and_retries_next_start(worker, monkeypatch):
    old, _ = enroll(1, T0), enroll(2, T0 + timedelta(days=7))
    def unavailable(*a, **k):
        raise HTTPException(503, 'Account hub authorization unavailable')
    monkeypatch.setattr(account_hub, 'call', unavailable)
    assert cleanup.run_once() == 0
    assert database.get_paired_client(old['paired_client_id'])['revoked_at'] is None
    monkeypatch.setattr(account_hub, 'call', lambda *a, **k: {'ok': True})
    assert cleanup.run_once() == 1


@pytest.mark.parametrize('activity', ['authenticate', 'renew', 'renew-with-same-activity'])
def test_cleanup_rechecks_candidate_after_an_earlier_hub_call(worker, monkeypatch, activity):
    from starlette.requests import Request

    from app.api import mobile

    older = [enroll(1, T0, proof_hash='proof'), enroll(2, T0 + timedelta(days=7), proof_hash='proof')]
    keeper = enroll(3, T0 + timedelta(days=14))
    candidates = cleanup.stale_entries(store.live_account_entries(SERVER))
    assert len(candidates) == 2
    refreshed = candidates[1]
    calls = []

    def revoke_and_use_other_phone(binding, method, suffix, body=None):
        calls.append((method, suffix))
        assert suffix.endswith(candidates[0]['hub_device_id'])
        # The second candidate becomes active while the first hub call is in
        # flight, after the cleanup took its original snapshot. Use real auth
        # and enrollment producers rather than mutating that snapshot.
        if activity == 'authenticate':
            number = next(i for i, owner in enumerate(older, 1) if owner['device_id'] == refreshed['device_id'])
            monkeypatch.setattr(account_hub, 'require_active', lambda owner: None)
            request = Request({'type': 'http', 'headers': [(b'authorization', f'Bearer aim_{number}'.encode())]})
            assert mobile.get_device_from_api_key(request)['device_id'] == refreshed['device_id']
        else:
            identity = {'account_id': ACCOUNT, 'device_id': refreshed['hub_device_id'],
                        'device_name': refreshed['device_name'], 'server_id': SERVER}
            info = DeviceInfo(device_id=refreshed['hub_device_id'], device_name='x', platform='android', capabilities={})
            store.enroll(identity, info, hash_api_key('aim_refreshed'),
                         datetime.now(UTC) + timedelta(days=90),
                         list(pairing.DEFAULT_MOBILE_SCOPES), scoped=True, install_proof_hash='proof')
            if activity == 'renew-with-same-activity':
                # Even if activity timestamps coincide, a changed credential
                # generation must not be revoked from the old snapshot.
                with database.get_db() as conn:
                    conn.cursor().execute('UPDATE mobile_devices SET last_seen=%s WHERE device_id=%s',
                                          (refreshed['device_seen'], refreshed['device_id']))
                assert database.get_mobile_device(refreshed['device_id'])['id'] == refreshed['mobile_record_id']
        return {'ok': True}

    monkeypatch.setattr(account_hub, 'call', revoke_and_use_other_phone)
    assert cleanup.run_once() == 1
    assert calls == [('DELETE', 'account/devices/' + candidates[0]['hub_device_id'])]
    assert database.get_paired_client(candidates[0]['paired_client_id'])['revoked_at'] is not None
    assert database.get_paired_client(refreshed['paired_client_id'])['revoked_at'] is None
    assert database.get_paired_client(keeper['paired_client_id'])['revoked_at'] is None
