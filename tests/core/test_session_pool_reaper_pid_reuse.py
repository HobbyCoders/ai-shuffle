"""The dead-tree reaper never signals a recycled pid (security S-6).

Descendant pids sit in ``_terminated_trees`` for up to a resource-monitor
interval. If one exits and the kernel hands its pid to an unrelated app child
(a hook shell, git, an MCP server), the reaper used to SIGTERM/SIGKILL it. The
snapshot now carries each process's start time and the reaper skips any pid
whose current start time differs.
"""

from __future__ import annotations

import sys
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


def test_a_recycled_pid_is_not_killed():
    # The pool snapshots the CLI's tree while it is alive, as evict/remove do.
    _FAKE_TABLE[500] = _FakeProc(500, 5.0)
    _FAKE_CHILDREN.append(_FakeProc(501, 50.0))
    sp.record_terminated_subprocess(500, sp.snapshot_descendants(500))
    del _FAKE_TABLE[500]
    # 501 died and the kernel gave the pid to an unrelated, newer process.
    stranger = _FakeProc(501, 90.0)
    _FAKE_TABLE[501] = stranger
    assert sp.reap_terminated_process_trees() == 0
    assert not stranger.terminated and not stranger.killed
    assert 500 not in sp._terminated_trees


def test_the_original_orphan_is_still_reaped():
    orphan = _FakeProc(601, 60.0)
    _FAKE_TABLE[601] = orphan
    sp.record_terminated_subprocess(600, descendants=[(601, 60.0)])
    assert sp.reap_terminated_process_trees() == 1
    assert orphan.terminated


def test_bare_pids_are_stamped_at_record_time():
    _FAKE_TABLE[701] = _FakeProc(701, 70.0)
    sp.record_terminated_subprocess(700, descendants=[701, 702])  # 702 is gone
    assert sp._terminated_trees[700] == [(701, 70.0)]
