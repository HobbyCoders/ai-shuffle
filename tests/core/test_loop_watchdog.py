"""Event-loop stall watchdog: a blocked loop gets its stack dumped."""

import asyncio
import logging
import time

import pytest

from app.core.loop_watchdog import LoopStallWatchdog, _threshold_from_env


def _block_the_loop_for(seconds: float) -> None:
    # A distinct frame name so the dump assertion can find it.
    time.sleep(seconds)


@pytest.mark.asyncio
async def test_blocked_loop_dumps_sleeping_frame(caplog):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.2,
        ping_interval_s=0.05,
        redump_interval_s=30.0,
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
    assert wd.dump_count == 1  # one stall, no 30 s re-dump window elapsed
    assert wd.last_stall_seconds is not None and wd.last_stall_seconds >= 0.2

    dumps = [r for r in caplog.records if "loop thread stack" in r.getMessage()]
    assert len(dumps) == 1
    text = dumps[0].getMessage()
    assert "_block_the_loop_for" in text
    assert "time.sleep" in text

    ended = [r for r in caplog.records if "stall ended after" in r.getMessage()]
    assert len(ended) == 1


@pytest.mark.asyncio
async def test_healthy_loop_never_dumps(caplog):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(), threshold_s=0.2, ping_interval_s=0.02
    )
    wd.start()
    try:
        for _ in range(10):
            await asyncio.sleep(0.03)
    finally:
        wd.stop()
    assert wd.stall_count == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_persistent_stall_redumps(caplog):
    caplog.set_level(logging.ERROR, logger="app.core.loop_watchdog")
    wd = LoopStallWatchdog(
        asyncio.get_running_loop(),
        threshold_s=0.1,
        ping_interval_s=0.02,
        redump_interval_s=0.15,
    )
    wd.start()
    try:
        await asyncio.sleep(0.05)
        _block_the_loop_for(0.5)
        await asyncio.sleep(0.1)
    finally:
        wd.stop()
    assert wd.stall_count == 1
    assert wd.dump_count >= 2
    still = [r for r in caplog.records if "still stalled" in r.getMessage()]
    assert still


def test_threshold_env(monkeypatch):
    monkeypatch.delenv("LOOP_STALL_THRESHOLD_S", raising=False)
    assert _threshold_from_env() == 3.0
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "7.5")
    assert _threshold_from_env() == 7.5
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "nope")
    assert _threshold_from_env() == 3.0
    monkeypatch.setenv("LOOP_STALL_THRESHOLD_S", "0")
    assert _threshold_from_env() == 3.0
