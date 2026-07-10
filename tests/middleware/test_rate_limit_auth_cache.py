"""Tests for the short-TTL positive auth-lookup cache in RateLimitMiddleware.

These prove the middleware's per-request threaded DB auth lookup
(``run_sync(database.get_auth_session, ...)``) is served from an in-process
cache on repeat requests, that the cache expires, and that anonymous/failed
lookups are never cached. Everything is exercised against a fake request so no
database or full app is needed.
"""

import types

import pytest

import app.middleware.rate_limit as rl
from app.middleware.rate_limit import (
    RateLimitMiddleware,
    _AuthLookupCache,
    _auth_cache_key,
    invalidate_auth_cache_for_session,
)


class FakeState:
    pass


class FakeRequest:
    """Minimal stand-in for a Starlette Request as used by _get_user_info."""

    def __init__(self, *, cookies=None, headers=None, query_params=None):
        self.state = FakeState()
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.query_params = query_params or {}


@pytest.fixture
def middleware():
    # BaseHTTPMiddleware.__init__ requires an app; a dummy callable is fine
    # because we only call the pure _get_user_info helper.
    return RateLimitMiddleware(app=lambda scope, receive, send: None)


@pytest.fixture
def clock():
    """A controllable monotonic clock for driving TTL expiry deterministically."""
    return {"t": 1000.0}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch, clock):
    """Swap in a fresh cache bound to the fake clock for each test."""
    cache = _AuthLookupCache(ttl=5.0, time_func=lambda: clock["t"])
    monkeypatch.setattr(rl, "_auth_lookup_cache", cache)
    return cache


@pytest.fixture
def db_counter(monkeypatch):
    """Replace run_sync with a counting async stub.

    Returns a truthy 'session' for get_auth_session so the cookie path resolves
    to admin; counts every invocation so tests can assert cache hits/misses.
    """
    calls = {"n": 0}

    async def fake_run_sync(fn, *args, **kwargs):
        calls["n"] += 1
        if fn is rl.database.get_auth_session:
            return {"id": "sess-1"}  # truthy -> admin
        return None

    monkeypatch.setattr(rl, "run_sync", fake_run_sync)
    return calls


@pytest.mark.asyncio
async def test_second_request_within_ttl_skips_db(middleware, db_counter):
    """A repeat lookup with the same cookie is served from cache (no 2nd DB hit)."""
    req1 = FakeRequest(cookies={"session": "tok-abc"})
    res1 = await middleware._get_user_info(req1)
    assert res1 == ("admin", None, True)
    assert db_counter["n"] == 1

    # Fresh request object (new request.state) — only the module cache can help.
    req2 = FakeRequest(cookies={"session": "tok-abc"})
    res2 = await middleware._get_user_info(req2)
    assert res2 == ("admin", None, True)
    assert db_counter["n"] == 1  # unchanged: served from cache


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(middleware, db_counter, clock):
    """After the TTL elapses the DB is consulted again."""
    req1 = FakeRequest(cookies={"session": "tok-abc"})
    await middleware._get_user_info(req1)
    assert db_counter["n"] == 1

    clock["t"] += 5.001  # push just past the 5s TTL

    req2 = FakeRequest(cookies={"session": "tok-abc"})
    res2 = await middleware._get_user_info(req2)
    assert res2 == ("admin", None, True)
    assert db_counter["n"] == 2  # cache expired -> DB hit again


@pytest.mark.asyncio
async def test_anonymous_result_is_not_cached(middleware, monkeypatch):
    """Failed/anonymous lookups are never cached (a new session must work at once)."""
    calls = {"n": 0}

    async def always_none(fn, *args, **kwargs):
        calls["n"] += 1
        return None  # nothing resolves -> anonymous

    monkeypatch.setattr(rl, "run_sync", always_none)

    req1 = FakeRequest(cookies={"session": "unknown-tok"})
    res1 = await middleware._get_user_info(req1)
    assert res1 == (None, None, False)
    assert calls["n"] == 1

    req2 = FakeRequest(cookies={"session": "unknown-tok"})
    res2 = await middleware._get_user_info(req2)
    assert res2 == (None, None, False)
    assert calls["n"] == 2  # not cached: DB hit again


@pytest.mark.asyncio
async def test_no_credentials_does_not_hit_db(middleware, db_counter):
    """A request with no tokens never touches the cache or the DB."""
    req = FakeRequest()
    res = await middleware._get_user_info(req)
    assert res == (None, None, False)
    assert db_counter["n"] == 0


@pytest.mark.asyncio
async def test_logout_purge_invalidates_cached_session(middleware, db_counter, fresh_cache):
    """invalidate_auth_cache_for_session purges the cookie-only cache entry."""
    req1 = FakeRequest(cookies={"session": "tok-abc"})
    await middleware._get_user_info(req1)
    assert db_counter["n"] == 1

    # The cookie-only key must be present, then purged by the logout helper.
    assert fresh_cache.get(_auth_cache_key("tok-abc", None, None)) is not None
    invalidate_auth_cache_for_session("tok-abc")
    assert fresh_cache.get(_auth_cache_key("tok-abc", None, None)) is None

    # Next request re-hits the DB (fresh classification).
    req2 = FakeRequest(cookies={"session": "tok-abc"})
    await middleware._get_user_info(req2)
    assert db_counter["n"] == 2


@pytest.mark.asyncio
async def test_distinct_tokens_do_not_collide(middleware, db_counter):
    """Different credentials get independent cache entries."""
    await middleware._get_user_info(FakeRequest(cookies={"session": "tok-a"}))
    await middleware._get_user_info(FakeRequest(cookies={"session": "tok-b"}))
    assert db_counter["n"] == 2  # two distinct lookups, neither served the other


def test_cache_bounds_size_via_lru():
    """The cache evicts oldest entries once max_entries is exceeded."""
    t = {"v": 0.0}
    cache = _AuthLookupCache(ttl=1000.0, max_entries=3, time_func=lambda: t["v"])
    for i in range(5):
        cache.set(f"k{i}", (f"u{i}", None, True))
    # Only the last 3 keys survive.
    assert cache.get("k0") is None
    assert cache.get("k1") is None
    assert cache.get("k2") == ("u2", None, True)
    assert cache.get("k4") == ("u4", None, True)


def test_cache_key_never_contains_raw_token():
    """The key is a sha256 digest, not the raw secret."""
    key = _auth_cache_key("super-secret-token", None, None)
    assert "super-secret-token" not in key
    assert len(key) == 64  # hex sha256
