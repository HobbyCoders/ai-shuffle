"""Real routes and isolated DB: cancelled mints cannot replace a newer cookie."""
from datetime import datetime, timedelta, timezone
import re

import pytest
from fastapi.testclient import TestClient

from app.api import auth, mobile
from app.core import account_hub, pairing
from app.db import database, pairing as store
from tests.api.test_managed_owner_admin import owner_app, bind_and_enroll, web_session, DEVICE

KEY = 'aim_owner-test'
HEADER = 'X-Shuffle-Web-Session-Reservation'


def reserve(client, key=KEY):
    response = client.post('/api/v1/mobile/web-session/reservations', json={},
                           headers={'Authorization': 'Bearer ' + key})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'set-cookie' not in response.headers
    data = response.json()
    assert re.fullmatch(r'wsm_[A-Za-z0-9_-]{43}', data['reservation'])
    assert data['expires_in'] == 120
    assert datetime.now(timezone.utc) < datetime.fromisoformat(data['expires_at']) <= datetime.now(timezone.utc) + timedelta(seconds=120)
    return data['reservation']


def commit(client, reservation, *, key=KEY, purpose='web'):
    return client.post('/api/v1/mobile/web-session', json={'purpose': purpose},
                       headers={'Authorization': 'Bearer ' + key, HEADER: reservation})


def assert_cookie_works(app, token):
    browser = TestClient(app, headers={'Origin': 'http://testserver'})
    browser.cookies.set('session', token)
    assert browser.get('/principal').status_code == 200


def test_latest_reservation_wins_and_replay_cannot_rotate(owner_app, monkeypatch, test_db):
    bind_and_enroll(monkeypatch)
    browser, original = web_session(owner_app)
    native = TestClient(owner_app).post('/api/v1/mobile/web-session', json={'purpose': 'native'},
                                      headers={'Authorization': 'Bearer ' + KEY}).json()['session_token']
    before = database.get_mobile_device(DEVICE)
    first = reserve(browser)
    second = reserve(browser)
    rows = test_db.execute('SELECT * FROM paired_client_web_reservations').fetchall()
    assert len(rows) == 1 and rows[0]['reservation_hash'] == auth.hash_api_key(second)
    assert first not in str(dict(rows[0])) and second not in str(dict(rows[0]))
    assert_cookie_works(owner_app, original)
    assert auth.get_paired_client_from_token(native)
    stale = commit(browser, first)
    assert stale.status_code == 409 and 'set-cookie' not in stale.headers
    winner = commit(browser, second)
    assert winner.status_code == 200 and winner.headers['cache-control'] == 'no-store'
    current = winner.json()['session_token']
    replay = commit(browser, second)
    assert replay.status_code == 409 and 'set-cookie' not in replay.headers
    assert_cookie_works(owner_app, current)
    assert auth.get_paired_client_from_token(original) is None
    assert auth.get_paired_client_from_token(native)
    assert database.get_mobile_device(DEVICE)['api_key_hash'] == before['api_key_hash']
    assert test_db.execute('SELECT * FROM paired_client_web_reservations').fetchall() == []


def test_late_commit_cannot_replace_newer_success(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    browser, _ = web_session(owner_app)
    first = reserve(browser)
    original = database.set_paired_client_session
    winner = []

    def delayed(*args, **kwargs):
        if kwargs.get('web_reservation_hash') == auth.hash_api_key(first):
            newer = reserve(browser)
            result = commit(browser, newer)
            assert result.status_code == 200
            winner.append(result.json()['session_token'])
        return original(*args, **kwargs)

    monkeypatch.setattr(database, 'set_paired_client_session', delayed)
    result = commit(browser, first)
    assert result.status_code == 409 and 'set-cookie' not in result.headers
    assert len(winner) == 1
    assert_cookie_works(owner_app, winner[0])


def test_cancelled_reserve_finishing_after_newer_commit_keeps_cookie(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    browser, _ = web_session(owner_app)
    original = store.reserve_paired_client_web_session
    winner = []
    nested = False

    def delayed(*args, **kwargs):
        nonlocal nested
        if not nested:
            nested = True
            newer = reserve(browser)
            result = commit(browser, newer)
            assert result.status_code == 200
            winner.append(result.json()['session_token'])
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'reserve_paired_client_web_session', delayed)
    reserve(browser)  # Its cancelled caller never commits this late reservation.
    assert len(winner) == 1
    assert_cookie_works(owner_app, winner[0])


@pytest.mark.parametrize('values,purpose', [
    ([''], 'web'), (['garbage'], 'web'), (['wsm_' + 'a' * 42], 'web'),
    (['wsm_' + 'a' * 44], 'web'), (['wsm_' + 'a' * 43, 'wsm_' + 'b' * 43], 'web'),
    (['wsm_' + 'a' * 43], 'native'),
])
def test_invalid_reservation_header_never_falls_back_to_legacy(owner_app, monkeypatch, values, purpose):
    bind_and_enroll(monkeypatch)
    browser, original = web_session(owner_app)
    pending = reserve(browser)
    headers = [('Authorization', 'Bearer ' + KEY)] + [(HEADER, value) for value in values]
    result = browser.post('/api/v1/mobile/web-session', json={'purpose': purpose}, headers=headers)
    assert result.status_code == 422 and 'set-cookie' not in result.headers
    assert_cookie_works(owner_app, original)
    assert commit(browser, pending).status_code == 200


def test_expired_and_cross_device_reservations_leave_current_cookie(owner_app, monkeypatch, test_db):
    bind_and_enroll(monkeypatch)
    browser, original = web_session(owner_app)
    pending = reserve(browser)
    database.create_mobile_device('other-phone', 'Other phone', 'android', auth.hash_api_key('aim_other'), {},
        api_key_expires_at=datetime.now(timezone.utc) + timedelta(days=1), scopes=['controller', 'web:session'])
    other = reserve(browser, 'aim_other')
    result = commit(browser, pending, key='aim_other')
    assert result.status_code == 409 and 'set-cookie' not in result.headers
    assert commit(browser, other, key='aim_other').status_code == 200
    test_db.execute('UPDATE paired_client_web_reservations SET expires_at=? WHERE reservation_hash=?',
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), auth.hash_api_key(pending)))
    test_db.commit()
    result = commit(browser, pending)
    assert result.status_code == 409 and 'set-cookie' not in result.headers
    assert_cookie_works(owner_app, original)


