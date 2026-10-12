"""#1386: forced-refresh cooldown, negative caching and cross-worker refreshes.

Every scenario runs on a fake clock and counts three things: poll passes,
budget claims (a DB write, or a round trip to the network coordinator) and
upstream provider requests. "Before" is the same scenario with the cooldown
set to zero, which is exactly the pre-cooldown behaviour.
"""

import asyncio
import time
import types
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core import provider_usage
from app.core import usage_polling as polling
from app.core.usage_limits import UsageLimitsMonitor
from app.db import database as db
from app.network import usage_budget

PROVIDER = "cooldown-router"


@pytest.fixture(autouse=True)
def _no_ambient_fal_credentials(monkeypatch):
    # These request counts cover only the seeded OpenRouter rows. An inherited
    # fal key also schedules the global billing poll, doubling the mock's calls
    # (and budget claims) without duplicating any OpenRouter request.
    for name in provider_usage.FAL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class Clock:
    def __init__(self):
        # Real epoch base: failure rows are stamped with the real wall clock.
        self.now = float(int(time.time()))

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(provider_usage, "time", types.SimpleNamespace(time=c, monotonic=c))
    monkeypatch.setattr(usage_budget, "time", types.SimpleNamespace(time=c))
    monkeypatch.setattr(polling.random, "uniform", lambda *_: 0)
    return c


@pytest.fixture
def counts(monkeypatch):
    counted = {"claims": 0}
    real = usage_budget.acquire

    async def acquire(key, interval=300):
        counted["claims"] += 1
        return await real(key, interval)

    monkeypatch.setattr(usage_budget, "acquire", acquire)
    return counted


@pytest.fixture
def upstream(monkeypatch):
    get = AsyncMock()
    client = Mock()
    client.get = get
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(polling.httpx, "AsyncClient", Mock(return_value=context))
    return get


def respond(upstream, status=200):
    body = {"data": {"usage": 1.0, "limit": 10.0}} if status == 200 else {}
    upstream.return_value = httpx.Response(
        status, json=body, request=httpx.Request("GET", "https://router.example.test/api/v1/key"))


@pytest.fixture
def provider():
    # A custom OpenRouter host: only the model-key reader applies.
    return db.create_model_provider(
        provider_id=PROVIDER, name="Router", provider_type="openrouter",
        base_url="https://router.example.test/api/v1",
        auth_token="model-key",  # plaintext rows are still read (migration compatibility)
    )


async def popover_opens(monitor, clock, *, every, until, cooldown):
    """Force a refresh every ``every`` seconds; return the passes started."""
    passes = 0
    start = clock.now
    while clock.now - start <= until:
        task = monitor.request_refresh(force=True, cooldown=cooldown)
        if task is not None:
            passes += 1
            await task
        clock.now += every
    return passes


@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown, expected", [
    # 201 opens over ten minutes. Before: every open ran a pass and claimed.
    (0, {"passes": 201, "claims": 201, "upstream": 3}),
    # After: one pass per 30 s. The 300 s budget kept upstream at t=0, 300
    # and 600 s either way.
    (30, {"passes": 21, "claims": 21, "upstream": 3}),
])
async def test_forced_refresh_respects_cooldown(provider, clock, counts, upstream,
                                                cooldown, expected):
    respond(upstream)
    passes = await popover_opens(provider_usage.ProviderUsageMonitor(), clock,
                                 every=3, until=600, cooldown=cooldown)
    assert {"passes": passes, "claims": counts["claims"], "upstream": upstream.await_count} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("env_name", provider_usage.FAL_KEY_ENV_VARS)
async def test_explicit_fal_key_polls_a_separate_endpoint(
    provider, clock, upstream, monkeypatch, env_name,
):
    """Two configured sources mean two requests, not a duplicate model-key poll."""
    monkeypatch.setenv(env_name, "dummy-fal-key")
    router_url = "https://router.example.test/api/v1/key"
    bodies = {
        router_url: {"data": {"usage": 1.0, "limit": 10.0}},
        provider_usage.FAL_BALANCE_URL: {"balance": 5.0},
    }

    def response(url, **_):
        return httpx.Response(200, json=bodies[url], request=httpx.Request("GET", url))

    upstream.side_effect = response
    monitor = provider_usage.ProviderUsageMonitor()
    await monitor.request_refresh(force=True, cooldown=30)
    assert sorted(call.args[0] for call in upstream.await_args_list) == sorted(bodies)
    snapshots = {s["provider_id"]: s for s in db.get_all_provider_usage_snapshots()}
    assert snapshots[PROVIDER]["credits"]["usage"] == 1.0
    assert snapshots[provider_usage.FAL_USAGE_PROVIDER_ID]["credits"]["balance_remaining"] == 5.0

    clock.now += 30
    await monitor.request_refresh(force=True, cooldown=30)
    assert upstream.await_count == 2  # Both sources are still budget-limited.
    clock.now += 270
    await monitor.request_refresh(force=True, cooldown=30)
    assert sorted(call.args[0] for call in upstream.await_args_list) == sorted(list(bodies) * 2)


