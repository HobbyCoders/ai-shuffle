"""Display caching must not supply authorization evidence."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.core import account_hub


@pytest.fixture
def display(monkeypatch):
    binding = dict(hub_url='https://hub.example', server_id='worker',
                   linked_by='owner', encrypted_secret='encrypted', active=True)
    now = [0.0]
    monkeypatch.setattr(account_hub.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(account_hub.store, 'get_binding', lambda: binding.copy())
    hub = Mock(return_value={'account': {'id': 'account', 'username': 'first', 'profile_complete': True},
                             'servers': [{'secret': 'not-for-badge'}]})
    monkeypatch.setattr(account_hub, 'call', hub)
    account_hub.reset_display_cache()
    yield binding, now, hub
    account_hub.reset_display_cache()


def test_display_ttl_and_inventory_is_not_retained(display):
    binding, now, hub = display
    assert set(account_hub.account_display(binding)) == {'account'}
    account_hub.account_display(binding)
    assert hub.call_count == 1
    now[0] = account_hub._DISPLAY_TTL
    account_hub.account_display(binding)
    assert hub.call_count == 2


@pytest.mark.parametrize('status', [429, 503])
def test_display_failure_backoff_never_serves_expired_name(display, status):
    binding, now, hub = display
    account_hub.account_display(binding)
    now[0] = account_hub._DISPLAY_TTL
    hub.side_effect = HTTPException(status, 'unavailable')
    for _ in range(3):
        with pytest.raises(HTTPException) as error:
            account_hub.account_display(binding)
        assert error.value.status_code == status
    assert hub.call_count == 2
    now[0] += account_hub._DISPLAY_RETRY
    with pytest.raises(HTTPException):
        account_hub.account_display(binding)
    assert hub.call_count == 3


def test_display_coalesces_concurrent_requests(display):
    binding, _, hub = display
    entered, release = Event(), Event()
    value = hub.return_value
    def fetch(*args):
        entered.set()
        assert release.wait(2)
        return value
    hub.side_effect = fetch
    with ThreadPoolExecutor(max_workers=8) as pool:
        requests = [pool.submit(account_hub.account_display, binding) for _ in range(8)]
        assert entered.wait(2)
        release.set()
        assert all(request.result(timeout=2)['account']['username'] == 'first' for request in requests)
    assert hub.call_count == 1


def test_stuck_display_flight_expires_without_overwriting_newer_result(display):
    binding, now, hub = display
    entered, release = Event(), Event()
    def fetch(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(2)
            return {'account': {'username': 'old'}}
        return {'account': {'username': 'new'}}
    hub.side_effect = fetch
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_request = pool.submit(account_hub.account_display, binding)
        assert entered.wait(2)
        now[0] = 6.0
        try:
            assert account_hub.account_display(binding)['account']['username'] == 'new'
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            old_request.result(timeout=2)
        assert error.value.status_code == 409
    assert account_hub.account_display(binding)['account']['username'] == 'new'
    assert hub.call_count == 2


@pytest.mark.parametrize('change', ['rename', 'disconnect', 'replacement'])
def test_display_rejects_inflight_response_after_identity_change(display, change):
    binding, _, hub = display
    original = binding.copy()
    def fetch(*args):
        if change == 'rename':
            account_hub.reset_display_cache()
        elif change == 'disconnect':
            binding['active'] = False
        else:
            binding['encrypted_secret'] = 'replacement'
        return hub.return_value
    hub.side_effect = fetch
    with pytest.raises(HTTPException) as error:
        account_hub.account_display(original)
    assert error.value.status_code == 409
    hub.side_effect = None
    if change != 'disconnect':
        hub.return_value['account']['username'] = 'new'
        assert account_hub.account_display(binding)['account']['username'] == 'new'
        assert hub.call_count == 2


@pytest.mark.parametrize('method,expected', [
    ('GET', 'Account hub is rate limiting this worker. Try again shortly.'),
    ('PATCH', 'Too many username changes. Try again in a few minutes.'),
])
def test_profile_rate_limit_message_distinguishes_read_from_rename(monkeypatch, method, expected):
    conn = Mock()
    conn.getresponse.return_value.status = 429
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', Mock(return_value=conn))
    monkeypatch.setattr(account_hub, 'trusted_origin', lambda value: value)
    with pytest.raises(HTTPException) as error:
        account_hub.call({'hub_url': 'https://hub.example',
                          'server_id': '11111111-1111-1111-1111-111111111111'},
                         method, 'account', credential='test')
    assert error.value.status_code == 429
    assert error.value.detail == expected
    conn.close.assert_called_once()


def test_display_does_not_populate_authorization_cache(display):
    binding, _, _ = display
    before = dict(account_hub._verdicts)
    account_hub.account_display(binding)
    assert account_hub._verdicts == before