def test_native_and_stale_logout_keep_reservation_but_legacy_web_retires_it(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    browser, original = web_session(owner_app)
    client_id = database.get_mobile_device(DEVICE)['paired_client_id']
    pending = reserve(browser)
    assert not database.clear_paired_client_session(client_id, 'unrelated-stale-token')
    assert browser.post('/api/v1/mobile/web-session', json={'purpose': 'native'},
                        headers={'Authorization': 'Bearer ' + KEY}).status_code == 200
    assert commit(browser, pending).status_code == 200
    pending = reserve(browser)
    _, legacy = web_session(owner_app)
    assert commit(browser, pending).status_code == 409
    assert_cookie_works(owner_app, legacy)
    pending = reserve(browser)
    assert database.clear_paired_client_session(client_id, pairing.hash_controller_session(legacy))
    assert commit(browser, pending).status_code == 409


def test_failed_commit_rolls_back_consumption_and_preserves_current_session(owner_app, monkeypatch, test_db):
    bind_and_enroll(monkeypatch)
    browser, original = web_session(owner_app)
    pending = reserve(browser)
    test_db.execute('''CREATE TRIGGER reject_web_rotation BEFORE UPDATE OF session_token_hash ON paired_clients
        BEGIN SELECT RAISE(ABORT, 'controlled write failure'); END''')
    test_db.commit()
    failing = TestClient(owner_app, raise_server_exceptions=False)
    response = commit(failing, pending)
    assert response.status_code == 500 and 'set-cookie' not in response.headers
    assert_cookie_works(owner_app, original)
    assert test_db.execute('SELECT reservation_hash FROM paired_client_web_reservations').fetchone()[0] == auth.hash_api_key(pending)
    test_db.execute('DROP TRIGGER reject_web_rotation')
    test_db.commit()
    assert commit(browser, pending).status_code == 200


@pytest.mark.parametrize('change', ['revoke', 'expire', 'scope', 'worker', 'account'])
def test_reservation_never_bypasses_changed_authority(owner_app, monkeypatch, test_db, change):
    bind_and_enroll(monkeypatch)
    browser, _ = web_session(owner_app)
    pending = reserve(browser)
    if change == 'revoke':
        database.revoke_paired_client(database.get_mobile_device(DEVICE)['paired_client_id'])
    elif change == 'expire':
        test_db.execute('UPDATE mobile_devices SET api_key_expires_at=?', ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
        test_db.commit()
    elif change == 'scope':
        test_db.execute('UPDATE mobile_devices SET scopes=?', ('["controller"]',))
        test_db.commit()
    elif change == 'worker':
        monkeypatch.setattr(account_hub.settings, 'instance_id', 'another-worker')
    else:
        monkeypatch.setattr(account_hub, 'call', lambda *a, **k: {'active': True, 'account_id': 'another-account', 'device_id': DEVICE})
    for path, headers in [('/api/v1/mobile/web-session/reservations', {}),
                          ('/api/v1/mobile/web-session', {HEADER: pending})]:
        result = browser.post(path, json={}, headers={'Authorization': 'Bearer ' + KEY, **headers})
        assert result.status_code in (401, 403)
        assert 'set-cookie' not in result.headers