@pytest.mark.asyncio
async def test_forced_refresh_serves_last_snapshot_with_cooldown_until(provider, clock, upstream):
    from app.api.usage import _load_provider_usage

    respond(upstream)
    monitor = provider_usage.ProviderUsageMonitor()
    await monitor.request_refresh(force=True, cooldown=30)
    fetched = int(clock.now * 1000)
    clock.now += 10
    assert monitor.request_refresh(force=True, cooldown=30) is None
    entry = next(p for p in await _load_provider_usage() if p.provider_id == PROVIDER)
    assert entry.fetched_at == fetched and entry.credits["usage"] == 1.0
    # The budget allows the next request 300 s after the last one.
    assert entry.cooldown_until == fetched + 300_000
    assert upstream.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown, expected_claims", [(0, 2), (30, 1)])
async def test_second_worker_uses_the_snapshot_timestamp(provider, clock, counts, upstream,
                                                         cooldown, expected_claims):
    """Two workers share the snapshot; the second one's refresh 10 s later
    neither claims a budget nor requests the provider."""
    respond(upstream)
    first, second = provider_usage.ProviderUsageMonitor(), provider_usage.ProviderUsageMonitor()
    await first.request_refresh(force=True, cooldown=cooldown)
    clock.now += 10
    await second.request_refresh(force=True, cooldown=cooldown)
    assert counts["claims"] == expected_claims
    assert upstream.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stagger_last", [False, True])
async def test_concurrent_workers_issue_one_request(provider, clock, counts, upstream,
                                                    stagger_last):
    respond(upstream)
    workers = [provider_usage.ProviderUsageMonitor() for _ in range(4)]
    first_batch = workers[:-1] if stagger_last else workers
    await asyncio.gather(*(w.request_refresh(force=True, cooldown=30) for w in first_batch))
    initial_claims = counts["claims"]
    # A worker can read the snapshot after another worker finishes. It then
    # skips the claim entirely; concurrency does not require four claims.
    assert 1 <= initial_claims <= len(first_batch)
    if stagger_last:
        await workers[-1].request_refresh(force=True, cooldown=30)
        assert counts["claims"] == initial_claims
    assert upstream.await_count == 1
    clock.now += 15
    # Four more workers that have not polled yet: within the cooldown each
    # serves the snapshot another worker wrote, without any claim.
    later = [provider_usage.ProviderUsageMonitor() for _ in range(4)]
    await asyncio.gather(*(w.request_refresh(force=True, cooldown=30) for w in later))
    assert counts["claims"] == initial_claims
    assert upstream.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected_upstream, error", [
    # Refused: remembered for this credential; never re-probed while it lasts.
    (403, 1, "billing_access_denied"),
    (405, 1, "unsupported"),
    # Not found / server error: bounded backoff 300, 600, 1200 s...
    (404, 4, "unavailable"),
])
async def test_failures_are_negative_cached(provider, clock, upstream, status, expected_upstream, error):
    from app.api.usage import _load_provider_usage

    respond(upstream, status)
    # A forced refresh every minute for an hour.
    await popover_opens(provider_usage.ProviderUsageMonitor(), clock, every=60, until=3600, cooldown=30)
    assert upstream.await_count == expected_upstream
    entry = next(p for p in await _load_provider_usage() if p.provider_id == PROVIDER)
    assert entry.last_error_kind == error
    if status in (403, 405):
        # Paused until the credential changes; the reason is the status.
        assert entry.cooldown_until is None


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_good_values(provider, clock, upstream):
    from app.api.usage import _load_provider_usage

    respond(upstream)
    monitor = provider_usage.ProviderUsageMonitor()
    await monitor.poll_once()
    fetched = int(clock.now * 1000)
    respond(upstream, 503)
    clock.now += 400
    await monitor.poll_once()
    assert upstream.await_count == 2
    entry = next(p for p in await _load_provider_usage() if p.provider_id == PROVIDER)
    assert entry.status == "stale" and entry.last_error_kind == "unavailable"
    assert entry.credits["usage"] == 1.0 and entry.fetched_at == fetched


