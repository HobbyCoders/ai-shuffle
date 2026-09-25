"""A membership change that fences this worker stops its running connectors."""
from app.core.connectors import manager as manager_mod
from app.network import ownership, startup


class _FakeManager:
    def __init__(self, running):
        self.running = dict(running)
        self.stopped = []
        self.started = 0

    def get_running_connectors(self, connector_type=None):
        yield from list(self.running.items())

    async def stop_connector(self, connector_id):
        if connector_id == "boom":
            raise RuntimeError("disconnect failed")
        self.stopped.append(connector_id)
        self.running.pop(connector_id, None)

    async def start_all(self):
        self.started += 1


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
