"""Event-loop stall watchdog: a blocked loop gets its stack dumped."""

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

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


def _block_the_loop_for_dump(dumped: threading.Event, blocking: threading.Event) -> None:
    # Keep the sleeping frame (including its source marker) stable for the dump.
    deadline = time.monotonic() + 5
    blocking.set()
    while not dumped.is_set() and time.monotonic() < deadline: time.sleep(0.01)  # noqa: E701
    assert dumped.is_set(), "watchdog did not finish the requested dump while the loop was blocked"


async def _exercise_stall(
    wd: LoopStallWatchdog,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dumps: int = 1,
    checks_after_dump: int = 0,
    records: list[logging.LogRecord] | None = None,
    gate_boot: bool = False,
) -> None:
    """Real boot/pong -> blocked loop -> outstanding ping -> dump -> recovery."""
    loop = asyncio.get_running_loop()
    boot_entered = asyncio.Event()
    boot_release = threading.Event()
    warmed = asyncio.Event()
    blocking = threading.Event()
    ping_sent = threading.Event()
    dumped = threading.Event()
    requested_dump_finished = threading.Event()
    additional_checks = 0
    recovered = asyncio.Event()
    boot, pong, send_ping, dump, check = (
        wd._boot_heartbeat, wd._pong, wd._send_ping, wd._dump, wd._check,
    )

    def gated_boot() -> None:
        loop.call_soon_threadsafe(boot_entered.set)
        assert boot_release.wait(5), "loop did not release watchdog startup"
        boot()

    def observed_pong() -> None:
        pong()
        warmed.set()

    def gated_ping(now: float) -> None:
        after_warmup = warmed.is_set() and not ping_sent.is_set()
        if after_warmup:
            assert blocking.wait(5), "loop never entered the blocking frame"
        # Do not age the ping by the time spent waiting at the test gate.
        send_ping(time.monotonic())
        if after_warmup:
            with wd._lock:
                assert wd._pending_since is not None, "blocked loop answered the ping"
            ping_sent.set()

    def observed_dump(age: float, *, first: bool) -> None:
        dump(age, first=first)
        if wd.dump_count == dumps:
            # Both the durable write and normal logging have finished.
            requested_dump_finished.set()
            if checks_after_dump == 0:
                dumped.set()

    def observed_check(now: float) -> None:
        nonlocal additional_checks
        after_requested_dump = requested_dump_finished.is_set()
        check(now)
        # Do not count the check that produced the requested dump. Keep the
        # same real ping unanswered through further production budget checks.
        if after_requested_dump:
            additional_checks += 1
            if additional_checks >= checks_after_dump:
                dumped.set()
        if wd.last_stall_seconds is not None:
            loop.call_soon_threadsafe(recovered.set)

    if gate_boot:
        monkeypatch.setattr(wd, "_boot_heartbeat", gated_boot)
    monkeypatch.setattr(wd, "_pong", observed_pong)
    monkeypatch.setattr(wd, "_send_ping", gated_ping)
    monkeypatch.setattr(wd, "_dump", observed_dump)
    monkeypatch.setattr(wd, "_check", observed_check)
    wd.start()
    try:
        if gate_boot:
            await asyncio.wait_for(boot_entered.wait(), timeout=5)
            assert not warmed.is_set(), "startup gate must prevent the first ping"
            boot_release.set()
        await asyncio.wait_for(warmed.wait(), timeout=5)
        assert wd.dump_count == 0
        _block_the_loop_for_dump(dumped, blocking)
        assert ping_sent.is_set(), "dump did not follow a real outstanding ping"
        assert additional_checks >= checks_after_dump
        assert wd.dump_count == dumps, "continuing stall exceeded its dump budget"
        if records is not None:
            assert sum("still stalled" in record.getMessage() for record in records) == 1
        # Only now may the loop yield to the outstanding pong and recover.
        await asyncio.wait_for(recovered.wait(), timeout=5)
    finally:
        boot_release.set()
        blocking.set()
        wd.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_boot", [False, True], ids=["normal-boot", "gated-boot"])
async def test_blocked_loop_dumps_sleeping_frame(
    caplog: pytest.LogCaptureFixture,
    diagnostic_logs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    gate_boot: bool,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.2,
        ping_interval_s=0.05,
        second_threshold_s=45.0,
    )
    await _exercise_stall(wd, monkeypatch, gate_boot=gate_boot)

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


