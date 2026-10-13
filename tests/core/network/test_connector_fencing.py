"""A membership change that fences this worker stops its running connectors."""
import asyncio

import pytest

from app.core.connectors import manager as manager_mod
from app.network import ownership, startup


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch):
    monkeypatch.setattr(startup, "FENCE_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(startup, "FENCE_SWEEP_INTERVAL", 0.01)


class _FakeManager:
    def __init__(self, running, stop_delay=0.0):
        self.running = dict(running)
        self.stopped = []
        self.started = 0
        self.events = []
        self.stop_delay = stop_delay

    def get_running_connectors(self, connector_type=None):
        yield from list(self.running.items())

    async def stop_connector(self, connector_id):
        if connector_id == "boom":
            raise RuntimeError("disconnect failed")
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)  # a disconnect takes a moment
        self.stopped.append(connector_id)
        self.events.append(("stop", connector_id))
        self.running.pop(connector_id, None)

    async def start_all(self):
        self.started += 1
        self.events.append(("start_all",))
        self.running.setdefault("a", object())


async def test_fenced_worker_stops_every_running_connector(monkeypatch):
    fake = _FakeManager({"a": object(), "boom": object(), "b": object()})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    monkeypatch.setattr(ownership, "can_execute_local", lambda: False)

    await startup._reconcile_owned_connectors()

    assert fake.stopped == ["a", "b"]
    assert fake.started == 0


async def test_eligible_worker_starts_its_connectors(monkeypatch):
    fake = _FakeManager({})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    monkeypatch.setattr(ownership, "can_execute_local", lambda: True)

    await startup._reconcile_owned_connectors()

    assert fake.started == 1
    assert fake.stopped == []


async def test_fence_stops_a_connector_that_was_still_connecting(monkeypatch):
    """R8-7: a start already past its eligibility check awaits connect() and
    registers only afterwards; one pass over the registry missed it and the
    connector kept running on the fenced worker."""
    fake = _FakeManager({"a": object()})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    monkeypatch.setattr(ownership, "can_execute_local", lambda: False)

    async def in_flight_start():
        await asyncio.sleep(0.05)  # connect() returns after the fence's first pass
        fake.running["late"] = object()

    starting = asyncio.create_task(in_flight_start())
    await startup._reconcile_owned_connectors()
    await starting

    assert fake.stopped == ["a", "late"]
    assert fake.running == {}


async def test_readmission_ends_the_fence_sweep(monkeypatch):
    monkeypatch.setattr(startup, "FENCE_SETTLE_SECONDS", 30.0)
    fake = _FakeManager({"a": object()})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    answers = iter([False])
    monkeypatch.setattr(ownership, "can_execute_local", lambda: next(answers, True))

    await asyncio.wait_for(startup._reconcile_owned_connectors(), 2.0)

    assert fake.stopped == ["a"]


async def test_fence_then_readmission_apply_in_order(monkeypatch):
    """R8-7: two reconciles scheduled back to back ran concurrently; the
    fence's slow disconnect finished after the re-admission's start and left
    an eligible worker with its connector stopped."""
    fake = _FakeManager({"a": object()}, stop_delay=0.05)
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    answers = iter([False])
    monkeypatch.setattr(ownership, "can_execute_local", lambda: next(answers, True))

    await asyncio.gather(startup._reconcile_owned_connectors(), startup._reconcile_owned_connectors())

    assert fake.events == [("stop", "a"), ("start_all",)]
    assert "a" in fake.running