@pytest.mark.asyncio
async def test_timeouts_back_off_and_keep_last_good_values(provider, clock, upstream):
    from app.api.usage import _load_provider_usage

    respond(upstream)
    monitor = provider_usage.ProviderUsageMonitor()
    await monitor.poll_once()
    fetched = int(clock.now * 1000)
    clock.now += 300
    upstream.side_effect = httpx.ReadTimeout("slow")
    # A forced refresh every minute for an hour while the provider times out:
    # backoff 300, 600, 1200, 2400 s, not one request per open.
    await popover_opens(monitor, clock, every=60, until=3600, cooldown=30)
    assert upstream.await_count == 1 + 4
    entry = next(p for p in await _load_provider_usage() if p.provider_id == PROVIDER)
    # Before: a timeout was only logged, so the row showed its last success as
    # if nothing had been attempted since.
    assert entry.last_error_kind == "timeout" and entry.status == "stale"
    assert entry.credits["usage"] == 1.0 and entry.fetched_at == fetched
    assert entry.last_attempt_at > entry.last_success_at


# ---------------------------------------------------------------------------
# Duplicate rows: rows that share a credential share one observation
# ---------------------------------------------------------------------------

def _duplicate_rows(*ids):
    for provider_id in ids:
        db.create_model_provider(
            provider_id=provider_id, name=provider_id, provider_type="openrouter",
            base_url="https://router.example.test/api/v1", auth_token="shared-model-key",
        )


@pytest.mark.asyncio
async def test_duplicate_rows_share_one_request_and_all_refresh(clock, counts, upstream):
    """Before: the first row spent the budget and the others were deferred on
    every pass, so they stayed stale forever while the provider was asked once."""
    from app.api.usage import _load_provider_usage

    ids = ("dup-a", "dup-b", "dup-c", "dup-d", "dup-e", "dup-f")
    _duplicate_rows(*ids)
    respond(upstream)
    monitor = provider_usage.ProviderUsageMonitor()
    for passes in range(1, 5):
        await monitor.request_refresh(force=True, cooldown=30)
        assert upstream.await_count == passes
        entries = {p.provider_id: p for p in await _load_provider_usage()}
        assert {entries[i].fetched_at for i in ids} == {int(clock.now * 1000)}
        assert all(entries[i].status == "fresh" for i in ids)
        clock.now += 301
    # One budget claim per pass: the other rows reuse the observation.
    assert counts["claims"] == 4