@pytest.fixture
def heartbeat_fsync(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    # Tick/healthy-loop tests exercise persistence ordering, not storage latency.
    # Keep real writes and atomic replace, but control the unbounded fsync syscall.
    # The lifecycle regression below explicitly holds I/O across a stop timeout.
    calls: list[int] = []

    def record_fsync(fd: int) -> None:
        calls.append(threading.get_ident())

    monkeypatch.setattr("app.core.loop_watchdog.os.fsync", record_fsync)
    return calls


@pytest.mark.asyncio
async def test_healthy_loop_never_dumps(
    caplog: pytest.LogCaptureFixture, heartbeat_fsync: list[int],
) -> None:
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
    assert wd._thread is None, _watchdog_stack(thread)
    assert not thread.is_alive(), _watchdog_stack(thread)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def _watchdog_stack(thread: threading.Thread | None) -> str:
    if thread is None:
        return "<no thread handle>"
    frame = sys._current_frames().get(thread.ident)
    return "".join(traceback.format_stack(frame)) if frame is not None else "<thread exited>"


@pytest.mark.parametrize("phase", ["boot", "heartbeat", "shutdown"])
@pytest.mark.parametrize("reap_with_start", [False, True], ids=["rejoin", "restart"])
def test_stop_retains_blocked_thread_until_it_can_be_joined(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    phase: str, reap_with_start: bool,
) -> None:
    from app.core import loop_watchdog

    entered = threading.Event()
    release = threading.Event()
    loop = asyncio.new_event_loop()
    wd = LoopStallWatchdog(loop, ping_interval_s=0.01)
    boot = wd._boot_heartbeat
    thread: threading.Thread | None = None

    def blocked_io() -> None:
        entered.set()
        assert release.wait(5), "test did not release blocked watchdog I/O"

    def blocked_boot() -> None:
        blocked_io()
        boot()

    def blocked_fsync(fd: int) -> None:
        if phase == "heartbeat" or wd._stop.is_set():
            blocked_io()

    monkeypatch.setattr(loop_watchdog.os, "fsync", blocked_fsync)
    if phase == "boot":
        monkeypatch.setattr(wd, "_boot_heartbeat", blocked_boot)
    wd._pong()
    try:
        wd.start()
        thread = wd._thread
        assert thread is not None
        if phase == "shutdown":
            wd._stop.set()  # Enter the real final-write path before joining.
        assert entered.wait(5), "watchdog did not enter the requested I/O phase"
        wd.stop(join_timeout=0.01)
        stack = _watchdog_stack(thread)
        assert thread.is_alive(), stack
        assert "blocked_io" in stack, stack
        assert wd._thread is thread, stack
        assert wd._stop.is_set()
        wd.start()
        assert wd._thread is thread, "start must not replace a surviving watchdog"
        assert wd._stop.is_set(), "start must not revive a watchdog still stopping"
        assert "did not stop" in caplog.text
        assert "blocked_io" in caplog.text
        release.set()
        if reap_with_start:
            thread.join(timeout=2)
            assert not thread.is_alive(), _watchdog_stack(thread)
            assert wd._thread is thread
            wd.start()
            restarted = wd._thread
            assert restarted is not None and restarted is not thread
            wd.stop()
            assert not restarted.is_alive(), _watchdog_stack(restarted)
        else:
            wd.stop()
        assert not thread.is_alive(), _watchdog_stack(thread)
        assert wd._thread is None
    finally:
        release.set()
        wd.stop()
        if thread is not None:
            thread.join(timeout=2)
        loop.close()


@pytest.mark.parametrize("depth", [0, 64], ids=["shallow", "deep"])
def test_stop_diagnostic_does_not_wait_for_source_lookup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, depth: int,
) -> None:
    import linecache

    io_entered = threading.Event()
    release_io = threading.Event()
    source_entered = threading.Event()
    release_source = threading.Event()
    returned = threading.Event()
    loop = asyncio.new_event_loop()
    wd = LoopStallWatchdog(loop, ping_interval_s=0.01)
    stopper: threading.Thread | None = None
    stopped_in_time = False

    def blocked_boot(remaining: int = depth) -> None:
        if remaining:
            blocked_boot(remaining - 1)
            return
        io_entered.set()
        release_io.wait(5)

    def blocked_source_lookup(*args: object, **kwargs: object) -> str:
        source_entered.set()
        release_source.wait(5)
        return ""

    def stop_watchdog() -> None:
        try:
            wd.stop(join_timeout=0.01)
        finally:
            returned.set()

    monkeypatch.setattr(wd, "_boot_heartbeat", blocked_boot)
    wd.start()
    thread = wd._thread
    assert thread is not None
    try:
        assert io_entered.wait(5), "watchdog never entered the held I/O"
        with monkeypatch.context() as source_patch:
            source_patch.setattr(linecache, "checkcache", blocked_source_lookup)
            source_patch.setattr(linecache, "getline", blocked_source_lookup)
            stopper = threading.Thread(target=stop_watchdog, daemon=True)
            stopper.start()
            # The source gate remains shut throughout this bounded observation.
            stopped_in_time = returned.wait(1)
    finally:
        release_source.set()
        release_io.set()
        if stopper is not None:
            stopper.join(timeout=2)
        wd.stop()
        loop.close()
    assert stopped_in_time, "bounded stop waited for filesystem-backed source lookup"
    assert not source_entered.is_set(), "shutdown diagnostics must not consult linecache"
    assert not thread.is_alive()
    assert stopper is not None and not stopper.is_alive()
    assert "blocked_boot" in caplog.text
    assert "test_loop_watchdog.py" in caplog.text
    assert caplog.text.count('", line ') <= 32
    assert ("<older frames omitted>" in caplog.text) == (depth > 32)


