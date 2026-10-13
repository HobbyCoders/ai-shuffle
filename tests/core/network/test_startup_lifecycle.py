"""Network start/stop keeps exactly one apply subscription per lifecycle (#2097)."""
import asyncio

import pytest

from app.core import managed_connection
from app.network import engine, enrollment, hub, startup, startup_sync, transport

TOUCHED = [("owner_prefs", "theme", "upsert")]


@pytest.fixture
def lifecycle(monkeypatch):
    """Real start()/stop() with the network side effects stubbed out."""
    calls = []
    monkeypatch.setattr(engine, "_listeners", [])
    monkeypatch.setattr(startup, "_tasks", [])
    monkeypatch.setattr(startup, "_unsubscribe_applied", None)
    monkeypatch.setattr(startup, "_on_applied", lambda touched: calls.append(touched))
    monkeypatch.setattr(transport, "start", list)
    monkeypatch.setattr(enrollment, "resume_enrollment", lambda: None)
    monkeypatch.setattr(hub, "start", list)

    async def idle():
        await asyncio.Event().wait()

    async def done():
        return None

    monkeypatch.setattr(managed_connection, "supervise", idle)
    monkeypatch.setattr(transport, "stop", done)
    monkeypatch.setattr(hub, "stop", done)
    monkeypatch.setattr(startup_sync, "stop", done)
    return calls


async def test_repeated_lifecycles_deliver_each_apply_once(lifecycle):
    counts = [len(engine._listeners)]
    for _ in range(3):
        await startup.start()
        counts.append(len(engine._listeners))
        engine._notify(TOUCHED)
        await startup.stop()
        counts.append(len(engine._listeners))
    assert counts == [0, 1, 0, 1, 0, 1, 0]
    assert lifecycle == [TOUCHED] * 3
    engine._notify(TOUCHED)
    assert len(lifecycle) == 3


async def test_stop_keeps_unrelated_listeners(lifecycle):
    other = []
    engine.on_applied(other.append)
    await startup.start()
    await startup.stop()
    engine._notify(TOUCHED)
    assert other == [TOUCHED]
    assert lifecycle == []


async def test_start_without_stop_does_not_double_the_subscription(lifecycle):
    await startup.start()
    await startup.start()
    engine._notify(TOUCHED)
    assert lifecycle == [TOUCHED]
    await startup.stop()
    assert engine._listeners == []


async def test_failed_start_removes_its_subscription(lifecycle, monkeypatch):
    def broken():
        raise RuntimeError("transport down")

    monkeypatch.setattr(transport, "start", broken)
    with pytest.raises(RuntimeError, match="transport down"):
        await startup.start()
    assert engine._listeners == []
    monkeypatch.setattr(transport, "start", list)
    await startup.start()
    engine._notify(TOUCHED)
    assert lifecycle == [TOUCHED]
    await startup.stop()


async def test_start_after_an_engine_reset_subscribes_again(lifecycle):
    await startup.start()
    engine._listeners.clear()
    await startup.stop()
    await startup.start()
    engine._notify(TOUCHED)
    assert lifecycle == [TOUCHED]
    await startup.stop()


def test_unsubscribe_removes_one_registration_once(monkeypatch):
    monkeypatch.setattr(engine, "_listeners", [])

    def listener(touched):
        return None

    first = engine.on_applied(listener)
    engine.on_applied(listener)
    first()
    first()
    assert engine._listeners == [listener]
