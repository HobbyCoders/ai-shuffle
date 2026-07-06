"""
Concurrency-safety tests for the SessionPool (prod-readiness Phase 2.3).

Two hazards are covered:

  1. "dictionary changed size during iteration": mutation methods (remove,
     remove_dead, remove_broken, evict_dead) pop entries from the live pool
     dict WITHOUT holding `_eviction_lock`, while eviction sweeps iterate that
     same dict. All sweep iterations must run over snapshots so a concurrent
     removal can never raise RuntimeError.

  2. Lock held across slow teardown: `evict_if_needed` used to hold the
     (non-reentrant) `_eviction_lock` across graceful_disconnect — up to
     several seconds of subprocess exit-grace — stalling every new-session
     admission behind it. The lock must now cover ONLY victim selection +
     dict removal; teardown and the eviction callback run after release.

No Postgres / SDK needed — adapters are mocked and disconnect is an AsyncMock,
matching tests/core/test_session_pool_eviction_guard.py.
"""

import asyncio
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

    The adapter's subprocess PID resolves to None (sdk_client=None), so no real
    process is ever touched. `idle_seconds` backdates last_activity so
    evict_idle can be exercised without sleeping.
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


@pytest.fixture(autouse=True)
def _isolate_registry_and_reaper():
    """Keep the global registry + reaper tree isolated per test."""
    task_registry.reset_all()
    sp._terminated_trees.clear()
    yield
    task_registry.reset_all()
    sp._terminated_trees.clear()


# ─── (2) Eviction lock is NOT held across subprocess teardown ───────────────