@pytest.mark.asyncio
async def test_persistent_stall_redumps(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.1,
        ping_interval_s=0.02,
        second_threshold_s=0.15,
    )
    await _exercise_stall(wd, monkeypatch, dumps=2, checks_after_dump=5, records=caplog.records)
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


_FREEZE_DUMP_CHILD = r"""
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from app.core import file_logging


def _block_for_dump() -> None:
    assert sys.stdin.readline().strip() == "dumped"


def test_faulthandler_dumps_a_frozen_interpreter_without_the_gil() -> None:
    with Path(sys.argv[1]).open("a", encoding="utf-8") as stream:
        file_logging._freeze_file = stream
        try:
            assert file_logging.arm_freeze_dump(0.2) is True
            # Native read releases the GIL. Only the parent observing a complete
            # C dump releases us; no Python observer runs alongside the dumper.
            _block_for_dump()
        finally:
            file_logging.cancel_freeze_dump()


test_faulthandler_dumps_a_frozen_interpreter_without_the_gil()
"""


def _freeze_dump_finished(text: str) -> bool:
    # The single-thread child ends its stack at the module frame. The timeout
    # header is written BEFORE the stack; it is not a completion signal (#572).
    return "Timeout (0:00:00.200000)!" in text and text.rstrip().endswith("in <module>")


def test_freeze_dump_completion_requires_the_last_frame() -> None:
    header = "Timeout (0:00:00.200000)!\nThread 0x1234 (most recent call first):\n"
    assert not _freeze_dump_finished(header)
    partial = header + (
        '  File "<string>", line 20 in '
        'test_faulthandler_dumps_a_frozen_interpreter_without_the_gil\n'
    )
    assert not _freeze_dump_finished(partial)
    assert _freeze_dump_finished(partial + '  File "<string>", line 21, in <module>\n')


