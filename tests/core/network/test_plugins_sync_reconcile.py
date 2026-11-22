"""Installed plugins catch up on every worker, not only when a change arrives.

``apply_desired`` tries a received change once. A worker that joined later,
lacked the plugin's marketplace, or was offline at that moment never got the
plugin; ``reconcile_missing`` installs what is still missing, adding the
marketplace from the source the row carries.
"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.network import engine, plugins_sync
from tests.core.network.harness import converge, join
from tests.core.network.test_plugins_sync_edges import FakePlugin, FakeService

PID = "tidy@community"
URL = "https://github.com/example/community.git"


class Service(FakeService):
    def __init__(self, installed=(), marketplaces=()):
        super().__init__(installed)
        self.marketplaces = {m.id: m for m in marketplaces}

    def get_marketplaces(self):
        return list(self.marketplaces.values())

    def add_marketplace(self, url, name=None):
        self.calls.append(("add_marketplace", name, url))
        if "add_marketplace" in self.fail:
            raise RuntimeError("clone failed")
        self.marketplaces[name] = mp(name, url)
        return self.marketplaces[name]

    def remove_marketplace(self, marketplace_id):
        self.calls.append(("remove_marketplace", marketplace_id))
        if "remove_marketplace" in self.fail:
            return False
        self.marketplaces.pop(marketplace_id, None)
        for pid in [p for p in self.installed if p.endswith("@" + marketplace_id)]:
            self.installed.pop(pid)
        return True

    def install_plugin(self, plugin_id, scope="user", enable=True):
        self.calls.append(("install", plugin_id, enable))
        if "install" in self.fail:
            raise RuntimeError("marketplace offline")
        if plugin_id.rpartition("@")[2] not in self.marketplaces:
            raise ValueError("Marketplace not found")
        self.installed[plugin_id] = FakePlugin(plugin_id, "1.0", enabled=enable)
        self.enabled[plugin_id] = enable
        return self.installed[plugin_id]


def mp(mp_id, url, source="git"):
    return SimpleNamespace(id=mp_id, source=source, url=url)


@pytest.fixture(autouse=True)
def fresh_retries(monkeypatch):
    monkeypatch.setattr(plugins_sync, "_retry", {})
    monkeypatch.setattr(engine, "_outbox_held", False)


def network(workers, monkeypatch, a_service, b_service):
    a, b = workers("a"), workers("b")
    services = {"a": a_service, "b": b_service}
    current = {"name": "a"}
    monkeypatch.setattr(plugins_sync, "_service", lambda: services[current["name"]])
    with a.active():
        engine.create_network("Plugins")
        plugins_sync.publish()
    join(a, b)
    current["name"] = "b"
    return a, b, current


def desired_config(worker, plugin_id=PID):
    with worker.active():
        rows = worker.sql("SELECT config FROM network_plugins_desired WHERE id = ?", (plugin_id,))
    value = rows[0]["config"]
    return json.loads(value) if isinstance(value, str) else value


def test_late_worker_adds_the_marketplace_and_installs_what_it_missed(workers, monkeypatch):
    a_service = Service([FakePlugin(PID, "2.3.0")], [mp("community", URL)])
    b_service = Service()
    a, b, _ = network(workers, monkeypatch, a_service, b_service)
    assert desired_config(b)["marketplace"] == {"id": "community", "url": URL}
    with b.active():
        assert plugins_sync.reconcile_missing() == 1
        assert b_service.calls == [("add_marketplace", "community", URL), ("install", PID, True)]
        # Installed: nothing more to do, and nothing to publish (same enabled state).
        assert plugins_sync.reconcile_missing() == 0
        assert plugins_sync.publish() == 0


def test_a_failing_install_is_retried_later_not_on_every_scan(workers, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(plugins_sync.time, "monotonic", lambda: clock["now"])
    a_service = Service([FakePlugin(PID, "2.3.0")], [mp("community", URL)])
    b_service = Service()
    b_service.fail.add("add_marketplace")
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        assert plugins_sync.reconcile_missing() == 0
        assert plugins_sync.reconcile_missing() == 0
        assert len(b_service.calls) == 1  # waiting out the delay
        clock["now"] += plugins_sync._RETRY_FIRST_SECONDS
        assert plugins_sync.reconcile_missing() == 0
        assert len(b_service.calls) == 2
        clock["now"] += plugins_sync._RETRY_FIRST_SECONDS  # the delay doubled
        assert plugins_sync.reconcile_missing() == 0
        assert len(b_service.calls) == 2
        b_service.fail.clear()
        clock["now"] += plugins_sync._RETRY_FIRST_SECONDS
        assert plugins_sync.reconcile_missing() == 1
        assert PID in b_service.installed
        assert PID not in plugins_sync._retry


def test_a_failed_apply_is_caught_up_by_reconcile(workers, monkeypatch):
    """The original defect: a change applied once while the marketplace was missing stayed missing."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(plugins_sync.time, "monotonic", lambda: clock["now"])
    pid = "review@official"
    a_service = Service([FakePlugin(pid, "1.0")])  # no shareable source: B must add it itself
    b_service = Service()
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        assert plugins_sync.apply_desired([pid]) == 0  # "Marketplace not found", logged
        b_service.marketplaces["official"] = mp("official", "https://example.com/official.git")
        clock["now"] += plugins_sync._RETRY_FIRST_SECONDS
        assert plugins_sync.reconcile_missing() == 1
        assert pid in b_service.installed


