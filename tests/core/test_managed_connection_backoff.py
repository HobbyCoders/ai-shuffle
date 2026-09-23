"""Hub failures must not take down a running connector or turn into a retry storm."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import account_hub, managed_connection as managed


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(managed, '_generation', 0)
    monkeypatch.setattr(managed, '_process', None)
    monkeypatch.setattr(managed, '_state', 'disconnected')
    monkeypatch.setattr(managed, '_error', None)
    monkeypatch.setattr(managed, '_url', 'https://worker.example.com')
    monkeypatch.setattr(account_hub, '_paused_until', {})
    monkeypatch.setattr(managed.cloudflared, 'find_executable', lambda: '/usr/local/bin/cloudflared')


def hub_error(status, retry_after=None):
    return account_hub.HubResponseError('Account hub authorization unavailable', status, retry_after)


def test_backoff_grows_with_jitter_caps_at_ten_minutes_and_honours_retry_after(monkeypatch):
    monkeypatch.setattr(managed.random, 'uniform', lambda low, high: high)
    assert [managed._backoff(n) for n in (1, 2, 3, 4, 5, 6, 7, 20)] == [15, 30, 60, 120, 240, 480, 600, 600]
    monkeypatch.setattr(managed.random, 'uniform', lambda low, high: low)
    assert managed._backoff(1) == 7.5
    # Retry-After is a floor, even above the cap (a kill switch asks for an hour).
    assert managed._backoff(1, 60) == 60
    assert managed._backoff(3, 3600) == 3600


async def run_supervisor(monkeypatch, check, *, ticks, spawn_process=True):
    """Start with a running connector and drive the loop on a fake clock."""
    process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock(), kill=Mock())
    monkeypatch.setattr(managed, '_process', process)
    clock = [1000.0]
    monkeypatch.setattr(managed, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=managed.time.time))
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(managed, 'run_sync', inline)
    monkeypatch.setattr(managed, '_check_authorized', check)
    monkeypatch.setattr(managed, '_linked_binding', Mock(return_value={'server_id': 'worker'}))
    monkeypatch.setattr(managed, '_reachable', Mock(return_value=True))
    credentials = Mock(return_value=('private-token' * 8, 'https://worker.example.com'))
    monkeypatch.setattr(managed, '_credentials', credentials)
    monkeypatch.setattr(managed.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    monkeypatch.setattr(managed.random, 'uniform', lambda low, high: high)
    sleeps = []
    # supervise() stops the connector on exit; count stops while it runs.
    stops_while_running = [0]
    seen = {}
    async def tick(delay):
        sleeps.append(delay)
        stops_while_running[0] = process.terminate.call_count
        seen.update(state=managed._state, error=managed._error)
        clock[0] += delay
        if len(sleeps) >= ticks:
            raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    with pytest.raises(asyncio.CancelledError):
        await managed.supervise()
    return stops_while_running[0], credentials, sleeps, seen


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [500, 502, 503, 429, None])
async def test_running_connector_survives_hub_outage_and_rate_limits(monkeypatch, status):
    check = Mock(side_effect=hub_error(status))
    stops, credentials, _, seen = await run_supervisor(monkeypatch, check, ticks=40)
    assert stops == 0
    credentials.assert_not_called()
    assert seen['state'] == 'connected'
    assert seen['error']
    # 40 half-minute ticks is 20 minutes: backoff (15, 30, 60, 120, 240, 480...)
    # keeps hub checks far below one per tick.
    assert check.call_count <= 7


@pytest.mark.asyncio
async def test_kill_switch_retry_after_holds_hub_checks_for_at_least_that_long(monkeypatch):
    check = Mock(side_effect=hub_error(503, 3600))
    stops, _, sleeps, seen = await run_supervisor(monkeypatch, check, ticks=100)
    assert stops == 0
    assert sum(sleeps) >= 3000
    assert check.call_count == 1
    assert 'paused' in seen['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
async def test_explicit_revocation_stops_the_connector(monkeypatch, status):
    check = Mock(side_effect=hub_error(status))
    stops, _, sleeps, seen = await run_supervisor(monkeypatch, check, ticks=1)
    assert stops == 1
    assert seen['state'] == 'reconnecting'


@pytest.mark.asyncio
async def test_failed_connector_refresh_backs_off_instead_of_every_fifteen_seconds(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(managed, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=managed.time.time))
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(managed, 'run_sync', inline)
    credentials = Mock(side_effect=hub_error(502))
    monkeypatch.setattr(managed, '_credentials', credentials)
    monkeypatch.setattr(managed.random, 'uniform', lambda low, high: high)
    sleeps = []
    async def tick(delay):
        sleeps.append(delay)
        clock[0] += delay
        if clock[0] >= 3600:
            raise asyncio.CancelledError()
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    with pytest.raises(asyncio.CancelledError):
        await managed.supervise()
    # One hour at a fixed 15 s would be 240 connector calls.
    assert credentials.call_count <= 12
    assert max(sleeps) <= 600


@pytest.mark.asyncio
async def test_signout_cuts_a_long_backoff_short(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(managed, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=managed.time.time))
    calls = []
    async def tick(delay):
        calls.append(delay)
        clock[0] += delay
        if len(calls) == 2:
            managed._generation += 1
        assert len(calls) < 10
    monkeypatch.setattr(managed.asyncio, 'sleep', tick)
    await managed._sleep(3600, managed._generation)
    assert sum(calls) < 3600


class _Connection:
    replies = []
    requests = 0

    def __init__(self, *args, **kwargs):
        pass

    def request(self, *args, **kwargs):
        type(self).requests += 1

    def getresponse(self):
        self.status, self._headers, self._body = type(self).replies.pop(0)
        return self

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, limit):
        return json.dumps(self._body).encode()

    def close(self):
        pass


def patch_transport(monkeypatch, replies, now):
    _Connection.replies, _Connection.requests = list(replies), 0
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', _Connection)
    monkeypatch.setattr(account_hub, 'trusted_origin', lambda value: value)
    monkeypatch.setattr(account_hub, 'identifier', lambda value: value)
    monkeypatch.setattr(account_hub, 'secret_for', lambda _: 'test-secret')
    monkeypatch.setattr(account_hub.time, 'monotonic', lambda: now[0])


def test_hub_errors_carry_status_and_retry_after(monkeypatch):
    now = [0.0]
    binding = {'hub_url': 'https://hub.example', 'server_id': 'worker', 'encrypted_secret': 'a'}
    patch_transport(monkeypatch, [(429, {'Retry-After': '60'}, {}), (401, {}, {}), (500, {}, {})], now)
    with pytest.raises(account_hub.HubResponseError) as limited:
        account_hub.call(binding, 'GET', 'status')
    assert (limited.value.status_code, limited.value.hub_status, limited.value.retry_after) == (503, 429, 60)
    with pytest.raises(account_hub.HubResponseError) as revoked:
        account_hub.call(binding, 'GET', 'status')
    assert revoked.value.hub_status == 401
    with pytest.raises(account_hub.HubResponseError) as failed:
        account_hub.call(binding, 'GET', 'status')
    assert (failed.value.hub_status, failed.value.retry_after) == (500, None)


def test_kill_switch_pause_answers_locally_until_retry_after(monkeypatch):
    now = [0.0]
    binding = {'hub_url': 'https://hub.example', 'server_id': 'worker', 'encrypted_secret': 'a'}
    patch_transport(monkeypatch, [(503, {'Retry-After': '3600'}, {'paused': True}), (200, {}, {'active': True})], now)
    with pytest.raises(account_hub.HubResponseError):
        account_hub.call(binding, 'GET', 'status')
    now[0] = 1800
    with pytest.raises(account_hub.HubResponseError) as paused:
        account_hub.call(binding, 'GET', 'network/pending')
    assert paused.value.hub_status == 503 and paused.value.retry_after == 1800
    assert _Connection.requests == 1  # Nothing left the worker while paused.
    now[0] = 3601
    assert account_hub.call(binding, 'GET', 'status') == {'active': True}
    assert _Connection.requests == 2
