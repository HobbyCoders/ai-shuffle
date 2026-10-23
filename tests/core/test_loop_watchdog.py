"""Event-loop stall watchdog: a blocked loop gets its stack dumped."""

import asyncio
import logging
import sys
import threading
import time

import pytest

from app.core.loop_watchdog import LoopStallWatchdog, _threshold_from_env


def _wait_for_watchdog_test(stop: threading.Event) -> None:
    stop.wait()


@pytest.fixture
def diagnostic_logs(tmp_path, monkeypatch):
    from app.core import file_logging

    freeze_path = tmp_path / "freeze.log"
    app_path = tmp_path / "app.log"
    handler = logging.FileHandler(app_path, encoding="utf-8")
    log = logging.getLogger("app.core.loop_watchdog")
    log.addHandler(handler)
    stop = threading.Event()
    thread = threading.Thread(target=_wait_for_watchdog_test, args=(stop,), name="test-idle-thread")
    thread.start()
    try:
        with freeze_path.open("w", encoding="utf-8") as stream:
            monkeypatch.setattr(file_logging, "_freeze_file", stream)
            yield app_path, freeze_path
    finally:
        stop.set()
        thread.join(timeout=1)
        log.removeHandler(handler)
        handler.close()


def _block_the_loop_for(seconds: float) -> None:
    # A distinct frame name so the dump assertion can find it.
    time.sleep(seconds)


@pytest.mark.asyncio
async def test_blocked_loop_dumps_sleeping_frame(caplog, diagnostic_logs):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.2,
        ping_interval_s=0.05,
        second_threshold_s=45.0,
    )
    wd.start()
    try:
        await asyncio.sleep(0.15)  # let the first ping be answered normally
        assert wd.dump_count == 0
        _block_the_loop_for(0.5)  # the loop is frozen here
        # Let the watchdog observe the answered ping. A fixed 0.2 s was too
        # short on a loaded Windows machine, so wait for it, up to 5 s.
        deadline = time.monotonic() + 5
        while wd.last_stall_seconds is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    finally:
        wd.stop()

    assert wd.stall_count == 1
    assert wd.dump_count == 1  # below the second threshold
    assert wd.last_stall_seconds is not None and wd.last_stall_seconds >= 0.2

    dumps = [r for r in caplog.records if "loop thread stack" in r.getMessage()]
    assert len(dumps) == 1
    text = dumps[0].getMessage()
    assert "_block_the_loop_for" in text
    assert "time.sleep" in text
    assert "(event-loop thread)" in text
    assert "_wait_for_watchdog_test" in text
    app_path, freeze_path = diagnostic_logs
    for path in (app_path, freeze_path):
        durable = path.read_text(encoding="utf-8")
        assert "_block_the_loop_for" in durable
        assert "_wait_for_watchdog_test" in durable
        assert durable.count("Event loop stalled for") == 1

    ended = [r for r in caplog.records if "stall ended after" in r.getMessage()]
    assert len(ended) == 1


@pytest.mark.asyncio
async def test_healthy_loop_never_dumps(caplog):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(), threshold_s=0.2, ping_interval_s=0.02
    )
    wd.start()
    thread = wd._thread
    assert thread is not None and thread.daemon
    try:
        for _ in range(10):
            await asyncio.sleep(0.03)
    finally:
        wd.stop()
    assert wd.stall_count == 0
    assert wd.dump_count == 0
    assert wd._thread is None
    assert not thread.is_alive()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_persistent_stall_redumps(caplog):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.1,
        ping_interval_s=0.02,
        second_threshold_s=0.15,
    )
    wd.start()
    try:
        await asyncio.sleep(0.05)
        _block_the_loop_for(0.5)
        await asyncio.sleep(0.1)
    finally:
        wd.stop()
    assert wd.stall_count == 1
    assert wd.dump_count == 2
    still = [r for r in caplog.records if "still stalled" in r.getMessage()]
    assert len(still) == 1


def test_threshold_env(monkeypatch):
    monkeypatch.delenv("LOOP_STALL_THRESHOLD_S", raising=False)
    assert _threshold_from_env() == 10.0  # default for production
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "7.5")
    assert _threshold_from_env() == 7.5
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "nope")
    assert _threshold_from_env() == 10.0
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "0")
    assert _threshold_from_env() == 10.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_threshold_env_must_be_finite_positive(monkeypatch, value):
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", value)
    assert _threshold_from_env() == 10.0


def test_new_stall_gets_its_own_dump_budget(monkeypatch, caplog):
    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop, threshold_s=10, second_threshold_s=45)
        calls = []
        monkeypatch.setattr(wd, "_dump", lambda age, *, first: calls.append(first))
        wd._pending_since = 0
        for now in (10, 44, 45, 90, 300):
            wd._check(now)
        assert calls == [True, False]
        wd._pong()
        wd._check(301)
        assert wd.last_stall_seconds == 301
        assert "stall ended after" in caplog.text
        wd._pending_since = 302
        for now in (312, 347, 600):
            wd._check(now)
        assert calls == [True, False, True, False]
        assert wd.stall_count == 2
    finally:
        loop.close()


