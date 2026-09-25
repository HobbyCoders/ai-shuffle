"""Desired plugin state between workers whose installs differ."""
from app.db import network as store
from app.network import engine, plugins_sync
from tests.core.network.harness import converge, join


class FakePlugin:
    def __init__(self, pid, version, enabled=True, scope="user"):
        self.id, self.version, self.enabled, self.scope = pid, version, enabled, scope


class FakeService:
    def __init__(self, installed=()):
        self.installed = {p.id: p for p in installed}
        self.enabled = {p.id: p.enabled for p in installed}
        self.calls = []
        self.fail = set()

    def get_installed_plugins(self):
        return list(self.installed.values())

    def get_enabled_plugins(self):
        return dict(self.enabled)

    def install_plugin(self, plugin_id, scope="user"):
        self.calls.append(("install", plugin_id))
        if "install" in self.fail:
            raise RuntimeError("marketplace offline")
        self.installed[plugin_id] = FakePlugin(plugin_id, "9.9.9")
        return self.installed[plugin_id]

    def uninstall_plugin(self, plugin_id):
        self.calls.append(("uninstall", plugin_id))
        self.installed.pop(plugin_id, None)

    def enable_plugin(self, plugin_id):
        self.calls.append(("enable", plugin_id))
        self.enabled[plugin_id] = True

    def disable_plugin(self, plugin_id):
        self.calls.append(("disable", plugin_id))
        self.enabled[plugin_id] = False


def _desired_ops(worker):
    with worker.active():
        return [o for o in store.ops_since({}) if o["collection"] == "plugins_desired"]


def test_workers_with_different_installed_versions_stop_republishing(workers, monkeypatch):
    """Version is not enforced by ``apply_desired``; it must not flip-flop between workers.

    A plugin installed months ago on A (1.0.0) and fresh on B (2.0.0) used to
    make every full scan on each worker rewrite the shared row with its own
    version, so the pair exchanged a new op for the same plugin forever.
    """
    a, b = workers("a"), workers("b")
    services = {"a": FakeService([FakePlugin("tools@official", "1.0.0")]),
                "b": FakeService([FakePlugin("tools@official", "2.0.0")])}
    current = {"name": "a"}
    monkeypatch.setattr(plugins_sync, "_service", lambda: services[current["name"]])
    with a.active():
        engine.create_network("Home", device_name="A")
        assert plugins_sync.publish() == 1
    join(a, b)
    for _round in range(3):
        for name, worker in (("a", a), ("b", b)):
            current["name"] = name
            with worker.active():
                plugins_sync.publish()
        converge([a, b])
    # Only A's original publish exists; B (bootstrapped from a snapshot) never rewrote it.
    assert len(_desired_ops(a)) == 1
    assert not [o for o in _desired_ops(b) if o["origin_device_id"] == b.device_id]
    # A real change (disabling) still propagates.
    current["name"] = "b"
    services["b"].enabled["tools@official"] = False
    with b.active():
        assert plugins_sync.publish() == 1
    converge([a, b])
    current["name"] = "a"
    with a.active():
        plugins_sync.apply_desired(["tools@official"])
    assert services["a"].calls[-1] == ("disable", "tools@official")


def test_apply_desired_uninstalls_tombstoned_and_survives_failures(workers, monkeypatch):
    a = workers("a")
    service = FakeService([FakePlugin("gone@x", "1"), FakePlugin("proj@x", "1", scope="project")])
    monkeypatch.setattr(plugins_sync, "_service", lambda: service)
    with a.active():
        engine.create_network("Home", device_name="A")
        # Project-scoped plugins stay local.
        assert set(plugins_sync._installed()) == {"gone@x"}
        assert plugins_sync.apply_desired([]) == 0
        # No desired row: an installed plugin is uninstalled; a missing one is left alone.
        assert plugins_sync.apply_desired(["gone@x", "never@x"]) == 1
        assert ("uninstall", "gone@x") in service.calls
        a.sql("INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at) "
              "VALUES ('new@x', '1', 1, '{}', CURRENT_TIMESTAMP)")
        service.fail.add("install")
        assert plugins_sync.apply_desired(["new@x"]) == 0  # logged, sync goes on

    def broken():
        raise RuntimeError("plugin service unavailable")

    monkeypatch.setattr(plugins_sync, "_service", broken)
    with a.active():
        assert plugins_sync._installed() == {}
        assert plugins_sync.apply_desired(["new@x"]) == 0


def test_publish_waits_for_a_ready_network(workers, monkeypatch):
    a = workers("a")
    monkeypatch.setattr(plugins_sync, "_service", lambda: FakeService([FakePlugin("p@x", "1")]))
    with a.active():
        assert plugins_sync.publish() == 0
