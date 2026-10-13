"""
Tests for the observe-only resource monitor / session-pool watchdog fix.

Background (the production bug being fixed)
-------------------------------------------
The ResourceMonitor previously ran a wall-clock "stuck streaming" watchdog that
force-terminated LIVE sessions on a timer. Overnight it fired on a legitimately
long-running orchestrator driving 30+ subagents and:
  1. SIGTERM'd the SDK subprocess mid-run,
  2. discarded the 161-message in-memory transcript + truncated the PG mirror,
  3. orphaned the subagent child-process trees (the leak it exists to detect).

This suite verifies the three pillars of the fix:
  (a) a long-streaming LIVE session is NOT evicted by the monitor,
  (b) a TERMINATED session's orphaned descendant tree IS reaped,
  (c) admission backpressure refuses NEW sessions past the ceiling.

No Postgres or live SDK is needed — the adapter is mocked, and the reaper test
spawns real throwaway child processes so the reaping is genuinely exercised.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import app.core.session_pool as sp
from app.core.session_pool import SessionPool, SessionState


# ─── Helpers ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("pressure", [None, {"connections_opened": 8, "waiting": 72,
                                           "hold_seconds_total": 1.25}])
async def test_collect_metrics_includes_db_pressure(monkeypatch, pressure):
    import app.db.base as base
    from app.core import resource_monitor as rm

    monkeypatch.setattr(base, "pool_stats", lambda: {"in_use": 8, "idle": 0, "max": 8})
    monkeypatch.setattr(base, "pressure_stats", lambda: pressure)
    metrics = await rm._collect_metrics()
    assert metrics["db_pool_in_use"] == 8
    assert metrics["db_pool_max"] == 8
    if pressure is None:
        assert "db_pool_waiting" not in metrics
    else:
        for key, value in pressure.items():
            assert metrics["db_pool_" + key] == value


def _make_state(*, is_streaming: bool, streaming_age_seconds: float = 0.0,
                connected: bool = True, alive: bool = True) -> SessionState:
    """Build a SessionState backed by a mock adapter.

    The mock adapter reports is_alive()/is_connected() and a None
    subprocess_pid() (so no real process is touched unless a test wires one up
    explicitly).
    """
    adapter = MagicMock()
    adapter.is_alive.return_value = alive
    adapter.is_connected.return_value = connected
    adapter.subprocess_pid.return_value = None

    started = datetime.now(timezone.utc) - timedelta(seconds=streaming_age_seconds)
    return SessionState(
        adapter=adapter,
        is_connected=connected,
        is_streaming=is_streaming,
        streaming_started_at=started if is_streaming else None,
        last_activity=started,
    )


@pytest.fixture(autouse=True)
def _clear_reaper_registry():
    """Keep the module-level terminated-tree registry isolated per test."""
    sp._terminated_trees.clear()
    yield
    sp._terminated_trees.clear()


@pytest.fixture(autouse=True)
def _fake_healthy_host_memory(monkeypatch):
    """admission_check() reads REAL host MemAvailable via _host_available_mb(),
    so on a loaded host every admission test spuriously refuses with "host low
    on memory". Fake a healthy reading so outcomes depend only on the state each
    test sets up; the low-memory refusal path is exercised explicitly in
    test_admission_refuses_when_host_memory_low with a faked low reading.
    """
    monkeypatch.setattr(sp, "_host_available_mb", lambda: 8192.0)


# ─── (a) Long-streaming LIVE session is NOT evicted ─────────────────────────


@pytest.mark.asyncio
async def test_long_streaming_session_is_reported_not_evicted():
    """report_long_streaming OBSERVES but never removes a live streaming session."""
    pool = SessionPool(max_size=3)
    # 1 hour of continuous streaming — would have tripped the old 30-min watchdog.
    state = _make_state(is_streaming=True, streaming_age_seconds=3600)
    pool.put("ses-orchestrator", state)

    report = pool.report_long_streaming(warn_after_seconds=600)

    # It is reported...
    assert report == [("ses-orchestrator", pytest.approx(3600, abs=5))]
    # ...but NOT evicted: still in the pool, subprocess untouched.
    assert "ses-orchestrator" in pool
    assert len(pool) == 1
    state.adapter.disconnect.assert_not_called()


def test_evict_stuck_streaming_method_is_gone():
    """The destructive timer-based eviction method must no longer exist."""
    pool = SessionPool(max_size=3)
    assert not hasattr(pool, "evict_stuck_streaming")
    assert hasattr(pool, "report_long_streaming")


@pytest.mark.asyncio
async def test_short_streaming_session_not_reported():
    """A session under the warn threshold is not reported at all."""
    pool = SessionPool(max_size=3)
    pool.put("ses-quick", _make_state(is_streaming=True, streaming_age_seconds=30))
    assert pool.report_long_streaming(warn_after_seconds=600) == []


@pytest.mark.asyncio
async def test_periodic_cleanup_does_not_evict_long_streaming(monkeypatch):
    """The monitor's periodic cleanup leaves a long-streaming live session intact."""
    import app.core.resource_monitor as rm

    pool = SessionPool(max_size=3)
    pool.put("ses-orchestrator", _make_state(is_streaming=True, streaming_age_seconds=7200))

    # Point the monitor at our pool instance.
    monkeypatch.setattr(sp, "_session_pool", pool, raising=False)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool, raising=False)

    cleaned = await rm._periodic_cleanup()

    # Session survived the cleanup sweep.
    assert "ses-orchestrator" in pool
    # And cleanup did NOT record any stuck-streaming eviction.
    assert "stuck_streaming_sessions" not in cleaned
    # It WAS observed.
    assert cleaned.get("long_streaming_observed") == 1