def test_durable_dump_precedes_logger_locks(diagnostic_logs):
    _, freeze_path = diagnostic_logs

    class CheckingLogger:
        def error(self, *args):
            assert "test_durable_dump_precedes_logger_locks" in freeze_path.read_text(encoding="utf-8")

    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop, log=CheckingLogger())
        wd._dump(10, first=True)
        assert wd.dump_count == 1
    finally:
        loop.close()


def test_freeze_dump_timer_rearms_from_the_loop_and_disarms_on_stop(monkeypatch):
    from app.core import file_logging
    from app.core.loop_watchdog import FreezeDumpTimer

    armed, cancelled = [], []
    monkeypatch.setattr(file_logging, "arm_freeze_dump", lambda timeout: armed.append(timeout) or True)
    monkeypatch.setattr(file_logging, "cancel_freeze_dump", lambda: cancelled.append(1))

    async def scenario():
        timer = FreezeDumpTimer(asyncio.get_running_loop(), timeout_s=30.0, rearm_s=0.01)
        timer.start()
        await asyncio.sleep(0.1)
        timer.stop()
        count = len(armed)
        await asyncio.sleep(0.05)
        return count

    count = asyncio.run(scenario())
    assert count >= 3 and set(armed) == {30.0}
    assert len(armed) == count, "no re-arm after stop"
    assert cancelled == [1]


def test_freeze_dump_timer_stays_off_without_a_freeze_log(monkeypatch):
    from app.core import file_logging
    from app.core.loop_watchdog import FreezeDumpTimer

    monkeypatch.setattr(file_logging, "_freeze_file", None)

    async def scenario():
        timer = FreezeDumpTimer(asyncio.get_running_loop(), rearm_s=0.01)
        timer.start()
        await asyncio.sleep(0.05)
        assert timer.armed is False
        timer.stop()

    asyncio.run(scenario())


def test_faulthandler_dumps_a_frozen_interpreter_without_the_gil(tmp_path, monkeypatch):
    """The C watchdog writes all stacks even while this thread never yields."""
    from app.core import file_logging

    with (tmp_path / "freeze.log").open("a", encoding="utf-8") as stream:
        monkeypatch.setattr(file_logging, "_freeze_file", stream)
        assert file_logging.arm_freeze_dump(0.2) is True
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and "Timeout" not in (tmp_path / "freeze.log").read_text():
                time.sleep(0.05)
        finally:
            file_logging.cancel_freeze_dump()
    text = (tmp_path / "freeze.log").read_text()
    assert "Timeout (0:00:00.200000)!" in text and "test_faulthandler_dumps_a_frozen_interpreter" in text


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGUSR1"), reason="SIGUSR1 is POSIX only")
def test_sigusr1_dumps_every_thread_to_the_freeze_log(tmp_path):
    import faulthandler
    import os
    import signal

    from app.core import file_logging

    with (tmp_path / "freeze.log").open("a", encoding="utf-8") as stream:
        assert file_logging._register_stack_dump_signal(stream) is True
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            time.sleep(0.1)
        finally:
            faulthandler.unregister(signal.SIGUSR1)
    assert "test_sigusr1_dumps_every_thread_to_the_freeze_log" in (tmp_path / "freeze.log").read_text()


_GIL_HOLDER = r"""
import ctypes, sys, threading, time
sys.path.insert(0, sys.argv[2])
from app.core import file_logging

stream = open(sys.argv[1], "a", encoding="utf-8")
file_logging._freeze_file = stream
assert file_logging._register_stack_dump_signal(stream)
assert file_logging.arm_freeze_dump(0.5)
idle = threading.Event()
threading.Thread(target=idle.wait, name="bystander", daemon=True).start()
print("holding", flush=True)
# A PyDLL call keeps the GIL for its whole duration, like the native git call
# in #436: no Python thread (the stall watchdog included) can run meanwhile.
ctypes.PyDLL(None).sleep(3)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="libc sleep through ctypes.PyDLL")
def test_a_gil_held_in_native_code_still_leaves_every_stack_in_the_freeze_log(tmp_path):
    """#436: the interpreter froze with the GIL held and no stack was written."""
    import signal
    import subprocess
    from pathlib import Path

    freeze = tmp_path / "freeze.log"
    freeze.write_text("--- an earlier dump ---\n", encoding="utf-8")
    repo = str(Path(__file__).resolve().parents[2])
    child = subprocess.Popen([sys.executable, "-c", _GIL_HOLDER, str(freeze), repo],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "holding"
        time.sleep(1.2)  # past the 0.5 s timer, at least twice with repeat=True
        child.send_signal(signal.SIGUSR1)  # the supervisor's pre-stop request
        assert child.wait(timeout=15) == 0
    finally:
        if child.poll() is None:
            child.kill()
    text = freeze.read_text(encoding="utf-8")
    assert text.startswith("--- an earlier dump ---\n"), "appended, never truncated"
    assert text.count("Timeout (0:00:00.500000)!") >= 2, "the timer repeats while the interpreter stays frozen"
    holding = 'File "<string>", line 15 in <module>'  # the PyDLL sleep line
    assert text.count(holding) >= 3, "the GIL-holding frame is in every dump"
    assert text.count(holding) > text.count("Timeout ("), "SIGUSR1 added its own dump while the GIL was held"
    assert "threading.py" in text, "the bystander thread's stack is dumped too"
