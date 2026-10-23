"""Event-loop stall watchdog: a blocked loop gets its stack dumped."""

import asyncio
import json
import logging
import sys
import threading
import time

import pytest

from app.core.loop_watchdog import LoopStallWatchdog, _threshold_from_env


@pytest.fixture(autouse=True)
def isolated_heartbeat(tmp_path, monkeypatch):
    from app.core import file_logging

    monkeypatch.setattr(file_logging, "get_log_dir", lambda: tmp_path)


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
    recovery = ended[0].getMessage()
    assert "threads=" in recovery and "thread_frames=[" in recovery
    assert f"thread_frames=[{threading.current_thread().name}=" in recovery
    assert "asyncio_tasks=" in recovery
    assert wd._recovery_task_count > 0


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

@pytest.mark.asyncio
async def test_heartbeat_tracks_ticks_not_watchdog_wakeups(tmp_path, monkeypatch):
    from app.core import loop_watchdog

    monkeypatch.setattr(loop_watchdog, "HEARTBEAT_INTERVAL_S", 0.04)
    path = tmp_path / "loop-heartbeat.json"
    loop_thread = threading.get_ident()
    writers = []
    replace = loop_watchdog.os.replace

    def record_replace(source, target):
        writers.append(threading.get_ident())
        replace(source, target)

    monkeypatch.setattr(loop_watchdog.os, "replace", record_replace)
    wd = LoopStallWatchdog(asyncio.get_running_loop(), threshold_s=5, ping_interval_s=0.01)
    wd.start()
    try:
        deadline = time.monotonic() + 5
        while len(writers) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        first_tick = json.loads(path.read_text())["last_tick"]
        await asyncio.sleep(0.1)
        assert json.loads(path.read_text())["last_tick"] > first_tick
        # Real loop stall: allow any pre-stall tick to flush, then prove that
        # neither the record nor its mtime changes while the thread keeps waking.
        _block_the_loop_for(0.1)
        during = path.read_bytes(), path.stat().st_mtime_ns
        _block_the_loop_for(0.1)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == during
        stalled_tick = json.loads(during[0])["last_tick"]
        await asyncio.sleep(0.15)
        assert json.loads(path.read_text())["last_tick"] > stalled_tick
    finally:
        wd.stop()
    assert writers and loop_thread not in writers
    assert json.loads(path.read_text())["stopped_at"] is not None
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("clean", [True, False])
def test_boot_reports_stale_heartbeat(tmp_path, caplog, clean):
    path = tmp_path / "loop-heartbeat.json"
    loop = asyncio.new_event_loop()
    try:
        old = LoopStallWatchdog(loop)
        old._boot_heartbeat()
        old._pong()  # Exercise the producing path, not only a seeded consumer.
        old._write_heartbeat(0)
        last_tick = json.loads(path.read_text())["last_tick"]
        if clean:
            old._write_heartbeat(20, stopped_at=last_tick + 30)
        else:
            # Boot time is only a bound for an unclean process end.
            import unittest.mock
            with unittest.mock.patch("app.core.loop_watchdog.time.time", return_value=last_tick + 30):
                restarted = LoopStallWatchdog(loop)
                restarted._boot_heartbeat()
        if clean:
            restarted = LoopStallWatchdog(loop)
            restarted._boot_heartbeat()
        restarted._send_ping(time.monotonic())
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert "previous run stopped ticking at" in caplog.text
    assert ("before shutdown" if clean else "process end time unknown") in caplog.text


def test_clean_shutdown_downtime_is_not_a_stall(tmp_path, caplog):
    now = time.time()
    (tmp_path / "loop-heartbeat.json").write_text(json.dumps({
        "last_tick": now - 3601, "stopped_at": now - 3600,
    }))
    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop)
        wd._boot_heartbeat()
        wd._send_ping(time.monotonic())
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert "previous run stopped ticking" not in caplog.text


