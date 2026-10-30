"""Explicit uninstall intent wins over an un-reconciled installed inventory."""
import asyncio

import pytest

from app.api import plugins as api
from app.core.plugin_service import PluginService
from app.db import network as store
from app.network import engine, plugins_sync
from tests.core.network.harness import exchange, exchange_with_gap, join
from tests.core.network.test_plugins_sync_edges import FakePlugin, FakeService

PID = "tools@official"


@pytest.fixture(autouse=True)
def clear_outbox_hold(monkeypatch):
    monkeypatch.setattr(engine, "_outbox_held", False)


class Service(FakeService):
    def get_installation_records(self, plugin_id):
        plugin = self.installed.get(plugin_id)
        return [{"scope": plugin.scope}] if plugin else []

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


@pytest.mark.parametrize("delivery", ["ordered", "gap"])
def test_api_uninstall_tombstone_cannot_be_resurrected_by_stale_inventory(workers, monkeypatch, delivery):
    a, b = workers("a"), workers("b")
    inventories, current = services(monkeypatch)
    with a.active():
        engine.create_network("Plugins")
        assert plugins_sync.publish() == 1
    join(a, b)
    with a.active():
        if delivery == "gap":
            from app.db import database as db
            for index in range(2):
                db.set_system_setting(f"gap_buffer_probe_{index}", "held-prefix")
            engine.scan_collection("system_settings")
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        # The same selected pass requested by the post-commit hook publishes it.
        assert engine.scan_all(collections=["plugins_desired"]) == 1
    assert deleted(a)["deleted"]
    deliver = exchange_with_gap if delivery == "gap" else exchange
    deliver(a, b)
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


def test_project_only_uninstall_keeps_peers_shared_install(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    inventories, current = services(monkeypatch)
    inventories["b"].installed[PID].scope = "project"
    with a.active():
        engine.create_network("Scopes")
        plugins_sync.publish()
    join(a, b)
    current["name"] = "b"
    with b.active():
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        assert plugins_sync.publish() == 0
        assert not deleted(b)["deleted"]
    exchange(b, a)
    current["name"] = "a"
    with a.active():
        plugins_sync.apply_desired([PID])
        assert PID in inventories["a"].installed
        assert not deleted(a)["deleted"]


@pytest.mark.parametrize("failed_save_first", [False, True])
def test_owner_approval_publishes_pending_agent_reinstall(workers, monkeypatch, tmp_path, failed_save_first):
    from app.core import plugin_install, plugin_service
    from app.mcp import plugin_tools

    a, b = workers("a"), workers("b")
    inventories, current = services(monkeypatch)
    with a.active():
        engine.create_network("Approved reinstall")
        plugins_sync.publish()
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        plugins_sync.publish()
    join(a, b)
    assert deleted(a)["deleted"]
    current["name"] = "b"
    with b.active():
        assert plugins_sync.apply_desired([PID]) == 1
    current["name"] = "a"
    monkeypatch.setattr(plugin_service, "_get_fluid_dir", lambda: tmp_path / "fluid")
    service = PluginService()
    monkeypatch.setattr(plugin_tools, "_service", lambda: service)
    monkeypatch.setattr(plugin_tools, "install_refusal", lambda _: None)
    monkeypatch.setattr(api, "get_plugin_service", lambda: service)
    monkeypatch.setattr(plugins_sync, "_service", lambda: service)

    def fetch_plugin(svc, plugin_id, scope, enable, requested_by):
        # Only the marketplace download is stubbed. The real agent handler,
        # provenance producer, registry and owner-enable path are exercised.
        record = {"scope": scope, "version": "2.0", "installPath": str(svc.CACHE_DIR / "tools")}
        record.update(plugin_install._agent_request(None, requested_by))
        svc._write_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {plugin_id: [record]}})
        svc._write_json_file(svc.SETTINGS_FILE, {"enabledPlugins": {plugin_id: enable}})
        return svc.get_installed_plugins()[0]

    monkeypatch.setattr(plugin_install, "install_plugin", fetch_plugin)
    with a.active():
        plugin_tools.install_listed(PID, {"session_id": "throwaway", "agent_id": "test"})
        assert service.get_installed_plugins()[0].approval == "pending"
        assert plugins_sync.publish() == 0
        assert deleted(a)["deleted"]
        if failed_save_first:
            writer = service._write_json_file
            monkeypatch.setattr(service, "_write_json_file", lambda path, data: False)
            with pytest.raises(api.HTTPException):
                asyncio.run(api.enable_plugin(PID, token="test"))
            assert service.get_installed_plugins()[0].approval == "pending"
            assert plugins_sync.publish() == 0
            assert deleted(a)["deleted"]
            monkeypatch.setattr(service, "_write_json_file", writer)
        asyncio.run(api.enable_plugin(PID, token="test"))
        assert service.get_installed_plugins()[0].approval is None
        assert plugins_sync.publish() == 1
        assert not deleted(a)["deleted"]
    exchange(a, b)
    monkeypatch.setattr(plugins_sync, "_service", lambda: inventories["b"])
    with b.active():
        assert not deleted(b)["deleted"]
        assert plugins_sync.apply_desired([PID]) == 1
        assert inventories["b"].get_enabled_plugins()[PID]


