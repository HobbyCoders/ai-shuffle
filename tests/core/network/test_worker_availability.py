"""Availability overrides replicate as scoped settings, never as fleet defaults."""
from collections.abc import Callable

from pytest import MonkeyPatch

from app.core import worker_availability as availability
from app.core.config import settings
from app.db import database
from app.network import engine, transport
from tests.core.network.harness import Worker, exchange, join


def test_override_sync_preserves_per_worker_scope(
    workers: Callable[[str], Worker], monkeypatch: MonkeyPatch,
) -> None:
    desktop, server = workers("DesktopWorker"), workers("ServerWorker")
    with desktop.active():
        engine.create_network("Availability test")
    join(desktop, server)
    with desktop.active():
        monkeypatch.setattr(settings, "instance_id", desktop.name)
        monkeypatch.setattr(settings, "local_mode", True)
        monkeypatch.setenv("SHUFFLE_DESKTOP", "1")
        availability.save(True)
        key = availability.setting_key()
        engine.scan_collection("system_settings")
        # The same producer used by HTTP exchange and heartbeats advertises it.
        caps = transport._capabilities()
        assert caps["desktop"] is True and caps["always_on"] is True
    exchange(desktop, server)
    with server.active():
        monkeypatch.setattr(settings, "instance_id", server.name)
        monkeypatch.setattr(settings, "local_mode", False)
        assert database.get_system_setting(key) == "true"
        assert availability.snapshot()["always_on_override"] is None
        availability.save(False)
        engine.scan_collection("system_settings")
        assert transport._capabilities()["always_on"] is False
    exchange(server, desktop)
    with desktop.active():
        monkeypatch.setattr(settings, "instance_id", desktop.name)
        monkeypatch.setattr(settings, "local_mode", True)
        assert database.get_system_setting("worker_always_on:ServerWorker") == "false"
        assert availability.snapshot()["always_on"] is True
        availability.save(None)
        engine.scan_collection("system_settings")
        assert transport._capabilities()["always_on"] is False
    exchange(desktop, server)
    with server.active():
        assert database.get_system_setting(key) is None
