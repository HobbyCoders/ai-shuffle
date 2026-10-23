"""Agents that start and end leave nothing behind (#436).

Many concurrent agents took a 62 GB host down because what they started
outlived them: a closed CLI's descendants waited up to five minutes for the
resource monitor's reap pass, and idle CLIs past the warm pool size held their
subprocesses for the full cold timeout. These tests start and end fake agents
and check that processes, pool entries and asyncio tasks return to baseline.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import psutil
import pytest

from app.core import session_pool as sp
from app.core.session_pool import IdlePolicy, SessionPool, SessionState

POLICY = IdlePolicy(
    hot_size=2,
    hot_idle_seconds=1800,
    cold_idle_seconds=420,
    detached_grace_seconds=180,
    min_idle_floor_seconds=60,
    reevaluate_seconds=60,
)


def _state(*, idle_seconds: float = 0.0, pid=None, is_streaming: bool = False) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive.return_value = True
    adapter.is_connected.return_value = True
    adapter.subprocess_pid.return_value = pid
    adapter.disconnect = AsyncMock()
    return SessionState(
        adapter=adapter,
        is_connected=True,
        is_streaming=is_streaming,
        last_activity=datetime.now(timezone.utc) - timedelta(seconds=idle_seconds),
    )


def _pool(max_size: int, **sessions: SessionState) -> SessionPool:
    """Insertion order is LRU order (last inserted is the most recent)."""
    pool = SessionPool(max_size=max_size, idle_policy=POLICY)
    pool.set_attachment_probe(lambda _sid: None)
    for sid, state in sessions.items():
        pool._pool[sid] = state
    return pool


@pytest.fixture(autouse=True)
def _clean_reaper_registry():
    sp._terminated_trees.clear()
    yield
    sp._terminated_trees.clear()


# ─── Pool overrun: max_size is a warm-cache size, not an agent limit ─────────


@pytest.mark.asyncio
async def test_idle_sessions_past_warm_size_are_released_after_the_floor():
    """Busy agents grow the pool past max_size; once idle, the overflow goes.

    Before #436 an idle CLI ranked past the warm size kept its subprocess
    until the cold timeout (seven minutes), so a burst of agents left the pool
    at several times its configured size.
    """
    pool = _pool(
        2,
        a=_state(idle_seconds=90),
        b=_state(idle_seconds=90),
        c=_state(idle_seconds=90),
        d=_state(idle_seconds=90),
    )

    evicted = await pool.evict_idle()

    assert sorted(evicted) == ["a", "b"]
    assert list(pool._pool) == ["c", "d"]


@pytest.mark.asyncio
async def test_overflow_still_respects_the_activity_floor():
    pool = _pool(1, a=_state(idle_seconds=10), b=_state(idle_seconds=10))

    assert await pool.evict_idle() == []
    remaining = pool.idle_seconds_remaining("a")
    assert remaining is not None and 0 < remaining <= 60


@pytest.mark.asyncio
async def test_busy_agents_are_never_trimmed_and_count_toward_the_warm_size():
    """Running agents are never touched; an idle one behind them is released."""
    pool = _pool(
        2,
        idle=_state(idle_seconds=90),
        busy1=_state(idle_seconds=90, is_streaming=True),
        busy2=_state(idle_seconds=90, is_streaming=True),
    )

    assert await pool.evict_idle() == ["idle"]
    assert list(pool._pool) == ["busy1", "busy2"]


@pytest.mark.asyncio
async def test_pool_within_warm_size_keeps_the_hot_timeout():
    pool = _pool(4, a=_state(idle_seconds=90), b=_state(idle_seconds=90))

    assert await pool.evict_idle() == []


# ─── Reap on end: a closed agent's descendants go with it ───────────────────


_PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _spawn_agent_with_child() -> tuple[subprocess.Popen, int]:
    """A stand-in CLI that has started a long-running child of its own."""
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT], stdout=subprocess.PIPE, text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    return parent, child_pid


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.mark.asyncio
async def test_closing_a_session_reaps_its_cli_descendants_immediately():
    """The child the CLI left behind is gone when remove() returns.

    The fake disconnect kills only the CLI, as a clean CLI exit does: the
    model's backgrounded shell commands stay behind. Before #436 they ran on
    until the next resource-monitor pass (five minutes), or the next boot.
    """
    parent, child_pid = _spawn_agent_with_child()
    try:
        state = _state(pid=parent.pid)

        async def _disconnect(**_kwargs):
            parent.kill()
            parent.wait(timeout=10)

        state.adapter.disconnect = AsyncMock(side_effect=_disconnect)
        pool = _pool(4, agent=state)

        await pool.remove("agent")

        assert len(pool) == 0
        assert _wait_gone(child_pid), "the closed agent's child is still running"
        assert sp._terminated_trees == {}
    finally:
        if parent.poll() is None:
            parent.kill()
        try:
            psutil.Process(child_pid).kill()
        except psutil.NoSuchProcess:
            pass


@pytest.mark.asyncio
async def test_reap_never_touches_a_live_sessions_tree():
    """Only a closed CLI's tree is reaped; a still-running agent keeps its child."""
    parent, child_pid = _spawn_agent_with_child()
    try:
        # Recorded as if closed, but the CLI is still alive: nothing is signalled.
        reaped = await sp.reap_closed_subprocess(parent.pid, sp.snapshot_descendants(parent.pid))
        assert reaped == 0
        assert _alive(child_pid)
        assert parent.poll() is None
    finally:
        parent.kill()
        parent.wait(timeout=10)
        try:
            psutil.Process(child_pid).kill()
        except psutil.NoSuchProcess:
            pass