def test_reconcile_never_uninstalls_toggles_or_overrides_a_removal(workers, monkeypatch):
    kept, removing = "kept@community", "removing@community"
    a_service = Service([FakePlugin(PID, "1"), FakePlugin(kept, "1"), FakePlugin(removing, "1")],
                        [mp("community", URL)])
    local_only = FakePlugin("mine@elsewhere", "1")
    off_here = FakePlugin(kept, "1", enabled=False)
    pending = FakePlugin(PID, "1", enabled=False)
    pending.approval = "pending"  # an agent's install awaiting review here counts as present
    b_service = Service([local_only, off_here, pending], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        plugins_sync.request_uninstall(removing)
        a_row = b.sql("SELECT id FROM network_plugins_desired")
        assert {r["id"] for r in a_row} == {PID, kept}
        b.sql("INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at) "
              "VALUES (?, '1', 1, '{}', CURRENT_TIMESTAMP)", (removing,))
        assert plugins_sync.reconcile_missing() == 0
    assert b_service.calls == []
    assert b_service.enabled[kept] is False


def test_credentialed_or_local_sources_are_not_shared(workers, monkeypatch):
    plugins = [FakePlugin("a@private", "1"), FakePlugin("b@lan", "1"), FakePlugin("c@ssh", "1")]
    a_service = Service(plugins, [mp("private", "https://user:token@github.com/me/private.git"),
                                  mp("lan", "/srv/plugins", source="directory"),
                                  mp("ssh", "git@github.com:me/ssh.git")])
    a, _, _ = network(workers, monkeypatch, a_service, Service())
    assert "marketplace" not in desired_config(a, "a@private")
    assert "marketplace" not in desired_config(a, "b@lan")
    assert desired_config(a, "c@ssh")["marketplace"] == {"id": "ssh", "url": "git@github.com:me/ssh.git"}


def test_a_row_without_a_source_gets_one_once(workers, monkeypatch):
    a_service = Service([FakePlugin(PID, "1")])
    b_service = Service([FakePlugin(PID, "2")], [mp("community", URL)])
    a, b, current = network(workers, monkeypatch, a_service, b_service)
    assert "marketplace" not in desired_config(b)
    with b.active():
        assert plugins_sync.publish() == 1  # fills in the source, enabled state unchanged
        assert plugins_sync.publish() == 0
    converge([a, b])
    assert desired_config(a)["marketplace"]["url"] == URL
    current["name"] = "a"
    a_service.marketplaces["community"] = mp("community", "https://mirror.example/community.git")
    with a.active():
        assert plugins_sync.publish() == 0  # a different URL here does not overwrite it


def test_removing_a_marketplace_uninstalls_its_plugins_everywhere(workers, monkeypatch):
    other = "keep@official"
    never_here = "late@community"
    a_service = Service([FakePlugin(PID, "1"), FakePlugin(other, "1"), FakePlugin(never_here, "1")],
                        [mp("community", URL), mp("official", "https://example.com/official.git")])
    b_service = Service([FakePlugin(PID, "1"), FakePlugin(other, "1")],
                        [mp("community", URL), mp("official", "https://example.com/official.git")])
    a, b, current = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        b_service.fail.add("remove_marketplace")
        assert plugins_sync.remove_marketplace(b_service, "community") is False
        assert plugins_sync.publish() == 0  # a failed removal records nothing
        b_service.fail.clear()
        assert plugins_sync.remove_marketplace(b_service, "community") is True
        assert plugins_sync.publish() == 2
        # The reconcile does not add the marketplace back for the removed rows.
        assert plugins_sync.reconcile_missing() == 0
    assert ("add_marketplace", "community", URL) not in b_service.calls
    converge([a, b])
    current["name"] = "a"
    with a.active():
        assert plugins_sync.apply_desired([PID, never_here]) == 2
    assert set(a_service.installed) == {other}


def test_schedule_runs_one_reconcile_at_a_time_on_the_app_loop(monkeypatch):
    from app.core.global_events import global_events

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    started, release, runs = threading.Event(), threading.Event(), []

    def reconcile():
        runs.append(1)
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(plugins_sync, "reconcile_missing", reconcile)
    monkeypatch.setattr(global_events, "_loop", loop)
    try:
        plugins_sync.schedule_reconcile()
        assert started.wait(5)
        plugins_sync.schedule_reconcile()  # one is running: not queued again
        release.set()
        for _ in range(100):
            if not plugins_sync._reconcile_scheduled:
                break
            threading.Event().wait(0.05)
        assert runs == [1]
        assert plugins_sync._reconcile_scheduled is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def test_schedule_is_a_no_op_without_an_app_loop(monkeypatch):
    from app.core.global_events import global_events

    monkeypatch.setattr(global_events, "_loop", None)
    monkeypatch.setattr(plugins_sync, "reconcile_missing", lambda: pytest.fail("ran without a loop"))
    plugins_sync.schedule_reconcile()
