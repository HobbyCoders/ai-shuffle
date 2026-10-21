"""Tests for the machine-sized ``pytest -n`` gate in tests/_xdist_gate.py.

Every test uses a lock file under ``tmp_path``, never the machine-wide one, so
these pass while a real gated run holds that lock.
"""

import io
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import _xdist_gate
from tests._xdist_gate import XdistGate

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeConfig:
    def __init__(self, numprocesses=None, worker=False):
        self._numprocesses = numprocesses
        if worker:
            self.workerinput = {"workerid": "gw0"}

    def getoption(self, name, default=None):
        assert name == "numprocesses"
        return default if self._numprocesses is None else self._numprocesses


def _holder(lock_path: Path) -> subprocess.Popen:
    """A separate process that holds the gate on ``lock_path`` until stdin closes."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from tests._xdist_gate import XdistGate
        gate = XdistGate({str(lock_path)!r})
        print("locked" if gate.try_lock() else "busy", flush=True)
        sys.stdin.read()
        gate.release()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


def _try_lock_in_child(lock_path: Path) -> str:
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from tests._xdist_gate import XdistGate
        gate = XdistGate({str(lock_path)!r})
        print("locked" if gate.try_lock() else "busy")
        gate.release()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_lock_is_acquired_and_released(tmp_path):
    lock_path = tmp_path / "gate.lock"
    gate = XdistGate(str(lock_path), max_wait=1)

    assert gate.acquire() is True
    assert gate.held
    assert lock_path.exists()
    assert _try_lock_in_child(lock_path) == "busy"

    gate.release()
    assert not gate.held
    assert _try_lock_in_child(lock_path) == "locked"


def test_waits_for_another_process_then_times_out(tmp_path):
    lock_path = tmp_path / "gate.lock"
    holder = _holder(lock_path)
    try:
        stream = io.StringIO()
        gate = XdistGate(
            str(lock_path), max_wait=0.4, notice_interval=0.1, poll_interval=0.05, stream=stream
        )
        assert gate.acquire() is False
        assert not gate.held
        output = stream.getvalue()
        assert "another pytest -n run on this machine holds the lock; waiting (0s)" in output
        assert "AI_SHUFFLE_PYTEST_GATE=0" in output
        assert output.count("holds the lock") >= 2
        assert "proceeding without the lock" in output
    finally:
        holder.communicate(input="", timeout=60)

    # The holder released when it exited, so the lock is free again.
    gate = XdistGate(str(lock_path), max_wait=1)
    assert gate.acquire() is True
    gate.release()


def test_lock_dies_with_the_holding_process(tmp_path):
    lock_path = tmp_path / "gate.lock"
    holder = _holder(lock_path)
    holder.kill()
    holder.wait(timeout=60)

    gate = XdistGate(str(lock_path), max_wait=5, poll_interval=0.05)
    assert gate.acquire() is True
    gate.release()


def test_configure_and_unconfigure_hold_and_release(tmp_path):
    lock_path = tmp_path / "gate.lock"
    config = FakeConfig(numprocesses=2)

    gate = _xdist_gate.configure(config, environ={"AI_SHUFFLE_PYTEST_SLOTS": "1"},
                                 path=str(lock_path), max_wait=1)
    assert gate is not None and gate.held
    slot_path = Path(f"{lock_path}.0")
    assert _try_lock_in_child(slot_path) == "busy"

    _xdist_gate.unconfigure(config)
    assert not gate.held
    assert _try_lock_in_child(slot_path) == "locked"
    _xdist_gate.unconfigure(config)  # second call is a no-op


@pytest.mark.parametrize(
    "environ",
    [
        {"AI_SHUFFLE_PYTEST_GATE": "0"},
        {"GITHUB_ACTIONS": "true"},
        {"CI": "true", "GITHUB_ACTIONS": "true"},
    ],
)
def test_bypass_and_ci_env_skip_the_gate(tmp_path, environ):
    lock_path = tmp_path / "gate.lock"
    config = FakeConfig(numprocesses=4)

    assert _xdist_gate.gate_applies(config, environ) is False
    assert _xdist_gate.configure(config, environ=environ, path=str(lock_path)) is None
    assert not lock_path.exists()


def test_bare_ci_variable_keeps_the_gate():
    # The desktop worker exports CI=true to every agent shell it starts.
    assert _xdist_gate.gate_applies(FakeConfig(numprocesses=2), {"CI": "true"}) is True


def test_skipped_on_xdist_worker(tmp_path):
    lock_path = tmp_path / "gate.lock"
    config = FakeConfig(numprocesses=4, worker=True)

    assert _xdist_gate.gate_applies(config, {}) is False
    assert _xdist_gate.configure(config, environ={}, path=str(lock_path)) is None
    assert not lock_path.exists()


@pytest.mark.parametrize(
    "numprocesses, expected",
    [(None, False), (0, False), (1, False), (2, True), (8, True), ("auto", True), ("logical", True)],
)
def test_only_parallel_xdist_runs_are_gated(numprocesses, expected):
    assert _xdist_gate.gate_applies(FakeConfig(numprocesses=numprocesses), {}) is expected


def test_gate_value_other_than_zero_keeps_the_gate():
    assert _xdist_gate.gate_applies(FakeConfig(numprocesses=2), {"AI_SHUFFLE_PYTEST_GATE": "1"})


def test_lock_error_means_no_gate(tmp_path):
    stream = io.StringIO()
    missing_dir = tmp_path / "missing" / "gate.lock"
    gate = XdistGate(str(missing_dir), max_wait=1, stream=stream)

    assert gate.acquire() is False
    assert "running without it" in stream.getvalue()
    gate.release()  # no-op, never raises


def test_default_lock_path_is_in_system_temp():
    import tempfile

    assert Path(_xdist_gate.default_lock_path()) == (
        Path(tempfile.gettempdir()) / "ai-shuffle-pytest-xdist.lock"
    )


@pytest.mark.parametrize(
    "cpus,memory_gib,expected",
    [(1, 64, 1), (7, 64, 1), (16, 64, 2), (22, 62, 2),
     (64, 128, 4), (64, 24, 2), (32, 11, 1), (None, 64, 1), (64, None, 1)],
)
def test_default_slots_respect_cpu_and_memory(monkeypatch, cpus, memory_gib, expected):
    monkeypatch.setattr(_xdist_gate, "_cpu_count", lambda: cpus or 1)
    monkeypatch.setattr(_xdist_gate, "_total_memory_bytes", lambda: (
        None if memory_gib is None else memory_gib * 1024**3
    ))
    assert _xdist_gate.slot_count({}) == expected


@pytest.mark.parametrize("value,expected", [("1", 1), ("3", 3), (" 8 ", 8)])
def test_slot_override(value, expected):
    assert _xdist_gate.slot_count({"AI_SHUFFLE_PYTEST_SLOTS": value}) == expected


@pytest.mark.parametrize("value", ["", "0", "-1", "2.5", "auto", "garbage"])
def test_invalid_slot_override_uses_machine_default(monkeypatch, value):
    monkeypatch.setattr(_xdist_gate, "_cpu_count", lambda: 32)
    monkeypatch.setattr(_xdist_gate, "_total_memory_bytes", lambda: 48 * 1024**3)
    assert _xdist_gate.slot_count({"AI_SHUFFLE_PYTEST_SLOTS": value}) == 4


def test_slots_skip_held_files_and_release(tmp_path):
    base = tmp_path / "gate.lock"
    holders = [_holder(Path(f"{base}.{i}")) for i in (0, 1)]
    gate = _xdist_gate.XdistSlots(str(base), slots=3, max_wait=0.2)
    try:
        assert gate.acquire()
        assert gate.path == f"{base}.2"
        assert _try_lock_in_child(Path(gate.path)) == "busy"
        gate.release()
        assert _try_lock_in_child(Path(f"{base}.2")) == "locked"
    finally:
        gate.release()
        for holder in holders:
            holder.communicate(input="", timeout=60)


def test_all_slots_wait_and_resume_after_crash(tmp_path):
    base = tmp_path / "gate.lock"
    holders = [_holder(Path(f"{base}.{i}")) for i in (0, 1)]
    stream = io.StringIO()
    def crash_holder(_delay):
        holders[1].kill()
        holders[1].wait(timeout=60)
    gate = _xdist_gate.XdistSlots(
        str(base), slots=2, max_wait=5, stream=stream, sleep=crash_holder
    )
    try:
        assert gate.acquire()
        assert gate.path == f"{base}.1"
        assert "waiting" in stream.getvalue()
        assert _try_lock_in_child(Path(f"{base}.0")) == "busy"
    finally:
        gate.release()
        holders[0].communicate(input="", timeout=60)


@pytest.mark.parametrize("requested", [3, 8, "auto", "logical"])
def test_configure_caps_workers_per_slot(tmp_path, requested):
    from types import SimpleNamespace
    config = FakeConfig(numprocesses=requested)
    # xdist materializes these specs in pytest_cmdline_main, before configure.
    config.option = SimpleNamespace(numprocesses=requested, tx=["popen"] * 8)
    gate = _xdist_gate.configure(config, environ={"AI_SHUFFLE_PYTEST_SLOTS": "2"},
                                 path=str(tmp_path / "gate.lock"), max_wait=0.2)
    try:
        assert gate is not None
        assert config.option.numprocesses == 2
        assert config.option.tx == ["popen"] * 2
    finally:
        _xdist_gate.unconfigure(config)


def test_windows_lock_primitive(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls = []
    fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2,
                           locking=lambda fd, mode, size: calls.append((fd, mode, size)))
    # Replace only this module's os reference: changing os.name globally breaks pathlib.
    monkeypatch.setattr(_xdist_gate, "os", SimpleNamespace(
        name="nt", open=_xdist_gate.os.open, close=_xdist_gate.os.close,
        lseek=_xdist_gate.os.lseek, SEEK_SET=_xdist_gate.os.SEEK_SET,
        O_RDWR=_xdist_gate.os.O_RDWR, O_CREAT=_xdist_gate.os.O_CREAT,
    ))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    gate = XdistGate(str(tmp_path / "windows.lock"))
    assert gate.try_lock()
    gate.release()
    assert [call[1:] for call in calls] == [(fake.LK_NBLCK, 1), (fake.LK_UNLCK, 1)]


@pytest.mark.parametrize("host,affinity,expected", [(32, 22, 22), (8, 16, 8), (None, 16, 1)])
def test_cpu_count_respects_affinity(monkeypatch, host, affinity, expected):
    monkeypatch.setattr(_xdist_gate.os, "cpu_count", lambda: host)
    monkeypatch.setattr(_xdist_gate.os, "sched_getaffinity", lambda _pid: set(range(affinity)),
                        raising=False)
    assert _xdist_gate._cpu_count() == expected


def test_cpu_count_without_affinity(monkeypatch):
    monkeypatch.setattr(_xdist_gate.os, "cpu_count", lambda: 16)
    monkeypatch.delattr(_xdist_gate.os, "sched_getaffinity", raising=False)
    assert _xdist_gate._cpu_count() == 16


def test_slots_rotate_scan_and_close_losing_files(monkeypatch, tmp_path):
    attempts = []
    def busy(gate):
        attempts.append(gate.path)
        gate._open()
        return False
    monkeypatch.setattr(XdistGate, "try_lock", busy)
    gate = _xdist_gate.XdistSlots(str(tmp_path / "gate.lock"), slots=3)
    assert not gate.try_lock()
    assert gate._fd is None
    assert not gate.try_lock()
    assert gate._fd is None
    assert len(set(attempts[:3])) == 3
    assert attempts[3:] == attempts[1:3] + attempts[:1]


def test_slots_timeout_closes_files(tmp_path):
    base = tmp_path / "gate.lock"
    gate = _xdist_gate.XdistSlots(str(base), slots=1, max_wait=0.05,
                                 poll_interval=0.01, stream=io.StringIO())
    holder = _holder(Path(f"{base}.0"))
    try:
        assert not gate.acquire()
        assert not gate.held
        assert gate._fd is None
    finally:
        gate.release()
        holder.communicate(input="", timeout=60)


@pytest.mark.parametrize("environ", [{"AI_SHUFFLE_PYTEST_GATE": "0"}, {"GITHUB_ACTIONS": "true"}])
def test_exempt_runs_keep_requested_workers(tmp_path, environ):
    from types import SimpleNamespace
    config = FakeConfig(numprocesses=8)
    config.option = SimpleNamespace(numprocesses=8)
    assert _xdist_gate.configure(config, environ=environ,
                                 path=str(tmp_path / "gate.lock")) is None
    assert config.option.numprocesses == 8


def test_windows_slots_skip_contention(monkeypatch, tmp_path):
    import errno
    from types import SimpleNamespace
    calls = []
    def locking(fd, mode, size):
        calls.append(mode)
        if len(calls) == 1:
            raise OSError(errno.EACCES, "held")
    monkeypatch.setattr(_xdist_gate, "os", SimpleNamespace(**{
        **vars(_xdist_gate.os), "name": "nt", "getpid": lambda: 0,
    }))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2, locking=locking,
    ))
    gate = _xdist_gate.XdistSlots(str(tmp_path / "gate.lock"), slots=2)
    try:
        assert gate.acquire()
        assert gate.path.endswith(".1")
    finally:
        gate.release()
    assert calls == [1, 1, 2]
