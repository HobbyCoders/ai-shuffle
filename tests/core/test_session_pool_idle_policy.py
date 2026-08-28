"""Tiered idle policy + deadline-driven watchdog on the SessionPool (F1).

What is under test is the *policy*, not the plumbing around it:

  * the HOT tier (the N most-recently-used sessions) keeps the long timeout
    while everything ranked below it is evicted on the short COLD one;
  * a session whose last device disconnected is reclaimed on the detach grace
    regardless of tier;
  * the activity FLOOR protects a session that was busy a moment ago, so an
    over-eager detach signal can never cost a live conversation its subprocess;
  * the watchdog schedules its sleep from the earliest real deadline instead of
    riding somebody else's periodic tick, and parks forever when idle;
  * the legacy flat ``evict_idle(max_idle_seconds=...)`` call still means a flat
    timeout for every session.

No Postgres, no SDK: adapters are mocks and disconnect is an AsyncMock.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.session_pool import IdlePolicy, SessionPool, SessionState


# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_state(*, idle_seconds: float = 0.0, is_streaming: bool = False) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive.return_value = True
    adapter.is_connected.return_value = True
    adapter.sdk_client = None
    adapter.disconnect = AsyncMock()
    return SessionState(
        adapter=adapter,
        is_connected=True,
        is_streaming=is_streaming,
        last_activity=datetime.now(timezone.utc) - timedelta(seconds=idle_seconds),
    )


def _pool(policy: IdlePolicy, **sessions) -> SessionPool:
    """Build a pool; insertion order is LRU order (last inserted == MRU)."""
    pool = SessionPool(max_size=8, idle_policy=policy)
    # Nothing is attached unless a test says so: keep the probe deterministic.
    pool.set_attachment_probe(lambda _sid: None)
    for sid, state in sessions.items():
        pool._pool[sid] = state
    return pool


TEST_POLICY = IdlePolicy(
    hot_size=2,
    hot_idle_seconds=1800,
    cold_idle_seconds=420,
    detached_grace_seconds=180,
    min_idle_floor_seconds=60,
    reevaluate_seconds=60,
)


# ─── Tier ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_tier_evicted_hot_tier_kept():
    """Past the cold timeout: the 2 most-recent survive, the rest do not."""
    pool = _pool(
        TEST_POLICY,
        oldest=_make_state(idle_seconds=600),
        older=_make_state(idle_seconds=600),
        newer=_make_state(idle_seconds=600),
        newest=_make_state(idle_seconds=600),
    )

    evicted = await pool.evict_idle()

    assert sorted(evicted) == ["older", "oldest"]
    assert set(pool._pool) == {"newer", "newest"}


@pytest.mark.asyncio
async def test_hot_tier_survives_until_its_own_timeout():
    """A hot session idles for longer than the COLD timeout and stays."""
    pool = _pool(TEST_POLICY, only=_make_state(idle_seconds=600))

    assert await pool.evict_idle() == []
    assert "only" in pool._pool

    # ...but not past the hot timeout.
    pool._pool["only"].last_activity = datetime.now(timezone.utc) - timedelta(
        seconds=1801
    )
    assert await pool.evict_idle() == ["only"]


@pytest.mark.asyncio
async def test_hot_size_zero_makes_everything_cold():
    policy = IdlePolicy(hot_size=0, hot_idle_seconds=1800, cold_idle_seconds=420)
    pool = _pool(policy, a=_make_state(idle_seconds=600))

    assert await pool.evict_idle() == ["a"]


@pytest.mark.asyncio
async def test_promotion_demotes_a_previously_hot_session():
    """Touching an older session pushes another one out of the hot tier."""
    policy = IdlePolicy(
        hot_size=1, hot_idle_seconds=1800, cold_idle_seconds=300,
        min_idle_floor_seconds=0,
    )
    pool = _pool(
        policy,
        a=_make_state(idle_seconds=600),
        b=_make_state(idle_seconds=600),
    )
    # b is MRU (hot); a is cold.
    assert await pool.evict_idle() == ["a"]

    # Re-seed and promote a: now b is the cold one.
    pool._pool["a"] = _make_state(idle_seconds=600)
    pool.get("a")
    assert await pool.evict_idle() == ["b"]


# ─── Detach ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detached_session_evicted_on_grace_regardless_of_tier():
    pool = _pool(TEST_POLICY, hot=_make_state(idle_seconds=300))
    pool.note_session_detached("hot")
    # Hot tier would keep it for 1800s, but nobody is watching.
    pool._pool["hot"].detached_since = datetime.now(timezone.utc) - timedelta(
        seconds=181
    )

    assert await pool.evict_idle() == ["hot"]


@pytest.mark.asyncio
async def test_reattach_cancels_the_detach_deadline():
    pool = _pool(TEST_POLICY, hot=_make_state(idle_seconds=300))
    pool._pool["hot"].detached_since = datetime.now(timezone.utc) - timedelta(
        seconds=181
    )
    pool.note_session_attached("hot")

    assert await pool.evict_idle() == []
    assert pool._pool["hot"].detached_since is None


@pytest.mark.asyncio
async def test_activity_floor_protects_a_just_active_detached_session():
    """A WS flap between two turns must not cost the subprocess."""
    policy = IdlePolicy(
        hot_size=2, hot_idle_seconds=1800, cold_idle_seconds=420,
        detached_grace_seconds=0.0, min_idle_floor_seconds=60,
    )
    pool = _pool(policy, s=_make_state(idle_seconds=5))
    # Even with a zero grace and an immediate detach stamp, the floor wins.
    policy_zero_grace = IdlePolicy(
        hot_size=2, hot_idle_seconds=1800, cold_idle_seconds=420,
        detached_grace_seconds=0.001, min_idle_floor_seconds=60,
    )
    pool.idle_policy = policy_zero_grace
    pool._pool["s"].detached_since = datetime.now(timezone.utc) - timedelta(seconds=60)

    assert await pool.evict_idle() == []


@pytest.mark.asyncio
async def test_unknown_attachment_never_stamps_a_detach():
    """A probe that cannot tell must not manufacture an eviction."""
    pool = _pool(TEST_POLICY, s=_make_state(idle_seconds=300))
    pool.set_attachment_probe(lambda _sid: None)

    assert await pool.evict_idle() == []
    assert pool._pool["s"].detached_since is None


@pytest.mark.asyncio
async def test_probe_derives_detach_without_any_push_wiring():
    pool = _pool(TEST_POLICY, s=_make_state(idle_seconds=300))
    pool.set_attachment_probe(lambda _sid: False)

    # First evaluation stamps the detach; the grace has not elapsed yet.
    assert await pool.evict_idle() == []
    assert pool._pool["s"].detached_since is not None

    pool._pool["s"].detached_since = datetime.now(timezone.utc) - timedelta(
        seconds=181
    )
    assert await pool.evict_idle() == ["s"]


@pytest.mark.asyncio
async def test_put_clears_a_stale_detach_stamp():
    """A query is running, so the session is not abandoned."""
    pool = _pool(TEST_POLICY, s=_make_state(idle_seconds=300))
    pool._pool["s"].detached_since = datetime.now(timezone.utc) - timedelta(
        seconds=999
    )
    pool.put("s", pool._pool["s"])

    assert pool._pool["s"].detached_since is None
    assert await pool.evict_idle() == []


# ─── Busy guards still win ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_session_is_never_idle_evicted():
    pool = _pool(TEST_POLICY, s=_make_state(idle_seconds=9999, is_streaming=True))
    pool.set_attachment_probe(lambda _sid: False)
    pool._pool["s"].detached_since = datetime.now(timezone.utc) - timedelta(
        seconds=9999
    )

    assert await pool.evict_idle() == []
    assert pool.idle_seconds_remaining("s") is None


@pytest.mark.asyncio
async def test_idle_reader_session_is_never_idle_evicted():
    pool = _pool(TEST_POLICY, s=_make_state(idle_seconds=9999))
    pool._pool["s"].idle_reader_active = True

    assert await pool.evict_idle() == []


# ─── Legacy flat mode ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_max_idle_seconds_is_a_flat_timeout():
    """An explicit argument means one timeout for every session, no tiers."""
    pool = _pool(
        TEST_POLICY,
        a=_make_state(idle_seconds=700),
        b=_make_state(idle_seconds=700),
        c=_make_state(idle_seconds=700),
    )

    evicted = await pool.evict_idle(max_idle_seconds=600)

    assert sorted(evicted) == ["a", "b", "c"]


# ─── Watchdog ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_deadline_is_the_earliest_real_one():
    pool = _pool(
        TEST_POLICY,
        cold=_make_state(idle_seconds=400),   # cold: 20s left
        hot_a=_make_state(idle_seconds=100),  # hot: 1700s left
        hot_b=_make_state(idle_seconds=0),
    )

    delay = pool.next_idle_deadline()

    assert delay is not None
    assert 15 < delay < 25


@pytest.mark.asyncio
async def test_next_deadline_is_none_when_nothing_is_on_a_timer():
    empty = _pool(TEST_POLICY)
    assert empty.next_idle_deadline() is None

    busy = _pool(TEST_POLICY, s=_make_state(idle_seconds=9999, is_streaming=True))
    assert busy.next_idle_deadline() is None


@pytest.mark.asyncio
async def test_watchdog_evicts_on_the_deadline_not_on_a_far_off_tick():
    """The whole point of F1: RAM comes back when it is due."""
    policy = IdlePolicy(
        hot_size=0, hot_idle_seconds=0.05, cold_idle_seconds=0.05,
        detached_grace_seconds=0, min_idle_floor_seconds=0,
        reevaluate_seconds=30,
    )
    pool = _pool(policy, s=_make_state(idle_seconds=0))
    pool.start_idle_watchdog()
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "s" not in pool._pool:
                break
        assert "s" not in pool._pool, "watchdog did not evict on the deadline"
    finally:
        await pool.stop_idle_watchdog()


@pytest.mark.asyncio
async def test_watchdog_parks_with_no_timer_while_the_pool_is_empty():
    policy = IdlePolicy(reevaluate_seconds=0.05)
    pool = _pool(policy)
    pool.start_idle_watchdog()
    try:
        await asyncio.sleep(0.15)
        # Nothing pooled -> the watchdog is parked on its event, and adding a
        # session wakes it rather than waiting out a timer.
        assert pool._idle_task is not None and not pool._idle_task.done()
    finally:
        await pool.stop_idle_watchdog()


@pytest.mark.asyncio
async def test_detach_hook_wakes_the_watchdog_immediately():
    policy = IdlePolicy(
        hot_size=5, hot_idle_seconds=3600, cold_idle_seconds=3600,
        detached_grace_seconds=0.05, min_idle_floor_seconds=0,
        reevaluate_seconds=30,
    )
    pool = _pool(policy, s=_make_state(idle_seconds=0))
    pool.start_idle_watchdog()
    try:
        await asyncio.sleep(0.05)
        assert "s" in pool._pool  # long tier, nothing due
        pool.note_session_detached("s")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "s" not in pool._pool:
                break
        assert "s" not in pool._pool, (
            "a last-device disconnect did not reclaim the subprocess"
        )
    finally:
        await pool.stop_idle_watchdog()


# ─── Policy construction ──────────────────────────────────────────────────


def test_policy_from_settings_reads_the_configured_values():
    class _S:
        sdk_session_hot_pool_size = 3
        sdk_session_idle_timeout_seconds = 900
        sdk_session_cold_idle_timeout_seconds = 120
        sdk_session_detached_grace_seconds = 30
        sdk_session_idle_reevaluate_seconds = 45

    policy = IdlePolicy.from_settings(_S())

    assert policy.hot_size == 3
    assert policy.hot_idle_seconds == 900
    assert policy.cold_idle_seconds == 120
    assert policy.detached_grace_seconds == 30
    assert policy.reevaluate_seconds == 45


def test_policy_from_settings_falls_back_on_garbage():
    class _S:
        sdk_session_hot_pool_size = "nonsense"
        sdk_session_cold_idle_timeout_seconds = None

    policy = IdlePolicy.from_settings(_S())

    assert policy.hot_size == IdlePolicy.hot_size
    assert policy.cold_idle_seconds == IdlePolicy.cold_idle_seconds


def test_cold_timeout_is_clamped_below_the_hot_one():
    """A cold timeout longer than the hot one would invert the tiers."""
    policy = IdlePolicy(hot_size=1, hot_idle_seconds=100, cold_idle_seconds=999)

    assert policy.tier_idle_seconds(0) == 100
    assert policy.tier_idle_seconds(5) == 100


def test_configured_defaults_target_a_low_idle_floor():
    """The shipped defaults are the point of F1 — guard them."""
    from app.core.config import settings

    assert settings.sdk_session_pool_size <= 4
    assert settings.sdk_session_hot_pool_size <= 2
    assert settings.sdk_session_cold_idle_timeout_seconds <= 600
    assert settings.sdk_session_detached_grace_seconds > 0


# ─── Monitor's pool-capacity warning (F10's stale constant) ───────────────


def test_capacity_warning_uses_the_reported_pool_max():
    from app.core.resource_monitor import _check_warnings

    warnings = _check_warnings({"pool_size": 4, "pool_max_size": 4})
    assert any("Session pool at capacity (4/4)" in w for w in warnings)

    warnings = _check_warnings({"pool_size": 2, "pool_max_size": 4})
    assert not any("at capacity" in w for w in warnings)


def test_capacity_warning_falls_back_to_the_configured_size_not_a_constant():
    """The old fallback was a literal 3 and went stale the moment the default moved."""
    from app.core.config import settings
    from app.core.resource_monitor import _check_warnings

    configured = settings.sdk_session_pool_size
    at_capacity = _check_warnings({"pool_size": configured})
    below = _check_warnings({"pool_size": configured - 1})

    assert any("at capacity" in w for w in at_capacity)
    assert not any("at capacity" in w for w in below)


def test_capacity_warning_is_silent_before_the_pool_exists():
    from app.core.resource_monitor import _check_warnings

    assert not any("at capacity" in w for w in _check_warnings({}))
