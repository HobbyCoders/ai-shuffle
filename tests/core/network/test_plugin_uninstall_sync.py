"""Explicit uninstall intent wins over an un-reconciled installed inventory."""
import asyncio

import pytest

from app.api import plugins as api
from app.core.plugin_service import PluginService
from app.db import network as store
from app.network import engine, plugins_sync
from tests.core.network.harness import exchange, join
from tests.core.network.test_plugins_sync_edges import FakePlugin, FakeService

PID = "tools@official"


@pytest.fixture(autouse=True)
def clear_outbox_hold(monkeypatch):
    monkeypatch.setattr(engine, "_outbox_held", False)


class Service(FakeService):
    def install_plugin(self, plugin_id, scope="user", enable=True):
        plugin = super().install_plugin(plugin_id, scope)
        plugin.enabled = enable
        self.enabled[plugin_id] = enable
        return plugin


def services(monkeypatch, names=("a", "b")):
    inventories = {name: Service([FakePlugin(PID, "1.0")]) for name in names}
    current = {"name": names[0]}
    get = lambda: inventories[current["name"]]
    monkeypatch.setattr(plugins_sync, "_service", get)
    monkeypatch.setattr(api, "get_plugin_service", get)
    return inventories, current


def deleted(worker):
    with worker.active():
        return store.get_record(worker.shim.cursor(), "plugins_desired", PID)


def test_api_uninstall_tombstone_cannot_be_resurrected_by_stale_inventory(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    inventories, current = services(monkeypatch)
    with a.active():
        engine.create_network("Plugins")
        assert plugins_sync.publish() == 1
    join(a, b)
    with a.active():
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        # The same selected pass requested by the post-commit hook publishes it.
        assert engine.scan_all(collections=["plugins_desired"]) == 1
    assert deleted(a)["deleted"]
    exchange(a, b)
    current["name"] = "b"
    with b.active():
        assert PID in inventories["b"].installed  # reconcile has not run yet
        assert plugins_sync.publish() == 0
        assert b.sql("SELECT id FROM network_plugins_desired") == []
        assert plugins_sync.apply_desired([PID]) == 1
        assert PID not in inventories["b"].installed
        assert plugins_sync.publish() == 0
    exchange(b, a)
    assert deleted(a)["deleted"] and deleted(b)["deleted"]


@pytest.mark.parametrize("held", [False, True])
def test_uninstall_before_first_publish_is_durable(workers, monkeypatch, held):
    a, b = workers("a"), workers("b")
    inventories, current = services(monkeypatch)
    inventories["a"].installed.clear()
    with a.active():
        engine.create_network("Early removal")
    join(a, b)
    inventories["a"].installed[PID] = FakePlugin(PID, "1.0")
    with a.active():
        assert deleted(a) is None
        engine.hold_outbox(held)
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        if held:
            assert plugins_sync.publish() == 0
            assert deleted(a) is None
            engine.hold_outbox(False)
        assert plugins_sync.publish() == 1
    exchange(a, b)
    current["name"] = "b"
    with b.active():
        assert plugins_sync.publish() == 0
        assert plugins_sync.apply_desired([PID]) == 1
    assert deleted(b)["deleted"]


def test_explicit_reinstall_can_replace_tombstone(workers, monkeypatch):
    a = workers("a")
    inventories, _ = services(monkeypatch, ("a",))
    with a.active():
        engine.create_network("Reinstall")
        plugins_sync.publish()
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        plugins_sync.publish()
        assert deleted(a)["deleted"]
        plugins_sync.install_plugin(inventories["a"], PID, "user", True)
        assert plugins_sync.publish() == 1
        assert not deleted(a)["deleted"]


def test_failed_uninstall_does_not_publish_removal(workers, monkeypatch):
    a = workers("a")
    inventories, _ = services(monkeypatch, ("a",))
    with a.active():
        engine.create_network("Failed removal")
        plugins_sync.publish()
        def fail(plugin_id):
            raise RuntimeError("unreadable registry")
        monkeypatch.setattr(inventories["a"], "uninstall_plugin", fail)
        with pytest.raises(RuntimeError, match="unreadable registry"):
            asyncio.run(api.uninstall_plugin(PID, token="test"))
        assert plugins_sync.publish() == 0
        assert not deleted(a)["deleted"]


def test_authored_plugins_remain_outside_desired_state(workers, monkeypatch):
    a = workers("a")
    services(monkeypatch, ("a",))
    with a.active():
        engine.create_network("Authored removal")
        asyncio.run(api.uninstall_plugin("draft@authored", token="test"))
        assert plugins_sync.publish() == 1  # only the unrelated marketplace plugin
        assert not a.sql("SELECT record_id FROM sync_records WHERE collection='plugins_desired' AND record_id='draft@authored'")


@pytest.mark.parametrize("scope,pending", [("project", False), ("user", True)])
def test_nonshared_install_does_not_replace_tombstone(workers, monkeypatch, scope, pending):
    a = workers("a")
    inventories, _ = services(monkeypatch, ("a",))
    service = inventories["a"]
    with a.active():
        engine.create_network("Local install")
        plugins_sync.publish()
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        plugins_sync.publish()
        original = service.install_plugin
        def install(plugin_id, scope, enable):
            plugin = original(plugin_id, scope, enable)
            plugin.scope = scope
            if pending:
                plugin.approval = "pending"
            return plugin
        monkeypatch.setattr(service, "install_plugin", install)
        plugins_sync.install_plugin(service, PID, scope, True)
        assert plugins_sync.publish() == 0
        assert deleted(a)["deleted"]


def test_batch_keeps_successful_reinstall_before_later_failure(workers, monkeypatch):
    a = workers("a")
    inventories, _ = services(monkeypatch, ("a",))
    service = inventories["a"]
    with a.active():
        engine.create_network("Partial batch")
        plugins_sync.publish()
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        plugins_sync.publish()
        original = service.install_plugin
        def install(plugin_id, scope, enable):
            if plugin_id == "bad@missing":
                raise ValueError("missing source")
            return original(plugin_id, scope, enable)
        monkeypatch.setattr(service, "install_plugin", install)
        with pytest.raises(ValueError, match="missing source"):
            PluginService.install_plugins_batch(service, [PID, "bad@missing"])
        assert plugins_sync.publish() == 1
        assert not deleted(a)["deleted"]