# ─── Many agents start and end: everything returns to baseline ──────────────


@pytest.mark.asyncio
async def test_agents_starting_and_ending_return_pool_and_tasks_to_baseline():
    pool = SessionPool(max_size=4, idle_policy=POLICY)
    pool.set_attachment_probe(lambda _sid: None)
    await asyncio.sleep(0)
    baseline_tasks = len(asyncio.all_tasks())

    for round_ in range(3):
        ids = [f"agent-{round_}-{i}" for i in range(12)]
        for sid in ids:
            await pool.evict_if_needed()
            pool.put(sid, _state(is_streaming=True))
        assert len(pool) == 12  # busy agents are never refused or evicted
        for sid in ids:
            pool._pool[sid].is_streaming = False
            await pool.remove(sid)
        assert len(pool) == 0

    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == baseline_tasks
    assert sp._terminated_trees == {}


# ─── Memory pressure is visible before the kernel acts ──────────────────────


def test_low_host_memory_is_warned_before_the_admission_floor(monkeypatch):
    from app.core.config import settings
    from app.core.resource_monitor import _check_warnings

    monkeypatch.setattr(settings, "sdk_admission_min_available_mb", 1536)

    low = _check_warnings({"host_available_mb": 2500})
    assert any("Host memory low: 2500MB available" in w for w in low)
    assert not any("Host memory low" in w for w in _check_warnings({"host_available_mb": 40000}))
    assert not any("Host memory low" in w for w in _check_warnings({}))


# ─── No per-worktree daemons: the app never enables core.fsmonitor ──────────


def test_opening_a_repository_does_not_enable_fsmonitor(tmp_path):
    """libgit2 ignores core.fsmonitor, but with it set every git CLI call in
    every worktree started a detached fsmonitor daemon that never exited
    (228 of them, about 9 GB, on a Windows host running ten agents)."""
    pygit2 = pytest.importorskip("pygit2")
    from app.core import git_native

    pygit2.init_repository(str(tmp_path))

    repo = git_native._get_repo(str(tmp_path))

    assert repo is not None
    with pytest.raises(KeyError):
        pygit2.Repository(str(tmp_path)).config["core.fsmonitor"]
