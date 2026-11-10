"""A membership change that fences this worker stops its running connectors."""
import asyncio

from app.core.connectors import manager as manager_mod
from app.network import ownership, startup


class _FakeManager:
    """Records the RUN/STOP the reconcile hands out; the lanes are not modelled."""

    def __init__(self, running, connecting=()):
        self.running = dict(running)
        self.connecting = set(connecting)  # a connect in flight: held, not registered
        self.stopped = []
        self.started = 0
        self.events = []

    def held_connector_ids(self):
        return set(self.running) | self.connecting

    def request_stop(self, connector_id):
        if connector_id == "boom":
            raise RuntimeError("stop request failed")
        self.stopped.append(connector_id)
        self.events.append(("stop", connector_id))
        self.running.pop(connector_id, None)
        self.connecting.discard(connector_id)

    async def start_all(self, wait=True):
        assert wait is False  # the reconcile never waits for a connect
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
    """R8-7 / #1398: a start whose connect() is in flight is not registered yet;
    the fence asks it to stop too (its lane cancels the connect), instead of
    sweeping the registry until it registers."""
    fake = _FakeManager({"a": object()}, connecting={"late"})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    monkeypatch.setattr(ownership, "can_execute_local", lambda: False)

    await startup._reconcile_owned_connectors()

    assert fake.stopped == ["a", "late"]
    assert fake.held_connector_ids() == set()


async def test_fence_then_readmission_apply_in_order(monkeypatch):
    """R8-7: two reconciles scheduled back to back ran concurrently; the
    fence's slow disconnect finished after the re-admission's start and left
    an eligible worker with its connector stopped. Decisions are still made
    in order; each connector's lane applies the latest one."""
    fake = _FakeManager({"a": object()})
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    answers = iter([False])
    monkeypatch.setattr(ownership, "can_execute_local", lambda: next(answers, True))

    await asyncio.gather(startup._reconcile_owned_connectors(), startup._reconcile_owned_connectors())

    assert fake.events == [("stop", "a"), ("start_all",)]
    assert "a" in fake.running
