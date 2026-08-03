"""Regression tests for docker/chrome-supervisor.sh.

The supervisor used to PERMANENTLY latch Chrome off ("DISABLED for this
container's lifetime") after CHROME_MAX_RESTARTS rapid failures. That stranded
Chrome for the whole container lifetime whenever a boot-time intermittent-SIGTRAP
crash window burned through the restart budget. The fix drops into a bounded
slow-retry mode instead, which self-heals when the crash window passes.

These tests run the real script via subprocess with a stubbed `start-chrome`
(and no `setpriv`, exercising the non-root launch path the script exposes for
testability). The stub is driven by a small state file so it can fail instantly
N times and then "run healthily", proving the supervisor:
  a. enters slow-retry mode after MAX_RESTARTS rapid failures, keeps retrying,
     and recovers to a healthy run (reset), and
  b. never prints the old permanent-latch message and never exits the loop.

The supervisor loops forever by design, so each test runs it backgrounded and
kills it after asserting on the accumulated log (kept well under 30s via tiny
knob values).
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

# This file drives docker/chrome-supervisor.sh (a bash script) as a real
# subprocess and relies on os.setsid (in _run_supervisor, below) to put it in
# its own process group so the whole tree can be killed. os.setsid doesn't
# exist on Windows, so the whole module is POSIX-only.
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX-only: drives a bash script via os.setsid"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = REPO_ROOT / "docker" / "chrome-supervisor.sh"

# Stub start-chrome: reads/increments an attempt counter in $STUB_STATE. For the
# first $STUB_FAIL_COUNT invocations it exits 133 (SIGTRAP-equivalent rc) after
# ~0s; afterwards it sleeps longer than CHROME_HEALTHY_SECS so the run counts as
# healthy. Each invocation appends its number to $STUB_STATE.count for the test.
STUB_START_CHROME = """#!/bin/bash
n=$(cat "$STUB_STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STUB_STATE"
echo "$n" >> "$STUB_STATE.count"
if [ "$n" -le "$STUB_FAIL_COUNT" ]; then
    exit 133
fi
# Healthy run: outlive CHROME_HEALTHY_SECS so the supervisor resets.
sleep "$STUB_HEALTHY_SLEEP"
exit 0
"""


def _write_stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(body)
    p.chmod(0o755)


def _run_supervisor(tmp_path: Path, env_overrides: dict) -> subprocess.Popen:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_stub(bindir, "start-chrome", STUB_START_CHROME)

    log = tmp_path / "supervisor.log"
    env = os.environ.copy()
    # Prepend our stub dir so `start-chrome` resolves to the stub. There is no
    # `setpriv` stub — the test runs as non-root, so the supervisor takes its
    # direct-launch (no setpriv) path by design.
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["STUB_STATE"] = str(tmp_path / "state")
    env.update({k: str(v) for k, v in env_overrides.items()})

    logf = open(log, "w")
    proc = subprocess.Popen(
        ["bash", str(SUPERVISOR)],
        stdout=logf,
        stderr=subprocess.STDOUT,
        env=env,
        preexec_fn=os.setsid,  # own process group so we can kill the whole tree
    )
    proc._logfile = logf  # type: ignore[attr-defined]
    proc._logpath = log  # type: ignore[attr-defined]
    return proc


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc._logfile.close()  # type: ignore[attr-defined]
    except Exception:
        pass


def _wait_for(log: Path, needle: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists() and needle in log.read_text():
            return True
        time.sleep(0.1)
    return False


def test_enters_slow_retry_then_recovers(tmp_path):
    """Fail MAX_RESTARTS times rapidly -> slow-retry mode -> healthy recovery."""
    max_restarts = 3
    # Fail 3 rapid times (hits slow mode), then one more slow-retry failure,
    # then a healthy run that must reset the supervisor out of slow mode.
    fail_count = max_restarts + 1
    proc = _run_supervisor(
        tmp_path,
        {
            "CHROME_MAX_RESTARTS": max_restarts,
            "CHROME_HEALTHY_SECS": 2,
            "CHROME_SLOW_RETRY_SECS": 1,
            "CHROME_BACKOFF_MAX": 1,
            "CHROME_BACKOFF_INITIAL": 0,
            "STUB_FAIL_COUNT": fail_count,
            "STUB_HEALTHY_SLEEP": 3,  # > CHROME_HEALTHY_SECS=2
        },
    )
    log = proc._logpath  # type: ignore[attr-defined]
    try:
        assert _wait_for(
            log, "Entering SLOW-RETRY mode", 15
        ), f"never entered slow-retry mode. Log:\n{log.read_text()}"
        # Keeps retrying in slow mode (one short line per attempt).
        assert _wait_for(
            log, "slow-retry attempt 1 failed", 10
        ), f"no slow-retry attempt logged. Log:\n{log.read_text()}"
        # Recovers to a healthy run and leaves slow mode.
        assert _wait_for(
            log, "leaving slow-retry mode", 15
        ), f"never recovered out of slow mode. Log:\n{log.read_text()}"

        text = log.read_text()
        # The loud block prints exactly once (on entry), not every attempt.
        assert text.count("Entering SLOW-RETRY mode") == 1, text
        # Recovery ran the healthy invocation.
        count_file = Path(str(tmp_path / "state") + ".count")
        invocations = count_file.read_text().split()
        assert len(invocations) >= fail_count + 1, (
            f"expected the healthy run to be reached; invocations={invocations}"
        )
    finally:
        _kill(proc)


def test_no_permanent_latch(tmp_path):
    """The old permanent 'DISABLED for this container's lifetime' latch is gone
    and the loop does NOT exit after MAX_RESTARTS rapid failures."""
    max_restarts = 3
    proc = _run_supervisor(
        tmp_path,
        {
            "CHROME_MAX_RESTARTS": max_restarts,
            "CHROME_HEALTHY_SECS": 2,
            "CHROME_SLOW_RETRY_SECS": 1,
            "CHROME_BACKOFF_MAX": 1,
            "CHROME_BACKOFF_INITIAL": 0,
            "STUB_FAIL_COUNT": 9999,  # never succeeds -> would-be permanent latch
            "STUB_HEALTHY_SLEEP": 3,
        },
    )
    log = proc._logpath  # type: ignore[attr-defined]
    try:
        assert _wait_for(log, "Entering SLOW-RETRY mode", 15), log.read_text()
        # Give it time to loop several slow-retry attempts.
        assert _wait_for(log, "slow-retry attempt 3 failed", 10), log.read_text()

        text = log.read_text()
        assert "DISABLED for this container's lifetime" not in text, text
        assert "GIVING UP" not in text, text
        # Process is still alive — the loop did not exit after MAX_RESTARTS.
        assert proc.poll() is None, "supervisor exited; it must loop forever"
    finally:
        _kill(proc)