@pytest.mark.asyncio
async def test_duplicate_rows_share_a_compressed_response(clock, upstream):
    """Real providers answer gzip-encoded. The shared copy must not be decoded
    twice, or every row after the first fails instead of refreshing."""
    import gzip
    import json

    from app.api.usage import _load_provider_usage

    ids = ("dup-a", "dup-b", "dup-c")
    _duplicate_rows(*ids)
    body = gzip.compress(json.dumps({"data": {"usage": 2.0, "limit": 10.0}}).encode())
    upstream.return_value = httpx.Response(
        200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}, content=body,
        request=httpx.Request("GET", "https://router.example.test/api/v1/key"))
    await provider_usage.ProviderUsageMonitor().request_refresh(force=True, cooldown=30)
    assert upstream.await_count == 1
    entries = {p.provider_id: p for p in await _load_provider_usage()}
    assert all(entries[i].status == "fresh" and entries[i].credits["usage"] == 2.0 for i in ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_duplicate_rows_do_not_multiply_failed_requests(clock, upstream, status):
    _duplicate_rows("dup-a", "dup-b", "dup-c")
    respond(upstream, status)
    await popover_opens(provider_usage.ProviderUsageMonitor(), clock, every=60, until=600, cooldown=30)
    # 403 pauses; 429/503 back off 300 then 600 s. Never once per row.
    assert upstream.await_count == {403: 1, 429: 2, 503: 2}[status]


@pytest.mark.asyncio
async def test_shared_pass_coalesces_only_identical_requests(monkeypatch, clock):
    calls, release = [], asyncio.Event()

    async def get(url, headers=None, **_):
        calls.append(headers["Authorization"])
        await release.wait()
        return httpx.Response(200, json={"seen": headers["Authorization"]},
                              request=httpx.Request("GET", url))

    client = Mock(get=get)
    context = Mock(__aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False))
    monkeypatch.setattr(polling.httpx, "AsyncClient", Mock(return_value=context))

    async def fetch(token):
        return await polling.usage_get("https://router.example.test/api/v1/key",
                                       headers={"Authorization": f"Bearer {token}"})

    with polling.shared_pass():
        tasks = [asyncio.create_task(fetch(t)) for t in ("one", "one", "two", "one")]
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(*tasks)
    assert sorted(calls) == ["Bearer one", "Bearer two"]
    assert [r.json()["seen"] for r in results] == ["Bearer one", "Bearer one", "Bearer two", "Bearer one"]
    # Outside a pass nothing is reused: the budget defers the repeat.
    with pytest.raises(polling.UsageDeferred):
        await fetch("one")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_shared_pass_defers_waiters_when_the_request_is_cancelled(monkeypatch, clock):
    started, calls = asyncio.Event(), []

    async def get(url, **_):
        calls.append(url)
        started.set()
        await asyncio.Event().wait()

    client = Mock(get=get)
    context = Mock(__aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False))
    monkeypatch.setattr(polling.httpx, "AsyncClient", Mock(return_value=context))
    headers = {"Authorization": "Bearer one"}
    url = "https://router.example.test/api/v1/key"

    with polling.shared_pass():
        owner = asyncio.create_task(polling.usage_get(url, headers=headers))
        await started.wait()
        waiter = asyncio.create_task(polling.usage_get(url, headers=headers))
        await asyncio.sleep(0)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        with pytest.raises(polling.UsageDeferred):
            await waiter
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Claude: refresh=true re-persists and broadcasts at most once per cooldown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown, expected", [(0, 20), (30, 2)])
async def test_claude_refresh_cooldown(monkeypatch, cooldown, expected):
    from app.core import usage_limits

    clock = Clock()
    monkeypatch.setattr(usage_limits, "time", types.SimpleNamespace(time=clock, monotonic=clock))
    monitor = UsageLimitsMonitor()
    monitor.fetch = AsyncMock()
    monitor._broadcast = AsyncMock()
    monkeypatch.setattr(monitor, "_cluster_snapshot", dict)
    for _ in range(20):  # a popover open every 3 s for a minute
        task = monitor.request_refresh(cooldown=cooldown)
        if task is not None:
            await task
        clock.now += 3
    assert monitor.fetch.await_count == expected
    assert monitor._broadcast.await_count == expected


# ---------------------------------------------------------------------------
# U-10: a consumed banked reset clears the exhausted window
# ---------------------------------------------------------------------------

def _grok_snapshot():
    db.upsert_provider_usage_snapshot(
        "reset-grok", "grok", "SuperGrok",
        [{"type": "five_hour", "utilization": 100.0, "resets_at": "1"},
         {"type": "seven_day", "utilization": 40.0, "resets_at": "2"}],
        None, {}, 1234,
    )


def _windows():
    row = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == "reset-grok")
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, expected", [
    ("reset", ["seven_day"]),
    ("nothing_to_reset", ["five_hour", "seven_day"]),
    ("no_credit", ["five_hour", "seven_day"]),
])
async def test_banked_reset_clears_exhausted_window(outcome, expected):
    _grok_snapshot()
    await provider_usage.note_banked_reset("reset-grok", {"outcome": outcome, "windows_reset": 1})
    row = _windows()
    assert [w["type"] for w in row["windows"]] == expected
    assert row["fetched_at"] == 1234 and row["plan_type"] == "SuperGrok"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grok", "codex"])
async def test_consume_endpoints_clear_the_window(monkeypatch, kind):
    from app.api import model_providers
    from app.api.model_providers import CodexConsumeResetCreditRequest

    _grok_snapshot()
    result = {"outcome": "reset", "windows_reset": 1}
    monkeypatch.setattr(model_providers, f"_{kind}_reset_request_with_refresh", AsyncMock(return_value=result))
    endpoint = getattr(model_providers, f"{kind}_consume_rate_limit_reset_credit")
    body = CodexConsumeResetCreditRequest.model_construct(credit_id="c", idempotency_key="k")
    assert await endpoint("reset-grok", body, token="admin") == result
    assert [w["type"] for w in _windows()["windows"]] == ["seven_day"]