@pytest.mark.asyncio
async def test_eviction_lock_released_before_slow_teardown():
    """While the evicted session's disconnect is still in flight, the lock must
    already be free and the pool slot already vacated — new-session admission
    is never stalled behind a multi-second teardown."""
    pool = SessionPool(max_size=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_disconnect():
        started.set()
        await release.wait()

    slow = _make_state()
    slow.adapter.disconnect = AsyncMock(side_effect=_slow_disconnect)
    pool.put("slow", slow)

    t1 = asyncio.create_task(pool.evict_if_needed())
    await asyncio.wait_for(started.wait(), timeout=2)

    # Teardown in flight: the entry is already out of the pool and the lock is
    # free. (Under the old code the lock stayed held here and this failed.)
    assert "slow" not in pool
    assert not pool._eviction_lock.locked()

    # A concurrent eviction pass acquires the lock and returns immediately
    # (pool below capacity) — it must not wait for the slow disconnect.
    assert await asyncio.wait_for(pool.evict_if_needed(), timeout=1) is None

    # A FULL eviction cycle for another session also completes while the slow
    # teardown is still blocked.
    pool.put("fast", _make_state())
    assert await asyncio.wait_for(pool.evict_if_needed(), timeout=1) == "fast"

    release.set()
    # Per-caller semantics unchanged: the original call still returns the
    # evicted id only after its teardown completed.
    assert await asyncio.wait_for(t1, timeout=2) == "slow"
    slow.adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_eviction_callback_runs_outside_lock():
    """A callback that re-enters the pool (calls evict_if_needed) must not
    deadlock on the non-reentrant lock."""
    pool = SessionPool(max_size=1)
    reentered: list[object] = []

    async def _reentrant_cb(sid: str, reason: str) -> None:
        # Re-enter the pool's locked path from inside the eviction callback.
        reentered.append(await asyncio.wait_for(pool.evict_if_needed(), timeout=1))

    pool.set_eviction_callback(_reentrant_cb)
    pool.put("a", _make_state())

    evicted = await asyncio.wait_for(pool.evict_if_needed(), timeout=2)
    assert evicted == "a"
    assert reentered == [None]  # callback ran and re-acquired the lock fine


# ─── (1) Iteration vs concurrent mutation ───────────────────────────────────


@pytest.mark.asyncio
async def test_evict_if_needed_scan_survives_mutation_during_iteration(monkeypatch):
    """Deterministic regression for the 'dict changed size during iteration'
    hazard: the busy-check mutates the pool mid-scan (simulating a concurrently
    scheduled removal). The snapshot iteration must neither raise RuntimeError
    nor evict a wrong entry."""
    pool = SessionPool(max_size=2)
    for i in range(4):
        pool.put(f"s{i}", _make_state())

    calls: list[str] = []
    original = pool._has_background_work

    def _mutating_busy_check(session_id: str) -> bool:
        calls.append(session_id)
        if session_id == "s0":
            # A "concurrent" removal deletes a LATER entry while the scan is
            # mid-iteration over the pool dict.
            pool._pool.pop("s3", None)
            return True  # keep scanning past s0
        return original(session_id)

    monkeypatch.setattr(pool, "_has_background_work", _mutating_busy_check)

    evicted = await pool.evict_if_needed()  # old code: RuntimeError here

    assert evicted == "s1"  # s0 busy, s1 next LRU candidate
    assert "s3" not in pool
    assert set(pool._pool) == {"s0", "s2"}


@pytest.mark.asyncio
async def test_items_returns_snapshot_safe_to_iterate_across_mutation():
    """pool.items() hands out a snapshot: iterating it after the pool is
    mutated (or across awaits) can never raise RuntimeError."""
    pool = SessionPool(max_size=3)
    pool.put("a", _make_state())
    pool.put("b", _make_state())

    snapshot = pool.items()
    for sid, _state in snapshot:
        # Mutate the pool mid-iteration — live-view iteration would raise.
        pool._pool.pop("b", None)
    assert [sid for sid, _ in snapshot] == ["a", "b"]


@pytest.mark.asyncio
async def test_reap_terminated_trees_snapshots_pool(monkeypatch):
    """reap_terminated_process_trees iterates the pool to build the live-pid
    protection set; a mutation during that walk must not raise (a partial walk
    could otherwise mis-classify a live session's descendants)."""
    pool = SessionPool(max_size=3)
    pool.put("x", _make_state())
    pool.put("y", _make_state())
    monkeypatch.setattr(sp, "_session_pool", pool)

    pid_calls: list[object] = []

    def _mutating_pid(state):
        pid_calls.append(state)
        pool._pool.pop("y", None)  # concurrent removal mid-walk
        return None

    monkeypatch.setattr(sp, "subprocess_pid", _mutating_pid)
    sp._terminated_trees[99999] = [99998]  # non-empty so the walk runs

    # Must not raise despite the pool shrinking during the values() walk.
    sp.reap_terminated_process_trees()

    # The walk visited BOTH entries. Under the old live-view iteration the
    # RuntimeError was swallowed by the surrounding try/except, silently
    # truncating the live-pid protection set after the first entry.
    assert len(pid_calls) == 2


# ─── Concurrent sweeps + removals: no errors, no double teardown ────────────


@pytest.mark.asyncio
async def test_concurrent_evict_and_remove_same_session_single_teardown():
    """evict_if_needed racing remove() on the same session: dict pop is the
    arbiter, so exactly one side tears the session down."""
    pool = SessionPool(max_size=1)
    state = _make_state()

    async def _yielding_disconnect():
        await asyncio.sleep(0)

    state.adapter.disconnect = AsyncMock(side_effect=_yielding_disconnect)
    pool.put("contested", state)

    results = await asyncio.gather(
        pool.evict_if_needed(),
        pool.remove("contested"),
        return_exceptions=True,
    )

    assert not any(isinstance(r, Exception) for r in results), results
    assert "contested" not in pool
    # Exactly one of the two paths won the pop and disconnected.
    assert state.adapter.disconnect.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_sweeps_and_removals_stress():
    """Drive every sweep (evict_if_needed / evict_idle / evict_dead) against
    every mutation path (remove / remove_dead / remove_broken) concurrently.
    Asserts: no exception of any kind (esp. RuntimeError), the pool drains to a
    consistent state, and no session is torn down twice."""
    pool = SessionPool(max_size=2)
    states: dict[str, SessionState] = {}
    for i in range(8):
        # All long-idle so evict_idle also has candidates; s6/s7 have dead
        # subprocesses so evict_dead has candidates too.
        st = _make_state(idle_seconds=7200, alive=(i < 6))

        async def _yielding_disconnect():
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        st.adapter.disconnect = AsyncMock(side_effect=_yielding_disconnect)
        states[f"s{i}"] = st
        pool.put(f"s{i}", st)

    callback_hits: list[tuple[str, str]] = []

    async def _cb(sid: str, reason: str) -> None:
        await asyncio.sleep(0)  # widen the interleaving window
        callback_hits.append((sid, reason))

    pool.set_eviction_callback(_cb)

    results = await asyncio.gather(
        pool.evict_if_needed(),
        pool.evict_idle(max_idle_seconds=600),
        pool.evict_dead(),
        pool.remove("s2"),
        pool.remove_dead("s4"),
        pool.remove_broken("s5"),
        pool.evict_if_needed(),
        pool.remove("s0"),
        return_exceptions=True,
    )

    assert not any(isinstance(r, Exception) for r in results), results

    # Let remove_broken's background teardown finish before counting.
    for task in list(pool._background_teardowns):
        await task

    # Every session left the pool exactly once; none was disconnected twice.
    assert len(pool) == 0
    for sid, st in states.items():
        assert st.adapter.disconnect.await_count <= 1, (
            f"{sid} disconnected {st.adapter.disconnect.await_count} times"
        )
    # The eviction callback fired at most once per session.
    seen_ids = [sid for sid, _ in callback_hits]
    assert len(seen_ids) == len(set(seen_ids)), callback_hits


@pytest.mark.asyncio
async def test_parallel_evict_if_needed_calls_are_serialized_and_safe():
    """Many simultaneous evict_if_needed calls (one per admission) never evict
    the same session twice and never error."""
    pool = SessionPool(max_size=1)
    states = {}
    for i in range(4):
        st = _make_state()

        async def _yielding_disconnect():
            await asyncio.sleep(0)

        st.adapter.disconnect = AsyncMock(side_effect=_yielding_disconnect)
        states[f"p{i}"] = st
        pool.put(f"p{i}", st)

    results = await asyncio.gather(
        *[pool.evict_if_needed() for _ in range(6)], return_exceptions=True
    )

    assert not any(isinstance(r, Exception) for r in results), results
    evicted = [r for r in results if isinstance(r, str)]
    assert len(evicted) == len(set(evicted))  # no session evicted twice
    for sid, st in states.items():
        assert st.adapter.disconnect.await_count <= 1
