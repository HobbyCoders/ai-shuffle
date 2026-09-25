"""The dead-tree reaper never signals a recycled pid (security S-6).

Descendant pids sit in ``_terminated_trees`` for up to a resource-monitor
interval. If one exits and the kernel hands its pid to an unrelated app child
(a hook shell, git, an MCP server), the reaper used to SIGTERM/SIGKILL it. The
snapshot now carries each process's start time and the reaper skips any pid
whose current start time differs by a second or more (a clock step shifts
psutil's create_time slightly, so exact equality would disable the guard), or
whose start time cannot be read.

The psutil work runs in a worker thread so ``wait_procs`` never stalls the loop.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest

import app.core.session_pool as sp


class _FakeProc:
    def __init__(self, pid, created, ppid=1):
        self.pid = pid
        self._created = created
        self._ppid = ppid
        self.terminated = False
        self.killed = False

    def create_time(self):
        return self._created

    def ppid(self):
        return self._ppid

    def children(self, recursive=False):
        return list(_FAKE_CHILDREN)

    def is_running(self):
        return True

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


_FAKE_TABLE: dict = {}
_FAKE_CHILDREN: list = []


def _fake_psutil():
    mod = types.ModuleType("psutil")

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def process(pid):
        if pid not in _FAKE_TABLE:
            raise NoSuchProcess(pid)
        return _FAKE_TABLE[pid]

    mod.NoSuchProcess = NoSuchProcess
    mod.AccessDenied = AccessDenied
    mod.Process = process
    mod.pid_exists = lambda pid: pid in _FAKE_TABLE
    mod.wait_procs = lambda procs, timeout=None: (list(procs), [])
    return mod


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FAKE_TABLE.clear()
    _FAKE_CHILDREN.clear()
    sp._terminated_trees.clear()
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil())
    monkeypatch.setattr(sp, "_session_pool", None, raising=False)
    yield
    sp._terminated_trees.clear()


def test_snapshot_records_start_times():
    _FAKE_TABLE[100] = _FakeProc(100, 1.0)
    _FAKE_CHILDREN.extend([_FakeProc(101, 11.0), _FakeProc(102, 12.0)])
    assert sp.snapshot_descendants(100) == [(101, 11.0), (102, 12.0)]


async def test_a_recycled_pid_is_not_killed():
    # The pool snapshots the CLI's tree while it is alive, as evict/remove do.
    _FAKE_TABLE[500] = _FakeProc(500, 5.0)
    _FAKE_CHILDREN.append(_FakeProc(501, 50.0))
    sp.record_terminated_subprocess(500, sp.snapshot_descendants(500))
    del _FAKE_TABLE[500]
    # 501 died and the kernel gave the pid to an unrelated, newer process.
    stranger = _FakeProc(501, 90.0)
    _FAKE_TABLE[501] = stranger
    assert await sp.reap_terminated_process_trees() == 0
    assert not stranger.terminated and not stranger.killed
    assert 500 not in sp._terminated_trees


async def test_the_original_orphan_is_still_reaped():
    orphan = _FakeProc(601, 60.0)
    _FAKE_TABLE[601] = orphan
    sp.record_terminated_subprocess(600, descendants=[(601, 60.0)])
    assert await sp.reap_terminated_process_trees() == 1
    assert orphan.terminated


def test_bare_pids_are_stamped_at_record_time():
    _FAKE_TABLE[701] = _FakeProc(701, 70.0)
    sp.record_terminated_subprocess(700, descendants=[701, 702])  # 702 is gone
    assert sp._terminated_trees[700] == [(701, 70.0)]


async def test_a_small_clock_step_still_reaps_the_orphan():
    # A wall-clock step shifts psutil's boot-time-derived create_time a little.
    orphan = _FakeProc(801, 80.4)
    _FAKE_TABLE[801] = orphan
    sp.record_terminated_subprocess(800, descendants=[(801, 80.0)])
    assert await sp.reap_terminated_process_trees() == 1
    assert orphan.terminated


async def test_an_unreadable_start_time_skips_the_kill():
    class _Unreadable(_FakeProc):
        def create_time(self):
            raise sys.modules["psutil"].AccessDenied(self.pid)

    proc = _Unreadable(901, 90.0)
    _FAKE_TABLE[901] = proc
    sp.record_terminated_subprocess(900, descendants=[(901, 90.0)])
    assert await sp.reap_terminated_process_trees() == 0
    assert not proc.terminated and not proc.killed
    assert 900 not in sp._terminated_trees


async def test_an_incomparable_start_time_skips_the_kill():
    proc = _FakeProc(951, float("nan"))
    _FAKE_TABLE[951] = proc
    sp.record_terminated_subprocess(950, descendants=[(951, 95.0)])
    assert await sp.reap_terminated_process_trees() == 0
    assert not proc.terminated


async def test_wait_procs_does_not_block_the_event_loop():
    """A slow ``wait_procs`` (up to 2 s in production) runs off the loop."""
    orphan = _FakeProc(1001, 100.0)
    _FAKE_TABLE[1001] = orphan
    sp.record_terminated_subprocess(1000, descendants=[(1001, 100.0)])

    release = threading.Event()
    waiting = threading.Event()

    def _slow_wait_procs(procs, timeout=None):
        waiting.set()
        assert release.wait(5), "the loop never got to run while wait_procs slept"
        return list(procs), []

    sys.modules["psutil"].wait_procs = _slow_wait_procs

    reap = asyncio.create_task(sp.reap_terminated_process_trees())
    # This coroutine keeps running while wait_procs sleeps, and it is the only
    # thing that releases it: on the loop, the reaper would block until the
    # 5 s timeout and fail.
    for _ in range(500):
        if waiting.is_set():
            break
        await asyncio.sleep(0.01)
    assert waiting.is_set() and not reap.done()
    release.set()
    assert await asyncio.wait_for(reap, 5) == 1
    assert orphan.terminated
