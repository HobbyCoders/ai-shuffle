"""
Tests for the unified session-lifecycle changes on the SessionPool:

  1. The background-work busy guard (`_has_background_work`) that stops LRU and
     idle eviction from disconnecting a pooled SDK client out from under a
     PARKED background sub-agent (parent turn already returned, so the session
     looks idle to `is_streaming` — but tearing it down kills the sub-agent).
  2. The post-eviction callback fired from `_do_evict` (the single funnel for
     LRU + idle eviction) that lets a higher layer clean up the per-session
     live-store buffer + task registry, replacing the deleted standalone 30s
     cleanup timer. One lifecycle clock: pool membership.

The busy signal flows through the REAL task_registry singleton (lazy-imported
by `_has_background_work`), so these tests register a real agent task to mark a
session busy and reset the registry between tests. No Postgres / SDK needed —
the adapter is mocked and disconnect is an AsyncMock.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.core.session_pool as sp
from app.core.session_pool import SessionPool, SessionState
from app.core.task_registry import task_registry


# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_state(
    *, is_streaming: bool = False, idle_seconds: float = 0.0,
    connected: bool = True, alive: bool = True,
) -> SessionState:
    """Build a SessionState backed by a mock adapter with an awaitable disconnect.

    `idle_seconds` backdates last_activity so evict_idle can be exercised without
    sleeping. The adapter's subprocess PID resolves to None (sdk_client=None), so
    no real process is ever touched.
    """
    adapter = MagicMock()
    adapter.is_alive.return_value = alive
    adapter.is_connected.return_value = connected
    adapter.sdk_client = None
    adapter.disconnect = AsyncMock()

    last_activity = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    started = datetime.now(timezone.utc) if is_streaming else None
    return SessionState(
        adapter=adapter,
        is_connected=connected,
        is_streaming=is_streaming,
        streaming_started_at=started,
        last_activity=last_activity,
    )


def _mark_busy(session_id: str) -> None:
    """Register a running background AGENT task so the pool sees the session busy."""
    task_registry.start_task(session_id, f"{session_id}-agent", "agent", "bg work")


@pytest.fixture(autouse=True)
def _isolate_registry_and_reaper():
    """Keep the global registry + reaper tree isolated per test."""
    task_registry.reset_all()
    sp._terminated_trees.clear()
    yield
    task_registry.reset_all()
    sp._terminated_trees.clear()


# ─── (1) Busy guard: evict_if_needed ────────────────────────────────────────


@pytest.mark.asyncio
async def test_evict_if_needed_skips_busy_session_prefers_idle():
    """The LRU session is busy with a background agent — eviction skips it and
    picks the next (idle) candidate instead."""
    pool = SessionPool(max_size=1)
    pool.put("busy", _make_state())   # LRU (oldest), but busy
    pool.put("idle", _make_state())   # MRU, evictable
    _mark_busy("busy")

    evicted = await pool.evict_if_needed()

    assert evicted == "idle"
    assert "busy" in pool          # protected despite being LRU
    assert "idle" not in pool


@pytest.mark.asyncio
async def test_evict_if_needed_allows_overcapacity_when_only_busy():
    """When every candidate is busy and we're under the hard cap, eviction
    refuses (temporary over-capacity) rather than killing a parked agent."""
    pool = SessionPool(max_size=1)  # hard cap = 2
    pool.put("busy", _make_state())
    _mark_busy("busy")

    evicted = await pool.evict_if_needed()

    assert evicted is None
    assert "busy" in pool


@pytest.mark.asyncio
async def test_evict_if_needed_force_evicts_busy_at_hard_cap():
    """At the hard cap with every session busy, the ultimate memory bound still
    force-evicts the LRU session (reason 'lru_force') and fires the callback."""
    seen: list[tuple[str, str]] = []

    async def _cb(sid: str, reason: str) -> None:
        seen.append((sid, reason))

    pool = SessionPool(max_size=1)  # hard cap = 2
    pool.set_eviction_callback(_cb)
    pool.put("busy-old", _make_state())  # LRU
    pool.put("busy-new", _make_state())
    _mark_busy("busy-old")
    _mark_busy("busy-new")

    evicted = await pool.evict_if_needed()

    assert evicted == "busy-old"            # LRU force-evicted
    assert "busy-old" not in pool
    assert seen == [("busy-old", "lru_force")]


# ─── (1) Busy guard: evict_idle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evict_idle_skips_busy_session():
    """A long-idle session that is parked on a background agent is NOT idle-evicted."""
    pool = SessionPool(max_size=3)
    pool.put("parked", _make_state(idle_seconds=7200))
    _mark_busy("parked")

    evicted = await pool.evict_idle(max_idle_seconds=600)

    assert evicted == []
    assert "parked" in pool


@pytest.mark.asyncio
async def test_evict_idle_evicts_non_busy_idle_session():
    """A long-idle session with no background work IS idle-evicted, callback fires 'idle'."""
    seen: list[tuple[str, str]] = []

    async def _cb(sid: str, reason: str) -> None:
        seen.append((sid, reason))

    pool = SessionPool(max_size=3)
    pool.set_eviction_callback(_cb)
    pool.put("stale", _make_state(idle_seconds=7200))

    evicted = await pool.evict_idle(max_idle_seconds=600)

    assert evicted == ["stale"]
    assert "stale" not in pool
    assert seen == [("stale", "idle")]


@pytest.mark.asyncio
async def test_evict_idle_keeps_recently_active_session():
    """A session under the idle threshold is left alone regardless of busy state."""
    pool = SessionPool(max_size=3)
    pool.put("fresh", _make_state(idle_seconds=30))

    assert await pool.evict_idle(max_idle_seconds=600) == []
    assert "fresh" in pool


# ─── (2) Eviction callback fired from _do_evict ─────────────────────────────


@pytest.mark.asyncio
async def test_do_evict_fires_callback_with_session_and_reason():
    """_do_evict (the single LRU+idle funnel) fires the callback with (id, reason)
    AFTER removing the session and disconnecting the adapter."""
    seen: list[tuple[str, str]] = []

    async def _cb(sid: str, reason: str) -> None:
        # The session must already be out of the pool when cleanup runs.
        assert sid not in pool
        seen.append((sid, reason))

    pool = SessionPool(max_size=3)
    pool.set_eviction_callback(_cb)
    state = _make_state()
    pool.put("ses-x", state)

    returned = await pool._do_evict("ses-x", state, "lru")

    assert returned == "ses-x"
    assert "ses-x" not in pool
    assert seen == [("ses-x", "lru")]
    state.adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_evict_without_callback_is_noop_safe():
    """With no callback registered, eviction still works (callback is optional)."""
    pool = SessionPool(max_size=3)
    state = _make_state()
    pool.put("ses-y", state)

    assert await pool._do_evict("ses-y", state, "idle") == "ses-y"
    assert "ses-y" not in pool


@pytest.mark.asyncio
async def test_eviction_callback_failure_does_not_break_eviction():
    """A throwing callback is swallowed: the session is still evicted and no
    exception propagates out of eviction."""
    async def _bad_cb(sid: str, reason: str) -> None:
        raise RuntimeError("cleanup blew up")

    pool = SessionPool(max_size=3)
    pool.set_eviction_callback(_bad_cb)
    state = _make_state()
    pool.put("ses-z", state)

    # Must not raise.
    assert await pool._do_evict("ses-z", state, "lru") == "ses-z"
    assert "ses-z" not in pool


@pytest.mark.asyncio
async def test_set_eviction_callback_none_clears_it():
    """Passing None clears a previously-registered callback."""
    seen: list[tuple[str, str]] = []

    async def _cb(sid: str, reason: str) -> None:
        seen.append((sid, reason))

    pool = SessionPool(max_size=3)
    pool.set_eviction_callback(_cb)
    pool.set_eviction_callback(None)
    state = _make_state()
    pool.put("ses-w", state)

    await pool._do_evict("ses-w", state, "lru")
    assert seen == []  # cleared callback never fired


# ─── (1) Idle-reader hold guard: both eviction paths ────────────────────────
#
# During the CLI-wake grace window the between-turns idle reader holds the
# pooled client with is_streaming False and ZERO running background work
# (waiting for the CLI's delayed wake turn), and it reads via peek so
# last_activity never bumps. SessionState.idle_reader_active is what stops
# LRU/idle eviction from tearing the subprocess down mid-wait.


@pytest.mark.asyncio
async def test_evict_if_needed_skips_idle_reader_session():
    """The LRU session is held by an active idle reader (CLI-wake grace window)
    — LRU eviction skips it and picks the next candidate instead."""
    pool = SessionPool(max_size=1)
    reader_state = _make_state()
    reader_state.idle_reader_active = True
    pool.put("reader-held", reader_state)  # LRU (oldest), but reader-held
    pool.put("plain", _make_state())       # MRU, evictable

    evicted = await pool.evict_if_needed()

    assert evicted == "plain"
    assert "reader-held" in pool  # protected despite being LRU
    assert "plain" not in pool


@pytest.mark.asyncio
async def test_evict_idle_skips_idle_reader_session():
    """A long-idle session held by an active idle reader is NOT idle-evicted —
    peek-based reading never bumps last_activity, so idle age alone must not
    kill the client while the reader waits for the CLI wake."""
    pool = SessionPool(max_size=3)
    state = _make_state(idle_seconds=7200)
    state.idle_reader_active = True
    pool.put("waiting-on-wake", state)

    evicted = await pool.evict_idle(max_idle_seconds=600)

    assert evicted == []
    assert "waiting-on-wake" in pool


@pytest.mark.asyncio
async def test_idle_reader_session_evictable_again_after_clear():
    """Once the reader clears the flag, the same session is evictable by both
    paths again (no permanent immunity)."""
    pool = SessionPool(max_size=3)
    state = _make_state(idle_seconds=7200)
    state.idle_reader_active = True
    pool.put("was-held", state)

    assert await pool.evict_idle(max_idle_seconds=600) == []  # protected

    state.idle_reader_active = False  # reader exited (its finally clears)

    evicted = await pool.evict_idle(max_idle_seconds=600)
    assert evicted == ["was-held"]
    assert "was-held" not in pool


@pytest.mark.asyncio
async def test_idle_reader_flag_clears_on_reader_cancellation(monkeypatch):
    """run_idle_reader sets the flag on the pooled state for its whole run and
    its finally clears it even when the reader is CANCELLED mid-drain (the
    stop_idle_reader path) — a stopped reader must never leave the session
    permanently eviction-immune."""
    import asyncio

    from app.core import idle_reader

    pool = SessionPool(max_size=3)
    state = _make_state()
    pool.put("cancel-me", state)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool)

    entered = asyncio.Event()

    async def _blocking_drain(session_id, *, drain_turn_id=None):
        entered.set()
        await asyncio.Event().wait()  # block until cancelled

    monkeypatch.setattr(idle_reader, "_drain", _blocking_drain)

    task = asyncio.create_task(idle_reader.run_idle_reader("cancel-me"))
    await entered.wait()
    assert state.idle_reader_active is True  # held while the reader runs

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.idle_reader_active is False  # cleared despite cancellation


@pytest.mark.asyncio
async def test_idle_reader_flag_clears_on_reader_exception(monkeypatch):
    """The flag also clears when the reader dies via an unexpected exception
    (run_idle_reader swallows it — never raises to the caller)."""
    import asyncio

    from app.core import idle_reader

    pool = SessionPool(max_size=3)
    state = _make_state()
    pool.put("crash-me", state)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool)

    async def _crashing_drain(session_id, *, drain_turn_id=None):
        assert state.idle_reader_active is True  # held at drain time
        raise RuntimeError("reader blew up")

    monkeypatch.setattr(idle_reader, "_drain", _crashing_drain)

    await idle_reader.run_idle_reader("crash-me")  # must not raise

    assert state.idle_reader_active is False  # cleared despite the crash


# ─── _has_background_work bridges to the registry predicate ─────────────────


def test_has_background_work_reflects_registry():
    """The pool's busy predicate delegates to task_registry.has_active_background_agent."""
    pool = SessionPool(max_size=3)
    assert pool._has_background_work("none") is False

    _mark_busy("active")
    assert pool._has_background_work("active") is True

    task_registry.complete_task("active", "active-agent", "completed")
    assert pool._has_background_work("active") is False
