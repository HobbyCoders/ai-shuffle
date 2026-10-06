"""Tests for the one-``pytest -n``-per-machine gate in tests/_xdist_gate.py.

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

    gate = _xdist_gate.configure(config, environ={}, path=str(lock_path), max_wait=1)
    assert gate is not None and gate.held
    assert _try_lock_in_child(lock_path) == "busy"

    _xdist_gate.unconfigure(config)
    assert not gate.held
    assert _try_lock_in_child(lock_path) == "locked"
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
