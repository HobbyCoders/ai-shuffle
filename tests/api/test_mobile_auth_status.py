"""The bundled mobile UI authenticates with a minted Bearer, not a worker cookie."""
import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.core import pairing
from app.db import database
from tests.api.test_managed_owner_admin import owner_app, bind_and_enroll

MOBILE_ORIGIN = 'https://appassets.androidplatform.net'


@pytest.mark.parametrize('purpose', ['web', 'native'])
@pytest.mark.parametrize('cookie', [None, 'shc_retired-session'])
@pytest.mark.parametrize('linked_by, admin', [
    ('managed:owner-test-install', True), ('pcl_direct-controller', False),
])
def test_minted_mobile_bearer_reports_same_identity_as_cookie(
    owner_app, monkeypatch, purpose, cookie, linked_by, admin,
):
    bind_and_enroll(monkeypatch, linked_by=linked_by)
    native = TestClient(owner_app)
    mint_headers = {'Authorization': 'Bearer aim_owner-test'}
    if purpose == 'web':
        reservation = native.post('/api/v1/mobile/web-session/reservations', headers=mint_headers)
        assert reservation.status_code == 200
        mint_headers['X-Shuffle-Web-Session-Reservation'] = reservation.json()['reservation']
    minted = native.post('/api/v1/mobile/web-session', json={'purpose': purpose}, headers=mint_headers)
    assert minted.status_code == 200
    token = minted.json()['session_token']
    assert token.startswith(pairing.PAIRED_SESSION_PREFIX)

    # Do not inherit TestClient's cookie jar from the native mint. The bundled
    # WebView fetches cross-origin with credentials: omit and this Bearer only.
    bundle = TestClient(owner_app, headers={'Origin': MOBILE_ORIGIN, 'Authorization': 'Bearer ' + token})
    if cookie:
        bundle.cookies.set('session', cookie)
    principal = bundle.get('/principal')
    assert principal.status_code == 200  # protected API already accepts it
    response = bundle.get('/api/v1/auth/status')
    assert response.status_code == 200
    status = response.json()
    assert status['authenticated'] is True
    assert status['paired_client'] is True
    assert status['is_admin'] is admin
    assert status['local_access'] is False
    assert status['auth_mode'] == 'account'
    assert status['username'] == 'Owner phone'

    browser = TestClient(owner_app)
    browser.cookies.set('session', token)
    assert status['preference_identity'] == browser.get('/api/v1/auth/status').json()['preference_identity']
    assert status['preference_identity'] is not None


@pytest.mark.parametrize('token', [None, 'shc_invalid', 'aim_owner-test', 'aih_integration'])
def test_missing_invalid_and_non_session_bearers_do_not_unlock_ui(owner_app, monkeypatch, token):
    bind_and_enroll(monkeypatch)
    database.create_api_user('integration', 'Integration', auth.hash_api_key('aih_integration'))
    headers = {'Origin': MOBILE_ORIGIN}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    response = TestClient(owner_app, headers=headers).get('/api/v1/auth/status')
    assert response.status_code == 200
    status = response.json()
    assert not status['authenticated']
    assert not status['is_admin']
    assert not status['paired_client']
    assert status['preference_identity'] is None