@pytest.mark.parametrize("records,shared", [
    ({"scope": "project"}, False),
    ([{"scope": "project"}, {"scope": "user"}], True),
    ({"version": "legacy"}, True),
    ([{"scope": "local"}], False),
])
def test_uninstall_reads_all_real_registry_scopes_before_removal(workers, monkeypatch, tmp_path, records, shared):
    from app.core import plugin_service
    a = workers("a")
    monkeypatch.setattr(plugin_service, "_get_fluid_dir", lambda: tmp_path / "fluid")
    service = PluginService()
    monkeypatch.setattr(api, "get_plugin_service", lambda: service)
    monkeypatch.setattr(plugins_sync, "_service", lambda: service)
    service._write_json_file(service.INSTALLED_FILE, {"version": 2, "plugins": {PID: records}})
    with a.active():
        engine.create_network("All installation scopes")
        asyncio.run(api.uninstall_plugin(PID, token="test"))
        assert service.get_installation_records(PID) == []
        assert plugins_sync.publish() == int(shared)
        assert bool(deleted(a)) == shared


def test_connector_package_uninstall_publishes_shared_tombstone(workers, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from app.core import connector_packages, plugin_service

    a, b = workers("a"), workers("b")
    current = {"name": "a"}
    monkeypatch.setattr(plugin_service, "_get_fluid_dir", lambda: tmp_path / current["name"] / "fluid")
    monkeypatch.setattr(connector_packages, "root", lambda: tmp_path / current["name"] / "connector-runtimes")
    inventories = {}
    for name in ("a", "b"):
        current["name"] = name
        inventories[name] = PluginService()
        # Use the installer's actual active-record writer, without downloading
        # a runtime. Uninstall preserves binaries and only removes this record.
        connector_packages._publish("gmail", {
            "release": connector_packages.CATALOG["release"], "enabled": True,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        })
    monkeypatch.setattr(api, "get_plugin_service", lambda: inventories[current["name"]])
    monkeypatch.setattr(plugins_sync, "_service", lambda: inventories[current["name"]])
    pid = connector_packages.package_id("gmail")
    current["name"] = "a"
    with a.active():
        engine.create_network("Connector packages")
        assert plugins_sync.publish() == 1
    join(a, b)
    with a.active():
        asyncio.run(api.uninstall_plugin(pid, token="test"))
        assert connector_packages.record("gmail") is None
        assert plugins_sync.publish() == 1
        assert store.get_record(a.shim.cursor(), "plugins_desired", pid)["deleted"]
    exchange(a, b)
    current["name"] = "b"
    with b.active():
        assert connector_packages.record("gmail") is not None
        assert plugins_sync.publish() == 0  # stale connector inventory cannot revive it
        assert plugins_sync.apply_desired([pid]) == 1
        assert connector_packages.record("gmail") is None