def test_heartbeat_rate_limit_and_atomic_failure(tmp_path, monkeypatch):
    from app.core import loop_watchdog

    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop)
        wd._boot_heartbeat()
        wd._pong()
        wd._write_heartbeat(0)
        path = tmp_path / "loop-heartbeat.json"
        initial = path.read_bytes()
        wd._pong()
        wd._write_heartbeat(1)
        assert path.read_bytes() == initial

        def fail_replace(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(loop_watchdog.os, "replace", fail_replace)
        wd._write_heartbeat(5)
        assert path.read_bytes() == initial  # no partially overwritten record
        assert wd._heartbeat_failed
    finally:
        loop.close()


def test_heartbeat_failure_uses_no_wakeup_and_logs_on_normal_ping(tmp_path, monkeypatch, caplog):
    from unittest.mock import Mock

    from app.core import loop_watchdog

    loop = asyncio.new_event_loop()
    loop.set_debug(True)
    try:
        wd = LoopStallWatchdog(loop)
        wd._boot_heartbeat()
        wd._pong()
        wd._write_heartbeat(0)
        wd._pong()
        wakeup = Mock(wraps=loop.call_soon_threadsafe)
        monkeypatch.setattr(loop, "call_soon_threadsafe", wakeup)

        def fail_replace(*args):
            raise OSError("heartbeat disk unavailable")

        monkeypatch.setattr(loop_watchdog.os, "replace", fail_replace)
        wd._write_heartbeat(5)
        wakeup.assert_not_called()  # Even a failed debug-mode wakeup may log.
        assert wd._pending_heartbeat_warning is not None
        assert "Could not persist loop heartbeat" not in caplog.text

        wd._send_ping(time.monotonic())  # Only the normal watchdog ping wakes the loop.
        wakeup.assert_called_once_with(wd._pong)
        loop.run_until_complete(asyncio.sleep(0))
        assert wd._pending_heartbeat_warning is None
        assert caplog.text.count("Could not persist loop heartbeat") == 1

        # Repeated I/O failures remain rate-limited, with no growing queue.
        wd._write_heartbeat(10)
        assert wd._pending_heartbeat_warning is None
        wakeup.assert_called_once_with(wd._pong)
        assert caplog.text.count("Could not persist loop heartbeat") == 1
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_heartbeat_failure_cannot_block_durable_stall_dump(
    tmp_path, monkeypatch, diagnostic_logs, caplog,
):
    from app.core import loop_watchdog

    monkeypatch.setattr(loop_watchdog, "HEARTBEAT_INTERVAL_S", 0.01)
    _, freeze_path = diagnostic_logs
    handler_path = tmp_path / "locked-handler.log"
    handler = logging.FileHandler(handler_path, encoding="utf-8")
    log = logging.getLogger("heartbeat-lock-regression")
    caplog.set_level(logging.WARNING, logger=log.name)
    monkeypatch.setattr(log, "propagate", False)
    log.addHandler(handler)
    inject_failure = threading.Event()
    write_failed = threading.Event()
    replace = loop_watchdog.os.replace

    def fail_replace(source, target):
        if inject_failure.is_set():
            write_failed.set()
            raise OSError("heartbeat disk unavailable")
        replace(source, target)

    monkeypatch.setattr(loop_watchdog.os, "replace", fail_replace)
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(), threshold_s=0.08, ping_interval_s=0.01, log=log,
    )

    def stall_while_holding_logging_lock():
        handler.acquire()  # Held by the loop, not the watchdog's OS thread.
        try:
            inject_failure.set()
            wd._pong()  # A new successful tick must attempt persistence.
            assert write_failed.wait(2), "heartbeat failure was not exercised"
            deadline = time.monotonic() + 2
            while "Event loop stalled for" not in freeze_path.read_text() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert "Event loop stalled for" in freeze_path.read_text()
            assert wd.stall_count == 1
            assert "Could not persist loop heartbeat" not in handler_path.read_text()
        finally:
            handler.release()

    wd.start()
    try:
        # Let startup and a genuine loop ping/pong complete before the stall.
        deadline = time.monotonic() + 5
        while wd._heartbeat_written_tick is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert wd._heartbeat_written_tick is not None
        stall_while_holding_logging_lock()
        await asyncio.sleep(0.1)  # Deferred warning now runs on the healthy loop.
    finally:
        wd.stop()
        log.removeHandler(handler)
        handler.close()
    assert handler_path.read_text().count("Could not persist loop heartbeat") == 1


@pytest.mark.parametrize("record", ["not json", "null", '{"last_tick": "nan"}', '[]'])
def test_bad_previous_heartbeat_is_nonfatal(tmp_path, caplog, record):
    (tmp_path / "loop-heartbeat.json").write_text(record)
    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop)
        wd._boot_heartbeat()
        assert "Could not read previous loop heartbeat" not in caplog.text
        wd._send_ping(time.monotonic())
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert "Could not read previous loop heartbeat" in caplog.text


def test_recovery_thread_context_is_bounded(monkeypatch):
    from types import SimpleNamespace

    from app.core import loop_watchdog

    threads = [SimpleNamespace(ident=i, name=f"thread-{i}-" + "x" * 1000) for i in range(30)]
    monkeypatch.setattr(loop_watchdog.threading, "enumerate", lambda: threads)
    frame = sys._getframe()
    monkeypatch.setattr(loop_watchdog.sys, "_current_frames", lambda: {29: frame})
    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop)
        wd._loop_thread_id = 29  # not among the first five names alphabetically
        context = wd._recovery_context()
        assert "threads=30" in context
        assert "thread_frames=[thread-29-" in context
        assert context.count("thread-") == loop_watchdog.RECOVERY_THREAD_LIMIT
        assert "test_recovery_thread_context_is_bounded" in context
        assert len(context) < 1500
    finally:
        loop.close()


_HEARTBEAT_CHILD = r"""
import asyncio, sys, time
from pathlib import Path
from app.core import file_logging, loop_watchdog
file_logging.get_log_dir = lambda: Path(sys.argv[1])
loop_watchdog.HEARTBEAT_INTERVAL_S = 0.02
async def main():
    wd = loop_watchdog.LoopStallWatchdog(asyncio.get_running_loop(), ping_interval_s=0.01)
    wd.start()
    while not (Path(sys.argv[1]) / "loop-heartbeat.json").exists():
        await asyncio.sleep(0.01)
    print("stalled", flush=True)
    time.sleep(30)
asyncio.run(main())
"""


def test_killed_stalled_process_leaves_a_boot_readable_heartbeat(tmp_path, monkeypatch, caplog):
    import subprocess

    from app.core import loop_watchdog

    child = subprocess.Popen([sys.executable, "-c", _HEARTBEAT_CHILD, str(tmp_path)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "stalled"
        time.sleep(0.1)  # let the last pre-stall tick flush
        path = tmp_path / "loop-heartbeat.json"
        frozen = path.read_bytes()
        time.sleep(0.15)
        assert path.read_bytes() == frozen
    finally:
        child.kill()  # no Python cleanup / watchdog stop; SIGKILL on POSIX
        child.wait(timeout=5)
        child.stdout.close()
    assert json.loads(frozen)["stopped_at"] is None
    monkeypatch.setattr(loop_watchdog, "HEARTBEAT_INTERVAL_S", 0.02)
    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop, threshold_s=0.05)
        wd._boot_heartbeat()
        wd._send_ping(time.monotonic())
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert "previous run stopped ticking at" in caplog.text
