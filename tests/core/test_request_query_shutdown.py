"""Shutdown retires raw engine reads, not the application's saving tail."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import query_shutdown as shutdown


@pytest.fixture(autouse=True)
def fresh_shutdown(monkeypatch):
    monkeypatch.setattr(shutdown, "_closing_loop", None)
    monkeypatch.setattr(shutdown, "_turns", set())
    monkeypatch.setattr(shutdown, "_retired", set())


async def test_stalled_read_retires_before_uncancelled_saving_tail():
    reading, saving, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fenced = []

    async def events():
        reading.set()
        await asyncio.Event().wait()
        yield "unreachable"

    async def query():
        async with shutdown.turn():
            with pytest.raises(shutdown.QueryShutdown):
                await shutdown.engine_event(events(), lambda: fenced.append(True))
            saving.set()
            await asyncio.sleep(.02)
            finished.set()

    owner = asyncio.create_task(query())
    await reading.wait()
    await shutdown.settle(grace=0)
    await owner
    assert fenced == [True]
    assert saving.is_set() and finished.is_set()
    assert not owner.cancelled()


async def test_deadline_does_not_cancel_an_issued_app_write():
    writing, release = asyncio.Event(), asyncio.Event()
    done = []

    async def query():
        async with shutdown.turn():
            writing.set()
            await release.wait()
            done.append("saved")

    owner = asyncio.create_task(query())
    await writing.wait()
    stopping = asyncio.create_task(shutdown.settle(grace=0))
    await asyncio.sleep(.02)
    assert not stopping.done() and not owner.done()
    release.set()
    await stopping
    await owner
    assert done == ["saved"]


async def test_late_raw_reader_inherits_shutdown_but_app_writes_do_not():
    writing, release = asyncio.Event(), asyncio.Event()
    fenced = []

    async def events():
        await asyncio.Event().wait()
        yield None

    async def query():
        async with shutdown.turn():
            writing.set()
            await release.wait()
            with pytest.raises(shutdown.QueryShutdown):
                await shutdown.engine_event(events(), lambda: fenced.append(True))

    owner = asyncio.create_task(query())
    await writing.wait()
    stopping = asyncio.create_task(shutdown.settle(grace=0))
    await asyncio.sleep(.02)
    release.set()
    await stopping
    await owner
    assert fenced == [True]
    with pytest.raises(RuntimeError, match="shutting down"):
        async with shutdown.turn():
            pytest.fail("admitted after shutdown")


async def test_cancellation_resistant_raw_read_is_fenced_and_reaped():
    reading, release = asyncio.Event(), asyncio.Event()
    fenced = []

    async def events():
        reading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        yield "late engine event"

    async def query():
        async with shutdown.turn():
            with pytest.raises(shutdown.QueryShutdown):
                await shutdown.engine_event(events(), lambda: fenced.append(True))

    owner = asyncio.create_task(query())
    await reading.wait()
    await shutdown.settle(grace=0)
    await owner
    assert fenced == [True]
    assert shutdown._retired
    release.set()
    await asyncio.gather(*shutdown._retired)
    await asyncio.sleep(0)
    assert not shutdown._retired


async def test_windows_pre_stop_also_settles_unpooled_queries(monkeypatch):
    from app.api import internal
    from app.core import desktop_lifecycle

    monkeypatch.setattr(internal, "require_desktop", lambda request: None)
    monkeypatch.setattr(internal, "_shutdown_task", None)
    pooled_stop = AsyncMock()
    monkeypatch.setattr(desktop_lifecycle, "stop_running_sessions", pooled_stop)
    reading = asyncio.Event()
    saved = []

    async def events():
        reading.set()
        await asyncio.Event().wait()
        yield None

    async def headless():
        async with shutdown.turn():
            with pytest.raises(shutdown.QueryShutdown):
                await shutdown.engine_event(events())
            saved.append("terminal turn and memory")

    task = asyncio.create_task(headless())
    await reading.wait()
    assert await internal.desktop_stop_sessions(SimpleNamespace()) == {"stopped": True}
    await task
    assert saved == ["terminal turn and memory"]
    pooled_stop.assert_awaited_once()


@pytest.mark.parametrize("value,expected", [
    (None, 6.5), ("30", 26.5), ("15", 11.5), ("10", 6.5),
    ("bad", 6.5), ("nan", 6.5), ("inf", 6.5), ("0", 6.5), ("31", 6.5),
])
def test_supervisor_budget_scales_only_the_finite_drain(value, expected):
    environ = {} if value is None else {"SHUFFLE_SUPERVISOR_STOP_SECONDS": value}
    assert shutdown.configured_grace_seconds(environ) == expected


async def test_windows_does_not_acknowledge_clean_stop_with_unfinished_writes(monkeypatch):
    # The end-to-end slow-saving limit is tracked in #1305. This endpoint must
    # at least refuse a clean-stop acknowledgement while such work remains.
    from app.api import internal
    from app.core import desktop_lifecycle

    monkeypatch.setattr(internal, "require_desktop", lambda request: None)
    monkeypatch.setattr(internal, "_shutdown_task", None)
    monkeypatch.setattr(desktop_lifecycle, "stop_running_sessions", AsyncMock())
    monkeypatch.setattr(shutdown, "SETTLE_SECONDS", .01)
    writing, release = asyncio.Event(), asyncio.Event()

    async def headless():
        async with shutdown.turn():
            writing.set()
            await release.wait()

    task = asyncio.create_task(headless())
    await writing.wait()
    try:
        with pytest.raises(RuntimeError, match="not finished saving"):
            await internal.desktop_stop_sessions(SimpleNamespace())
        assert not task.done()  # The reserve itself did not cancel the write.
    finally:
        release.set()
        await task