def test_faulthandler_dumps_a_frozen_interpreter_without_the_gil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native all-thread dump must not run inside the xdist worker process."""
    from app.core import file_logging

    def unexpected_worker_timer(timeout_s: float) -> bool:
        pytest.fail("the process-global C dump timer must only be armed in the child")

    # Regression guard: a return to the in-worker arm/cancel sequence must fail.
    monkeypatch.setattr(file_logging, "arm_freeze_dump", unexpected_worker_timer)
    freeze = tmp_path / "freeze.log"
    with subprocess.Popen(
        [sys.executable, "-c", _FREEZE_DUMP_CHILD, str(freeze),
         str(Path(__file__).resolve().parents[2])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 10
            text = ""
            while child.poll() is None and time.monotonic() < deadline:
                text = freeze.read_text(encoding="utf-8") if freeze.exists() else ""
                if _freeze_dump_finished(text):
                    break
                time.sleep(0.01)
            assert _freeze_dump_finished(text), (
                f"C watchdog did not finish freeze.log (child status {child.poll()}):\n{text}"
            )
            stdout, stderr = child.communicate("dumped\n", timeout=5)
            assert child.returncode == 0, (
                f"freeze-dump child exited {child.returncode}\n"
                f"stdout: {stdout}\nstderr: {stderr}\nfreeze.log:\n{text}"
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    assert "Timeout (0:00:00.200000)!" in text
    assert "test_faulthandler_dumps_a_frozen_interpreter_without_the_gil" in text
    assert "_block_for_dump" in text  # The main thread is blocked in the native pipe read.


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
@pytest.mark.parametrize(
    ("gate_io", "startup_delay"), [(False, 0), (True, 0), (True, 5.1)],
    ids=["normal-io", "delayed-io", "slow-start-io"],
)
async def test_heartbeat_tracks_ticks_not_watchdog_wakeups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    heartbeat_fsync: list[int], gate_io: bool, startup_delay: float,
) -> None:
    from app.core import loop_watchdog

    # Rate limiting has its own test. Here every watchdog wakeup may write,
    # so an unchanged heartbeat proves tick gating, not a short rate-limit gap.
    monkeypatch.setattr(loop_watchdog, "HEARTBEAT_INTERVAL_S", 0)
    path = tmp_path / "loop-heartbeat.json"
    loop_thread = threading.get_ident()
    writers: list[int] = []
    replace = loop_watchdog.os.replace
    progressed = threading.Condition()
    hold_write = threading.Event()
    write_entered = threading.Event()
    release_write = threading.Event()
    wakeups = 0

    def record_replace(source: Path, target: Path) -> None:
        if not writers:
            time.sleep(startup_delay)
        if hold_write.is_set():
            hold_write.clear()
            write_entered.set()
            assert release_write.wait(5), "loop did not release the in-flight write"
        replace(source, target)
        writers.append(threading.get_ident())

    monkeypatch.setattr(loop_watchdog.os, "replace", record_replace)
    wd = LoopStallWatchdog(asyncio.get_running_loop(), threshold_s=5, ping_interval_s=0.01)
    write_heartbeat = wd._write_heartbeat

    def completed_write(now: float, *, stopped_at: float | None = None) -> None:
        nonlocal wakeups
        write_heartbeat(now, stopped_at=stopped_at)
        with progressed:
            wakeups += 1
            progressed.notify_all()

    monkeypatch.setattr(wd, "_write_heartbeat", completed_write)

    def wait_for(predicate: Callable[[], bool]) -> None:
        with progressed:
            # This covers real fsync/replace, not the watchdog's tick latency.
            # Shared disks can exceed 5s; completion and tick assertions below
            # remain the correctness gates, with a bounded 30s deadlock guard.
            assert progressed.wait_for(predicate, timeout=30), (
                f"watchdog did not progress\n{_watchdog_stack(wd._thread)}"
            )

    wd.start()
    try:
        await asyncio.to_thread(wait_for, lambda: len(writers) >= 2)
        first_tick = json.loads(path.read_text())["last_tick"]
        await asyncio.to_thread(wait_for, lambda: wd._heartbeat_written_tick > first_tick)
        assert json.loads(path.read_text())["last_tick"] > first_tick
        if gate_io:
            # Acknowledge an actual in-flight write instead of sleeping 300 ms.
            hold_write.set()
            assert await asyncio.to_thread(write_entered.wait, 5)
            assert wd._heartbeat_written_tick != wd._last_tick
        # From here the loop cannot answer another ping. Release the held write,
        # drain the final real pong, then acknowledge three no-new-tick wakeups.
        with wd._lock:
            final_tick = wd._last_tick
        release_write.set()
        wait_for(lambda: wd._heartbeat_written_tick == final_tick)
        during = path.read_bytes(), path.stat().st_mtime_ns
        with progressed:
            after_wakeups = wakeups + 3
        wait_for(lambda: wakeups >= after_wakeups)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == during
        stalled_tick = json.loads(during[0])["last_tick"]
        await asyncio.to_thread(wait_for, lambda: wd._heartbeat_written_tick > stalled_tick)
        assert json.loads(path.read_text())["last_tick"] > stalled_tick
    finally:
        release_write.set()
        wd.stop()
    assert writers and loop_thread not in writers
    assert heartbeat_fsync == writers, "each atomic replace must follow off-loop fsync"
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
@pytest.mark.parametrize("gate_stack_capture", [False, True], ids=["normal", "assertion-frame"])
async def test_heartbeat_failure_cannot_block_durable_stall_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_logs: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
    gate_stack_capture: bool,
) -> None:
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
    handler_read_entered = threading.Event()
    stacks_captured = threading.Event()
    warning_logged = asyncio.Event()
    warnings: list[logging.LogRecord] = []
    # The dump includes source lines. A literal on the assertion's blocked frame
    # can masquerade as a second warning in the final file count (#618).
    warning_message = "Could not persist loop heartbeat"
    replace = loop_watchdog.os.replace
    emit = handler.emit

    def observed_emit(record: logging.LogRecord) -> None:
        emit(record)
        if record.levelno == logging.WARNING:
            warnings.append(record)
            warning_logged.set()  # The deferred warning runs on the loop thread.

    monkeypatch.setattr(handler, "emit", observed_emit)

    def fail_replace(source: Path, target: Path) -> None:
        if inject_failure.is_set():
            write_failed.set()
            raise OSError("heartbeat disk unavailable")
        replace(source, target)

    monkeypatch.setattr(loop_watchdog.os, "replace", fail_replace)
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(), threshold_s=0.08, ping_interval_s=0.01, log=log,
    )
    format_stacks = wd.format_thread_stacks

    def gated_stacks() -> str:
        assert handler_read_entered.wait(2), "loop did not reach the locked-handler assertion"
        try:
            return format_stacks()  # Real stacks, including that assertion's source line.
        finally:
            stacks_captured.set()

    def locked_handler_text() -> str:
        if gate_stack_capture:
            handler_read_entered.set()
            assert stacks_captured.wait(2), "watchdog did not capture the assertion frame"
        return handler_path.read_text()

    if gate_stack_capture:
        monkeypatch.setattr(wd, "format_thread_stacks", gated_stacks)

    def stall_while_holding_logging_lock() -> None:
        handler.acquire()  # Held by the loop, not the watchdog's OS thread.
        try:
            inject_failure.set()
            wd._pong()  # A new successful tick must attempt persistence.
            assert write_failed.wait(2), "heartbeat failure was not exercised"
            deadline = time.monotonic() + 2
            while (
                "Event loop stalled for" not in freeze_path.read_text()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert "Event loop stalled for" in freeze_path.read_text()
            assert wd.stall_count == 1
            assert warning_message not in locked_handler_text()
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
        await asyncio.wait_for(warning_logged.wait(), timeout=5)
    finally:
        wd.stop()
        log.removeHandler(handler)
        handler.close()
    assert [record.getMessage() for record in warnings] == [warning_message]
    assert handler_path.read_text().count(warning_message) == 1


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
import asyncio, sys, threading, traceback
from collections.abc import Callable
from pathlib import Path
from app.core import file_logging, loop_watchdog

file_logging.get_log_dir = lambda: Path(sys.argv[1])
loop_watchdog.HEARTBEAT_INTERVAL_S = 0


async def main() -> None:
    progressed = threading.Condition()
    wakeups = 0
    replace = loop_watchdog.os.replace

    def delayed_replace(source: str, target: str) -> None:
        # Exercise writes that outlive the parent's former 100 ms flush window.
        threading.Event().wait(float(sys.argv[2]))
        replace(source, target)

    loop_watchdog.os.replace = delayed_replace
    wd = loop_watchdog.LoopStallWatchdog(asyncio.get_running_loop(), ping_interval_s=0.01)
    write_heartbeat = wd._write_heartbeat

    def completed_write(now: float, *, stopped_at: float | None = None) -> None:
        nonlocal wakeups
        write_heartbeat(now, stopped_at=stopped_at)
        with progressed:
            wakeups += 1
            progressed.notify_all()

    def wait_for(predicate: Callable[[], bool]) -> None:
        with progressed:
            # Persisting a tick includes real fsync/replace on a shared disk.
            # Match the in-process test's I/O guard, not a 5s latency assertion.
            if not progressed.wait_for(predicate, timeout=30):
                frame = sys._current_frames().get(wd._thread.ident)
                raise AssertionError("heartbeat did not progress\n" + "".join(
                    traceback.format_stack(frame) if frame is not None else ["<thread exited>"]
                ))

    wd._write_heartbeat = completed_write
    wd.start()
    await asyncio.to_thread(wait_for, lambda: wd._heartbeat_written_tick is not None)
    # From here until SIGKILL the event loop cannot answer another real ping.
    with wd._lock:
        final_tick = wd._last_tick
    wait_for(lambda: wd._heartbeat_written_tick == final_tick)
    print("stalled", flush=True)  # Acknowledges the completed final pre-stall write.
    assert sys.stdin.readline().strip() == "sampled"
    with progressed:
        after_wakeups = wakeups + 10
    wait_for(lambda: wakeups >= after_wakeups)
    print("woke", flush=True)  # More writes/checks ran, without a new loop tick.
    threading.Event().wait(30)


asyncio.run(main())
"""


@pytest.mark.parametrize(
    "write_delay", [0, 0.3, 5.1], ids=["normal-io", "delayed-io", "slow-start-io"],
)
def test_killed_stalled_process_leaves_a_boot_readable_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, write_delay: float,
) -> None:
    from app.core import loop_watchdog

    child = subprocess.Popen(
        [sys.executable, "-c", _HEARTBEAT_CHILD, str(tmp_path), str(write_delay)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "stalled"
        path = tmp_path / "loop-heartbeat.json"
        frozen = path.read_bytes()
        child.stdin.write("sampled\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "woke"
        assert path.read_bytes() == frozen
    finally:
        child.kill()  # no Python cleanup / watchdog stop; SIGKILL on POSIX
        child.wait(timeout=5)
        child.stdin.close()
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
