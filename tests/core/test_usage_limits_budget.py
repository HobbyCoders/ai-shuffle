"""Claude subscription usage reads never wait on, or multiply, upstream requests.

Every assertion counts requests at the HTTP transport (not log lines). The
monitor uses the real durable budget on the test database.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import usage_limits
from app.core.usage_limits import USAGE_ENDPOINT, UsageLimitsMonitor
from app.network import usage_budget

_RealAsyncClient = httpx.AsyncClient
_BODY = {"five_hour": {"utilization": 10.0, "resets_at": None}}


@pytest.fixture
def upstream(monkeypatch, mock_db):
    state = SimpleNamespace(requests=[], status=200, gate=None, token="token-a", claims=0)

    async def handler(request):
        assert str(request.url) == USAGE_ENDPOINT
        state.requests.append(request)
        if state.gate is not None:
            await state.gate.wait()
        return httpx.Response(state.status, json=_BODY)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _RealAsyncClient(
        *a, **{**kw, "transport": httpx.MockTransport(handler)}))
    oauth = SimpleNamespace(ensure_fresh_credentials=AsyncMock(return_value=False),
                            get_access_token=lambda: state.token)
    monkeypatch.setattr(usage_limits, "direct_oauth", oauth)
    original = usage_budget.acquire

    async def counting_acquire(*args, **kwargs):
        state.claims += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage_budget, "acquire", counting_acquire)
    return state


@pytest.fixture
def monitor(monkeypatch):
    mon = UsageLimitsMonitor()
    monkeypatch.setattr(mon, "_broadcast", AsyncMock())
    return mon


async def _drain(mon):
    while mon._refresh_task is not None and not mon._refresh_task.done():
        await mon._refresh_task


@pytest.mark.asyncio
async def test_concurrent_stale_reads_return_cached_and_share_one_request(upstream, monitor):
    upstream.gate = asyncio.Event()
    reads = await asyncio.wait_for(
        asyncio.gather(*(monitor.get_cluster_snapshot() for _ in range(12))), timeout=2)
    # Every read returned while the upstream request was still blocked.
    assert all(read["available"] is False for read in reads)
    await asyncio.sleep(0.05)
    assert len(upstream.requests) == 1 and monitor.refreshing
    upstream.gate.set()
    await _drain(monitor)
    assert len(upstream.requests) == 1
    assert (await monitor.get_snapshot())["available"] is True


@pytest.mark.asyncio
async def test_explicit_and_stale_refreshes_cannot_bypass_minimum_interval(upstream, monitor):
    for _ in range(5):
        await monitor.request_refresh()
    assert len(upstream.requests) == 1

    # Age the in-memory reading: an ordinary read schedules a refresh, but
    # the durable budget still holds the successful observation's interval.
    monitor._fetched_at -= 10_000
    monitor._attempted_at = 0
    await monitor.get_snapshot()
    await _drain(monitor)
    assert len(upstream.requests) == 1
    assert monitor._snapshot()["attempted_at"] > monitor._snapshot()["fetched_at"]


@pytest.mark.asyncio
async def test_stale_reads_space_out_attempts_after_a_failure(upstream, monitor):
    upstream.status = 500
    fetches = 0
    original = monitor.fetch

    async def counting_fetch():
        nonlocal fetches
        fetches += 1
        return await original()

    monitor.fetch = counting_fetch
    await monitor.get_snapshot()
    await _drain(monitor)
    for _ in range(10):
        await monitor.get_snapshot()
    await _drain(monitor)
    assert fetches == 1 and len(upstream.requests) == 1
    snapshot = monitor._snapshot()
    assert snapshot["last_error_kind"] == "unavailable"
    assert snapshot["fetched_at"] is None and snapshot["attempted_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, kind", [(401, "billing_access_denied"), (501, "unsupported")])
async def test_refused_token_is_not_reprobed_until_it_changes(upstream, monitor, status, kind):
    upstream.status = status
    await monitor.request_refresh()
    assert len(upstream.requests) == 1 and upstream.claims == 1
    for _ in range(3):
        monitor._attempted_at = 0
        await monitor.request_refresh()
    assert len(upstream.requests) == 1 and upstream.claims == 1
    assert monitor._snapshot()["last_error_kind"] == kind

    upstream.token, upstream.status = "token-b", 200
    await monitor.request_refresh()
    assert len(upstream.requests) == 2
    assert monitor._snapshot()["last_error_kind"] is None


@pytest.mark.asyncio
async def test_usage_api_refresh_never_waits_on_upstream(upstream, monkeypatch):
    from app.api import usage as usage_api

    mon = UsageLimitsMonitor()
    monkeypatch.setattr(mon, "_broadcast", AsyncMock())
    monkeypatch.setattr(usage_api, "usage_limits_monitor", mon)
    upstream.gate = asyncio.Event()
    results = await asyncio.wait_for(asyncio.gather(
        *(usage_api.get_usage_limits(refresh=True, _="user") for _ in range(5))), timeout=2)
    assert all(result.provider_refreshing for result in results)
    assert results[0].claude_source.support == "unofficial_subscription"
    upstream.gate.set()
    await _drain(mon)
    assert len(upstream.requests) == 1
