"""Event-loop stall watchdog: a blocked loop gets its stack dumped."""

import asyncio
import logging
import threading
import time

import pytest

from app.core.loop_watchdog import LoopStallWatchdog, _threshold_from_env


def _wait_for_watchdog_test(stop: threading.Event) -> None:
    stop.wait()


@pytest.fixture
def diagnostic_logs(tmp_path, monkeypatch):
    from app.core import file_logging

    fatal_path = tmp_path / "fatal.log"
    app_path = tmp_path / "app.log"
    handler = logging.FileHandler(app_path, encoding="utf-8")
    log = logging.getLogger("app.core.loop_watchdog")
    log.addHandler(handler)
    stop = threading.Event()
    thread = threading.Thread(target=_wait_for_watchdog_test, args=(stop,), name="test-idle-thread")
    thread.start()
    try:
        with fatal_path.open("w", encoding="utf-8") as stream:
            monkeypatch.setattr(file_logging, "_fatal_file", stream)
            yield app_path, fatal_path
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
        # Give the watchdog a tick to observe the answered ping.
        await asyncio.sleep(0.2)
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
    app_path, fatal_path = diagnostic_logs
    for path in (app_path, fatal_path):
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
    _, fatal_path = diagnostic_logs

    class CheckingLogger:
        def error(self, *args):
            assert "test_durable_dump_precedes_logger_locks" in fatal_path.read_text(encoding="utf-8")

    loop = asyncio.new_event_loop()
    try:
        wd = LoopStallWatchdog(loop, log=CheckingLogger())
        wd._dump(10, first=True)
        assert wd.dump_count == 1
    finally:
        loop.close()
