"""The worker's lease on hub tunnel calls, and the hub's tunnel_limit refusal.

The account hub caps managed tunnels per account (android/hub
src/tunnel-capacity.ts): one for an unpaid account, three for a licensed one.
A worker proves its license by sending its lease in X-Shuffle-Lease on the
activate and connector calls; a refused worker gets 409 tunnel_limit and keeps
running locally, with the reason shown in the remote-access UI.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import account_hub, license as licensing, managed_connection as managed

LEASE = 'eyJmb3JtYXQiOiJzaHVmZmxlLWxlYXNlLXYxIn0'
REAL_LEASE_HEADER = licensing.lease_header
BINDING = {'hub_url': 'https://hub.example', 'server_id': 'worker', 'encrypted_secret': 'a', 'linked_by': 'x', 'active': 0}


class _Connection:
    replies = []
    sent = []

    def __init__(self, host, *args, **kwargs):
        self.host = host

    def request(self, method, path, body=None, headers=None):
        type(self).sent.append({'host': self.host, 'method': method, 'path': path, 'headers': dict(headers or {})})

    def getresponse(self):
        self.status, self._headers, self._body = type(self).replies.pop(0)
        return self

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, limit):
        return json.dumps(self._body).encode()[:limit]

    def close(self):
        pass


@pytest.fixture
def hub(monkeypatch):
    _Connection.replies, _Connection.sent = [], []
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', _Connection)
    monkeypatch.setattr(account_hub, 'trusted_origin', lambda value: value)
    monkeypatch.setattr(account_hub, 'identifier', lambda value: value)
    monkeypatch.setattr(account_hub, 'secret_for', lambda _: 'test-secret')
    monkeypatch.setattr(account_hub, '_paused_until', {})
    monkeypatch.setattr(licensing, 'lease_header', lambda: LEASE)
    return _Connection


def refusal(limit=1, used=1, licensed=False):
    return (409, {'Retry-After': '3600'}, {
        'error': 'This account already uses its one remote connection.', 'code': 'tunnel_limit',
        'limit': limit, 'used': used, 'licensed': licensed})


@pytest.mark.parametrize('suffix', ['activate', 'connector'])
def test_tunnel_calls_carry_the_lease(hub, suffix):
    hub.replies = [(200, {}, {'ok': True})]
    account_hub.call(BINDING, 'POST', suffix, {'remote_port': 8001})
    sent = hub.sent[0]
    assert sent['headers'][licensing.LEASE_HEADER] == LEASE
    assert sent['path'] == f'/v1/servers/worker/{suffix}'
    assert sent['host'] == 'hub.example'


@pytest.mark.parametrize('suffix', ['status', 'account', 'network/pending', 'devices/x', 'connection'])
def test_other_hub_calls_do_not_carry_the_lease(hub, suffix):
    hub.replies = [(200, {}, {'active': True})]
    account_hub.call(BINDING, 'GET', suffix)
    assert licensing.LEASE_HEADER not in hub.sent[0]['headers']


@pytest.mark.parametrize('lease', [None, '', 'has space', 'a\r\nX-Other: 1', 'x' * 4097])
def test_no_or_malformed_lease_sends_no_header(hub, monkeypatch, lease):
    monkeypatch.setattr(licensing, 'lease_header', lambda: lease)
    hub.replies = [(200, {}, {'ok': True})]
    account_hub.call(BINDING, 'POST', 'activate', {'remote_port': 8001})
    assert licensing.LEASE_HEADER not in hub.sent[0]['headers']


def test_a_broken_license_store_never_blocks_the_hub_call(hub, monkeypatch):
    def broken():
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(licensing, 'lease_header', broken)
    hub.replies = [(200, {}, {'ok': True})]
    assert account_hub.call(BINDING, 'POST', 'connector', {'remote_port': 8001}) == {'ok': True}


def test_real_lease_header_reaches_the_hub_call(hub, monkeypatch):
    """Not stubbed: whatever license.lease_header() returns is what the hub gets."""
    monkeypatch.setattr(licensing, 'lease_header', REAL_LEASE_HEADER)
    monkeypatch.setattr(licensing, 'beta_mode', lambda: False)
    monkeypatch.setattr(licensing, '_load_state', lambda cached=False: {'lease': {'format': 'shuffle-lease-v1', 'payload': 'e30='}})
    hub.replies = [(200, {}, {'ok': True})]
    account_hub.call(BINDING, 'POST', 'activate', {'remote_port': 8001})
    header = hub.sent[0]['headers'][licensing.LEASE_HEADER]
    assert header and header == REAL_LEASE_HEADER()


def test_tunnel_limit_refusal_is_parsed(hub):
    hub.replies = [refusal(limit=3, used=3, licensed=True)]
    with pytest.raises(account_hub.TunnelLimitError) as refused:
        account_hub.call(BINDING, 'POST', 'activate', {'remote_port': 8001})
    exc = refused.value
    assert (exc.limit, exc.used, exc.licensed, exc.hub_status, exc.retry_after) == (3, 3, True, 409, 3600)
    assert account_hub.tunnel_limit(exc) is exc
    # Still a HubResponseError, so every existing caller treats it as "unavailable".
    assert isinstance(exc, account_hub.HubResponseError) and exc.status_code == 503
    assert 'remote connection' not in str(exc.detail), 'the hub text is not echoed'


@pytest.mark.parametrize('body', [
    {'code': 'other'}, {'code': 'tunnel_limit', 'limit': 'x', 'used': 1}, {'code': 'tunnel_limit', 'limit': 1},
    {'code': 'tunnel_limit', 'limit': True, 'used': 1}, ['tunnel_limit'],
])
def test_other_conflicts_stay_plain_hub_errors(hub, body):
    hub.replies = [(409, {}, body)]
    with pytest.raises(account_hub.HubResponseError) as failed:
        account_hub.call(BINDING, 'POST', 'connector', {'remote_port': 8001})
    assert account_hub.tunnel_limit(failed.value) is None and failed.value.hub_status == 409


def test_a_tunnel_limit_body_on_another_route_is_ignored(hub):
    hub.replies = [refusal()]
    with pytest.raises(account_hub.HubResponseError) as failed:
        account_hub.call(BINDING, 'GET', 'status')
    assert account_hub.tunnel_limit(failed.value) is None


# ---------------------------------------------------------------------------
# The supervisor and the remote-access status
# ---------------------------------------------------------------------------

@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setattr(managed, '_generation', 0)
    monkeypatch.setattr(managed, '_wake', 0)
    monkeypatch.setattr(managed, '_process', None)
    monkeypatch.setattr(managed, '_state', 'disconnected')
    monkeypatch.setattr(managed, '_error', None)
    monkeypatch.setattr(managed, '_url', None)
    monkeypatch.setattr(managed, '_refusal', None)
    monkeypatch.setattr(managed.cloudflared, 'find_executable', lambda: '/usr/local/bin/cloudflared')
    monkeypatch.setattr(managed.encryption, 'is_encryption_ready', lambda: True)
    monkeypatch.setattr(managed, '_pending', lambda: None)
    monkeypatch.setattr(managed.mobile_hub, 'get_binding', lambda: dict(BINDING, linked_by=managed._owner()))
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(managed, 'run_sync', inline)
    monkeypatch.setattr(managed.random, 'uniform', lambda low, high: high)
    clock = [0.0]
    monkeypatch.setattr(managed, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=managed.time.time))
    return clock


def test_status_reports_the_refusal_for_the_ui(supervisor):
    managed._set_refusal(account_hub.TunnelLimitError(1, 1, False, 3600))
    result = managed.status()
    assert result['status'] == 'limited'
    refusal_info = result['refusal']
    assert {k: refusal_info[k] for k in ('code', 'limit', 'used', 'licensed')} == {
        'code': 'tunnel_limit', 'limit': 1, 'used': 1, 'licensed': False}
    assert 'Settings > License' in refusal_info['message']
    assert 'Local use is not affected' in refusal_info['message']
    managed._set_refusal(account_hub.TunnelLimitError(3, 3, True))
    message = managed.status()['refusal']['message']
    assert 'all 3 remote connections its license covers' in message and 'Settings > License' not in message
    managed._set_refusal(None)
    assert managed.status()['refusal'] is None and managed.status()['status'] == 'connecting'


async def test_refused_worker_backs_off_an_hour_and_shows_why(supervisor, monkeypatch):
    clock = supervisor
    credentials = Mock(side_effect=account_hub.TunnelLimitError(1, 1, False, 3600))
    monkeypatch.setattr(managed, '_credentials', credentials)
    seen = []
    async def tick(delay):
        clock[0] += delay
        seen.append(managed.status())
        if clock[0] >= 7200:
            raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    with pytest.raises(asyncio.CancelledError):
        await managed.supervise()
    # Retry-After 3600 is honoured: two hours is at most three hub calls.
    assert credentials.call_count <= 3
    assert seen[0]['status'] == 'limited' and seen[0]['refusal']['limit'] == 1
    assert 'Remote access is off' in seen[0]['error']


async def test_a_license_activation_cuts_the_backoff_short(supervisor, monkeypatch):
    clock = supervisor
    process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock(), kill=Mock())
    answers = [account_hub.TunnelLimitError(1, 1, False, 3600), ('token-' * 8, 'https://worker.example.com')]
    def credentials():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(managed, '_credentials', credentials)
    monkeypatch.setattr(managed.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    monkeypatch.setattr(managed, '_linked_binding', Mock(return_value=BINDING))
    monkeypatch.setattr(managed, '_reachable', Mock(return_value=True))
    ticks = []
    async def tick(delay):
        ticks.append(delay)
        clock[0] += delay
        if len(ticks) == 2:
            managed.retry_soon()  # Settings > License: a key was just activated.
        if len(ticks) >= 6:
            raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    with pytest.raises(asyncio.CancelledError):
        await managed.supervise()
    assert not answers, 'the worker asked the hub again right after the activation'
    assert clock[0] < 3600
    assert managed._refusal is None


async def test_license_activation_nudges_remote_access(monkeypatch):
    nudged = Mock()
    monkeypatch.setattr(managed, 'retry_soon', nudged)
    licensing._nudge_remote_access()
    nudged.assert_called_once()