# ─── (b) Terminated session's orphan tree IS reaped ─────────────────────────


def _spawn_sleeper() -> subprocess.Popen:
    """Spawn a throwaway child process that sleeps (so it stays alive to be reaped)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_reaper_kills_recorded_orphan_descendants():
    """A recorded terminated-subprocess descendant that is still alive gets reaped."""
    psutil = pytest.importorskip("psutil")

    # Spawn two real child processes and pretend they were descendants of a
    # now-terminated SDK subprocess whose PID never existed (so it counts as
    # "already terminated"). Use a PID that is not running.
    child_a = _spawn_sleeper()
    child_b = _spawn_sleeper()
    try:
        assert psutil.pid_exists(child_a.pid)
        assert psutil.pid_exists(child_b.pid)

        # A PID that is (almost certainly) not a running process — stands in for
        # the already-terminated parent SDK subprocess.
        dead_parent_pid = 999_999_999
        assert not psutil.pid_exists(dead_parent_pid)

        sp.record_terminated_subprocess(
            dead_parent_pid, descendants=[child_a.pid, child_b.pid]
        )
        assert dead_parent_pid in sp._terminated_trees

        reaped = await sp.reap_terminated_process_trees()

        assert reaped == 2
        # Registry entry consumed.
        assert dead_parent_pid not in sp._terminated_trees
        # Both children actually died.
        child_a.wait(timeout=5)
        child_b.wait(timeout=5)
        assert child_a.poll() is not None
        assert child_b.poll() is not None
    finally:
        for c in (child_a, child_b):
            if c.poll() is None:
                c.kill()
                c.wait(timeout=5)


async def test_reaper_skips_descendant_of_live_session(monkeypatch):
    """A descendant that belongs to a still-LIVE SDK subprocess must NOT be reaped."""
    pytest.importorskip("psutil")

    child = _spawn_sleeper()
    try:
        # Make the live-session guard claim that `child` descends from a live
        # subprocess by reporting the child's OWN parent (our test process) as a
        # live SDK subprocess PID — _has_live_protected_ancestor walks ppid up to
        # our server PID and will cross our own PID.
        live_state = _make_state(is_streaming=True, alive=True)
        pool = SessionPool(max_size=3)
        pool.put("ses-live", live_state)
        monkeypatch.setattr(sp, "_session_pool", pool, raising=False)
        # The child's immediate parent is THIS python process (os.getpid()).
        monkeypatch.setattr(sp, "subprocess_pid", lambda st: os.getpid())

        sp.record_terminated_subprocess(999_999_998, descendants=[child.pid])
        reaped = await sp.reap_terminated_process_trees()

        # Protected: not reaped.
        assert reaped == 0
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


async def test_reaper_noop_when_registry_empty():
    """Reaper is a safe no-op with nothing recorded."""
    assert await sp.reap_terminated_process_trees() == 0


def test_record_terminated_subprocess_is_bounded():
    """The terminated-tree registry never grows unbounded."""
    for i in range(sp._TERMINATED_TREES_MAX + 50):
        # Use a fake descendant list so it actually records.
        sp.record_terminated_subprocess(1_000_000 + i, descendants=[(2_000_000 + i, 0.0)])
    assert len(sp._terminated_trees) == sp._TERMINATED_TREES_MAX


# ─── (c) Admission backpressure refuses past the ceiling ────────────────────


def test_admission_allows_below_capacity():
    pool = SessionPool(max_size=3)
    pool.put("ses-1", _make_state(is_streaming=True))
    assert pool.admission_check() is None


def test_admission_has_no_session_count_ceiling(monkeypatch):
    """Many busy subprocess sessions never refuse on count alone.

    The old hard cap (2x the warm size) refused the ninth Claude session on a
    default box and made /health/ready fail; only real resource guards refuse.
    """
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "sdk_admission_min_available_mb", 0, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_max_child_processes", 0, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_admission_rss_mb", 0, raising=False)
    pool = SessionPool(max_size=2)
    for i in range(50):
        pool.put(f"ses-{i}", _make_state(is_streaming=True))

    assert pool.delegated_size == 50
    assert not hasattr(pool, "hard_cap")
    assert pool.admission_check() is None


def _native_state(**kwargs) -> SessionState:
    """A pooled session on a native in-process leg (no CLI subprocess)."""
    state = _make_state(**kwargs)
    state.adapter.provider_kind = "openai_responses"
    return state


def test_native_sessions_are_counted_on_their_own_lane():
    """Native sessions own no subprocess and never count as delegated."""
    pool = SessionPool(max_size=2)
    for i in range(8):
        pool.put(f"native-{i}", _native_state(is_streaming=True))

    assert pool.native_size == 8
    assert pool.delegated_size == 0
    assert pool.admission_check() is None
    assert pool.admission_check("native") is None


def test_native_sessions_are_not_counted_by_a_delegated_admission():
    """A delegated session is admitted while natives fill the pool."""
    pool = SessionPool(max_size=2)
    for i in range(6):
        pool.put(f"native-{i}", _native_state(is_streaming=True))
    pool.put("ses-cli", _make_state(is_streaming=True))

    assert pool.lane_counts() == (1, 6)
    assert pool.admission_check() is None


def test_rss_ceiling_refuses_a_delegated_admission(monkeypatch):
    """Real resource pressure is what refuses a delegated session now."""
    pytest.importorskip("psutil")
    from app.core import config as cfg

    pool = SessionPool(max_size=2)
    monkeypatch.setattr(cfg.settings, "sdk_max_child_processes", 0, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_admission_rss_mb", 1, raising=False)
    pool.put("ses-0", _make_state(is_streaming=True))

    refusal = pool.admission_check()
    assert refusal is not None
    assert "tree RSS" in refusal


def test_native_soft_cap_refuses_when_configured(monkeypatch):
    """The native safety valve is the only pool-count ceiling natives meet."""
    from app.core import config as cfg

    pool = SessionPool(max_size=2)
    monkeypatch.setattr(cfg.settings, "sdk_native_session_soft_cap", 3, raising=False)
    for i in range(3):
        pool.put(f"native-{i}", _native_state(is_streaming=True))

    refusal = pool.admission_check("native")
    assert refusal is not None
    assert "native in-process sessions" in refusal
    assert "ceiling 3" in refusal
    # A delegated admission is untouched by it.
    assert pool.admission_check() is None


def test_rss_ceiling_still_refuses_a_native_admission(monkeypatch):
    """The real resource guard applies to every lane, and says RSS."""
    pytest.importorskip("psutil")
    from app.core import config as cfg

    pool = SessionPool(max_size=10)
    monkeypatch.setattr(cfg.settings, "sdk_max_child_processes", 0, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_admission_rss_mb", 1, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_native_session_soft_cap", 0, raising=False)
    pool.put("native-0", _native_state(is_streaming=True))

    refusal = pool.admission_check("native")
    assert refusal is not None
    assert "tree RSS" in refusal and "ceiling 1MB" in refusal


def test_admission_snapshot_reports_lane_counts():
    pool = SessionPool(max_size=2)
    for i in range(3):
        pool.put(f"ses-{i}", _make_state(is_streaming=True))
    for i in range(5):
        pool.put(f"native-{i}", _native_state(is_streaming=True))

    snap = pool.admission_snapshot()
    assert snap.lane == "delegated"
    assert (snap.delegated_in_pool, snap.native_in_pool) == (3, 5)
    assert snap.in_pool == 8
    assert snap.refusal is None

    native_snap = pool.admission_snapshot("native")
    assert native_snap.lane == "native" and native_snap.refusal is None


def test_admission_refuses_at_child_process_ceiling(monkeypatch):
    """With a configured child-process ceiling, admission refuses when exceeded."""
    pytest.importorskip("psutil")
    from app.core import config as cfg

    pool = SessionPool(max_size=10)  # well below hard cap so only the ceiling bites

    # Configure a tiny child-process ceiling.
    monkeypatch.setattr(cfg.settings, "sdk_max_child_processes", 1, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_admission_rss_mb", 0, raising=False)

    # Spawn a real child so the live process actually has >= 1 descendant.
    child = _spawn_sleeper()
    try:
        refusal = pool.admission_check()
        assert refusal is not None
        assert "descendant processes" in refusal
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_admission_refuses_when_host_memory_low(monkeypatch):
    """When faked host MemAvailable is below the configured floor, admission
    refuses with a 'host low on memory' reason — no real /proc/meminfo read."""
    from app.core import config as cfg

    pool = SessionPool(max_size=10)  # well below hard cap so only the floor bites
    monkeypatch.setattr(
        cfg.settings, "sdk_admission_min_available_mb", 1536, raising=False
    )
    monkeypatch.setattr(sp, "_host_available_mb", lambda: 512.0)

    refusal = pool.admission_check()
    assert refusal is not None
    assert "host low on memory" in refusal
    assert "512MB available" in refusal
    assert "floor 1536MB" in refusal


def test_admission_ceilings_disabled_by_default(monkeypatch):
    """With ceilings at 0 (default), only the pool hard cap can refuse."""
    from app.core import config as cfg

    pool = SessionPool(max_size=10)
    monkeypatch.setattr(cfg.settings, "sdk_max_child_processes", 0, raising=False)
    monkeypatch.setattr(cfg.settings, "sdk_admission_rss_mb", 0, raising=False)
    pool.put("ses-1", _make_state(is_streaming=True))
    assert pool.admission_check() is None


# ─── (d) Pool-eviction → cleanup wiring (replaced the 30s timer) ────────────


@pytest.mark.asyncio
async def test_start_resource_monitor_registers_eviction_callback(monkeypatch):
    """start_resource_monitor wires _on_pool_eviction onto the pool so LRU/idle
    eviction drives live-store + registry cleanup (the deleted timer's job)."""
    import app.core.resource_monitor as rm

    pool = SessionPool(max_size=3)
    monkeypatch.setattr(sp, "get_session_pool", lambda: pool, raising=False)

    task = await rm.start_resource_monitor(interval_seconds=3600)
    try:
        assert pool._eviction_callback is rm._on_pool_eviction
    finally:
        await rm.stop_resource_monitor(task)


@pytest.mark.asyncio
async def test_on_pool_eviction_delegates_to_cleanup(monkeypatch):
    """The registered callback forwards (session_id, reason) into the shared
    _cleanup_evicted_sessions routine as a single-element batch."""
    import app.core.resource_monitor as rm

    calls = []

    async def _fake_cleanup(session_ids, cleaned, reason):
        calls.append((list(session_ids), reason))

    monkeypatch.setattr(rm, "_cleanup_evicted_sessions", _fake_cleanup)

    await rm._on_pool_eviction("ses-evicted", "idle")

    assert calls == [(["ses-evicted"], "idle")]


# ─── (d) The subprocess-leak warning measures session CLIs only (W4-C) ─────


def test_descendants_of_running_sessions_are_not_a_leak():
    """26 descendants for 7 delegated sessions is normal: each CLI owns its own
    children, and postgres, tunnels and tool shells sit in the same tree."""
    from app.core.resource_monitor import _check_warnings

    warnings = _check_warnings({"child_processes": 26, "pool_delegated": 7,
                                "session_cli_processes": 7, "session_cli_untracked": 0})
    assert not any("leak" in w for w in warnings)


def test_a_session_cli_no_pooled_session_owns_is_a_leak():
    from app.core.resource_monitor import _check_warnings

    warnings = _check_warnings({"child_processes": 3, "pool_delegated": 2,
                                "session_cli_processes": 3, "session_cli_untracked": 1})
    assert warnings == [
        "1 session CLI subprocess(es) not owned by any pooled session - possible subprocess leak"
    ]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process name from exec path")
def test_untracked_session_clis_counts_only_unowned_cli_children(tmp_path, monkeypatch):
    """Real processes: a CLI-named child is counted, an owned one is not a leak,
    and an unrelated child (a tool shell, a tunnel) is not counted at all."""
    pytest.importorskip("psutil")
    from app.core import resource_monitor as rm

    sleep = "/bin/sleep" if os.path.exists("/bin/sleep") else "/usr/bin/sleep"
    cli = tmp_path / "claude"
    cli.symlink_to(sleep)
    procs = [subprocess.Popen([str(cli), "30"]), subprocess.Popen([str(cli), "30"]),
             subprocess.Popen([sleep, "30"])]
    try:
        monkeypatch.setattr(rm, "_UNTRACKED_CLI_MIN_AGE_SECONDS", 0.0)
        assert rm._untracked_session_clis({procs[0].pid}) == (2, 1)
        assert rm._untracked_session_clis({procs[0].pid, procs[1].pid}) == (2, 0)
        # A CLI still inside its connect window is not reported yet.
        monkeypatch.setattr(rm, "_UNTRACKED_CLI_MIN_AGE_SECONDS", 3600.0)
        assert rm._untracked_session_clis(set()) == (2, 0)
    finally:
        for p in procs:
            p.kill()
            p.wait()
