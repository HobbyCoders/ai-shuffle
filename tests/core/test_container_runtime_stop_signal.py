"""The container supervisor must act on a stop signal at any moment (#1317).

Python runs a signal handler in the main thread between two bytecodes. The
supervisor's SIGTERM handler used to call threading.Event.set(), which takes the
same non-reentrant lock that the main loop's STOP.wait() holds for a few
bytecodes on every pass. A signal landing there hung the supervisor, so docker
stop never reached the app and the container was killed with exit 137.
"""
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="The Linux container supervisor requires POSIX file locking")

RUNTIME = Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py"

# Loaded by path: the installed Docker SDK is also named docker.
LOAD = """import importlib.util, signal, sys
spec = importlib.util.spec_from_file_location("shuffle_container_runtime", sys.argv[1])
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
rt.install_stop_handlers()
"""

# Deliver the stop at every line executed inside STOP.wait(), one point per
# fresh wait, exactly as a signal handler would run between bytecodes there.
EVERY_POINT = LOAD + """
handler = signal.getsignal(signal.SIGTERM)

def wait_with_stop_at(target):
    state = {"seen": 0, "fired": False}
    def tracer(frame, event, arg):
        if event == "line" and not state["fired"]:
            state["seen"] += 1
            if state["seen"] == target:
                state["fired"] = True
                handler(signal.SIGTERM, None)
        return tracer
    rt.STOP.clear()
    sys.settrace(tracer)
    try:
        rt.STOP.wait(0.01)
    finally:
        sys.settrace(None)
    return state["fired"]

target = 1
while wait_with_stop_at(target):
    assert rt.STOP.is_set(), target
    target += 1
print("points", target - 1, flush=True)
"""

# The supervisor's loop at its most exposed: always inside STOP.wait().
SPIN = LOAD + """
print("ready", flush=True)
while not rt.STOP.is_set():
    rt.STOP.wait(0)
print("stopped", flush=True)
"""


def test_stop_handler_completes_at_every_point_inside_stop_wait():
    try:
        result = subprocess.run([sys.executable, "-c", EVERY_POINT, str(RUNTIME)],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("the stop handler deadlocked inside STOP.wait(); a SIGTERM there is never acted on")
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.split()[-1]) > 0


def test_sigterm_always_stops_a_supervisor_loop_spinning_on_stop_wait():
    for trial in range(20):
        child = subprocess.Popen([sys.executable, "-c", SPIN, str(RUNTIME)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ready", child.stderr.read()
            time.sleep(0.02)
            child.send_signal(signal.SIGTERM)
            try:
                out, err = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail(f"trial {trial}: SIGTERM was caught but the supervisor loop never stopped")
            assert child.returncode == 0 and out.strip() == "stopped", err
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_stop_flag_wait_times_out_and_returns_early_when_set():
    import importlib.util

    spec = importlib.util.spec_from_file_location("shuffle_container_runtime_stop", RUNTIME)
    assert spec is not None and spec.loader is not None
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    flag = rt.StopFlag()
    started = time.monotonic()
    assert flag.wait(0.1) is False
    assert time.monotonic() - started >= 0.1
    flag.set()
    started = time.monotonic()
    assert flag.wait(30) is True and flag.is_set()
    assert time.monotonic() - started < 1
    flag.clear()
    assert not flag.is_set()
