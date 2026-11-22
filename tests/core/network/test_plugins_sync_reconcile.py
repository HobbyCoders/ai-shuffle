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
    monkeypatch.setattr(plugins_sync, "_received_resumed", True)
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


@pytest.fixture
def app_loop(monkeypatch):
    from app.core.global_events import global_events

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(global_events, "_loop", loop)
    monkeypatch.setattr(plugins_sync, "_reconcile_running", False)
    monkeypatch.setattr(plugins_sync, "_reconcile_again", False)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def wait_idle():
    for _ in range(100):
        if not plugins_sync._reconcile_running:
            return
        threading.Event().wait(0.05)
    pytest.fail("reconcile still running")


def test_schedule_runs_one_pass_at_a_time_and_one_more_if_asked_meanwhile(app_loop, monkeypatch):
    started, release, runs = threading.Event(), threading.Event(), []

    def reconcile():
        runs.append(1)
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(plugins_sync, "reconcile_missing", reconcile)
    assert plugins_sync.schedule_reconcile() is True
    assert started.wait(5)
    # A row that arrives now was not in the running pass's list: one more pass.
    assert plugins_sync.schedule_reconcile() is True
    assert plugins_sync.schedule_reconcile() is True
    release.set()
    wait_idle()
    assert runs == [1, 1]
    assert plugins_sync._reconcile_again is False


def test_schedule_is_a_no_op_without_an_app_loop(monkeypatch):
    from app.core.global_events import global_events

    monkeypatch.setattr(global_events, "_loop", None)
    monkeypatch.setattr(plugins_sync, "reconcile_missing", lambda: pytest.fail("ran without a loop"))
    assert plugins_sync.schedule_reconcile() is False


def test_a_received_install_runs_on_the_plugin_thread_not_the_sync_thread(workers, monkeypatch, app_loop):
    a_service = Service([FakePlugin(PID, "1")], [mp("community", URL)])
    b_service = Service()
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    sync_thread = threading.get_ident()
    install_threads = []
    real_install = b_service.install_plugin

    def install(plugin_id, scope="user", enable=True):
        install_threads.append(threading.get_ident())
        return real_install(plugin_id, scope, enable)

    b_service.install_plugin = install
    plugins_sync._retry[PID] = (3, float("inf"))  # backing off from earlier failures
    with b.active():
        plugins_sync.apply_desired([PID])
        wait_for(lambda: PID in b_service.installed)
        wait_idle()
    assert PID in b_service.installed  # a received change is tried at once
    assert install_threads and sync_thread not in install_threads


def wait_for(condition):
    for _ in range(100):
        if condition():
            return
        threading.Event().wait(0.05)
    pytest.fail("condition not met")


def test_received_toggles_and_uninstalls_run_on_the_plugin_thread(workers, monkeypatch, app_loop):
    """They can wait on a plugin lock a clone holds; the sync thread must not."""
    kept, gone = "kept@community", "gone@community"
    a_service = Service([FakePlugin(kept, "1"), FakePlugin(gone, "1")], [mp("community", URL)])
    b_service = Service([FakePlugin(kept, "1"), FakePlugin(gone, "1")], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    sync_thread = threading.get_ident()
    threads = []
    for name in ("disable_plugin", "uninstall_plugin"):
        real = getattr(b_service, name)
        setattr(b_service, name, lambda pid, real=real: threads.append(threading.get_ident()) or real(pid))
    with b.active():
        b.sql("UPDATE network_plugins_desired SET enabled = ? WHERE id = ?", (False, kept))
        b.sql("DELETE FROM network_plugins_desired WHERE id = ?", (gone,))
        assert plugins_sync.apply_desired([kept, gone]) == 0  # queued, not applied here
        wait_for(lambda: len(threads) == 2)
    assert b_service.enabled[kept] is False and gone not in b_service.installed
    assert sync_thread not in threads


@pytest.fixture
def busy_plugin_thread(monkeypatch):
    """The plugin thread is busy (a long clone): queued jobs wait here until run."""
    queued = []
    monkeypatch.setattr(plugins_sync, "_on_plugin_thread", lambda fn, *args: queued.append((fn, args)) or True)
    return queued


def run_queued(queued):
    while queued:
        fn, args = queued.pop(0)
        fn(*args)


def desired_enabled(worker, plugin_id):
    return bool(worker.sql("SELECT enabled FROM network_plugins_desired WHERE id = ?", (plugin_id,))[0]["enabled"])


def test_a_scan_while_a_received_disable_waits_does_not_undo_it(workers, monkeypatch, busy_plugin_thread):
    kept = "kept@community"
    a_service = Service([FakePlugin(kept, "1")], [mp("community", URL)])
    b_service = Service([FakePlugin(kept, "1")], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        b.sql("UPDATE network_plugins_desired SET enabled = ? WHERE id = ?", (False, kept))  # from A
        plugins_sync.apply_desired([kept])
        assert busy_plugin_thread  # queued behind the clone
        plugins_sync.refresh_desired_from_installed()  # still enabled here: not an edit
        assert desired_enabled(b, kept) is False
        run_queued(busy_plugin_thread)
        assert b_service.enabled[kept] is False
        assert plugins_sync.refresh_desired_from_installed() == 0
        assert not b.sql("SELECT key FROM system_settings WHERE key LIKE 'network_plugin_received:%'")


def test_a_received_removal_cut_off_by_a_restart_is_applied_after_it(workers, monkeypatch, busy_plugin_thread):
    gone = "gone@community"
    a_service = Service([FakePlugin(gone, "1")], [mp("community", URL)])
    b_service = Service([FakePlugin(gone, "1")], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        b.sql("DELETE FROM network_plugins_desired WHERE id = ?", (gone,))  # A's tombstone
        plugins_sync.apply_desired([gone])
        busy_plugin_thread.clear()  # the process stops before the job runs
        monkeypatch.setattr(plugins_sync, "_on_plugin_thread", lambda fn, *args: False)
        monkeypatch.setattr(plugins_sync, "_received_resumed", False)  # a new process
        plugins_sync.publish()
    assert gone not in b_service.installed


def test_the_owners_toggle_here_outranks_a_queued_received_one(workers, monkeypatch, busy_plugin_thread):
    kept = "kept@community"
    a_service = Service([FakePlugin(kept, "1")], [mp("community", URL)])
    b_service = Service([FakePlugin(kept, "1")], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        b.sql("UPDATE network_plugins_desired SET enabled = ? WHERE id = ?", (False, kept))  # from A
        plugins_sync.apply_desired([kept])
        plugins_sync.note_local_change(kept)  # the owner turns it on here meanwhile
        b_service.enable_plugin(kept)
        run_queued(busy_plugin_thread)
        assert b_service.enabled[kept] is True  # the queued change did not undo it
        assert plugins_sync.refresh_desired_from_installed() == 1  # and theirs is published
        assert desired_enabled(b, kept) is True


def test_a_disable_that_lands_during_the_install_is_kept(workers, monkeypatch):
    a_service = Service([FakePlugin(PID, "1")], [mp("community", URL)])
    b_service = Service([], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    real_install = b_service.install_plugin

    def install(plugin_id, scope="user", enable=True):
        plugin = real_install(plugin_id, scope, enable)
        # Turned off on A meanwhile; that change found nothing installed here.
        b.sql("UPDATE network_plugins_desired SET enabled = ? WHERE id = ?", (False, plugin_id))
        return plugin

    b_service.install_plugin = install
    with b.active():
        assert plugins_sync.reconcile_missing() == 1
        assert b_service.enabled[PID] is False
        plugins_sync.publish()  # nothing stale to send back
        assert not b.sql("SELECT enabled FROM network_plugins_desired WHERE id = ?", (PID,))[0]["enabled"]


def test_the_shipped_default_source_is_never_added_back(workers, monkeypatch):
    """Every worker registers it once; a worker without it removed it."""
    from app.core.plugin_marketplace import DEFAULT_SOURCE_ID, DEFAULT_SOURCE_URL

    pid = "review@" + DEFAULT_SOURCE_ID
    a_service = Service([FakePlugin(pid, "1")], [mp(DEFAULT_SOURCE_ID, DEFAULT_SOURCE_URL)])
    b_service = Service()
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        assert plugins_sync.reconcile_missing() == 0
    assert ("add_marketplace", DEFAULT_SOURCE_ID, DEFAULT_SOURCE_URL) not in b_service.calls
    assert pid not in b_service.installed


def test_a_removal_that_lands_during_the_install_uninstalls_it(workers, monkeypatch):
    a_service = Service([FakePlugin(PID, "1")], [mp("community", URL)])
    b_service = Service([], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    real_install = b_service.install_plugin

    def install(plugin_id, scope="user", enable=True):
        plugin = real_install(plugin_id, scope, enable)
        # The tombstone is applied here meanwhile; it found nothing to uninstall.
        b.sql("DELETE FROM network_plugins_desired WHERE id = ?", (plugin_id,))
        return plugin

    b_service.install_plugin = install
    with b.active():
        assert plugins_sync.reconcile_missing() == 0
    assert PID not in b_service.installed
    assert b_service.calls[-1] == ("uninstall", PID)


def test_a_row_removed_before_its_turn_is_not_installed(workers, monkeypatch):
    first, second = "a-first@community", "b-second@community"
    a_service = Service([FakePlugin(first, "1"), FakePlugin(second, "1")], [mp("community", URL)])
    b_service = Service([], [mp("community", URL)])
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    real_install = b_service.install_plugin

    def install(plugin_id, scope="user", enable=True):
        if plugin_id == first:  # a slow clone; the second plugin's removal lands
            b.sql("DELETE FROM network_plugins_desired WHERE id = ?", (second,))
        return real_install(plugin_id, scope, enable)

    b_service.install_plugin = install
    with b.active():
        assert plugins_sync.reconcile_missing() == 1
    assert set(b_service.installed) == {first}


def test_a_registered_source_that_was_never_downloaded_is_fetched_first(workers, monkeypatch, tmp_path):
    a_service = Service([FakePlugin(PID, "1")], [mp("community", URL)])
    registered = SimpleNamespace(id="community", source="git", url=URL, install_location=str(tmp_path / "absent"))
    b_service = Service([], [registered])
    b_service.sync_marketplace = lambda mp_id: b_service.calls.append(("sync_marketplace", mp_id)) or True
    _, b, _ = network(workers, monkeypatch, a_service, b_service)
    with b.active():
        assert plugins_sync.reconcile_missing() == 1
    assert b_service.calls == [("sync_marketplace", "community"), ("install", PID, True)]


def test_a_snapshot_tombstone_reaches_the_plugin_callback_by_id(workers, monkeypatch, tmp_path):
    """A worker back after the uninstall op was pruned gets the tombstone only in a snapshot."""
    from app.network import peer_snapshot

    a_service = Service([FakePlugin(PID, "1")], [mp("community", URL)])
    a, b, current = network(workers, monkeypatch, a_service, Service())
    current["name"] = "a"
    with a.active():
        plugins_sync.request_uninstall(PID)
        assert plugins_sync.publish() == 1
        manifest = peer_snapshot.capture(b.device_id)
        for index in range(len(manifest["pages"])):
            (tmp_path / f"{index}.json").write_bytes(peer_snapshot.page(b.device_id, manifest["transfer"], index))
    notified = []
    real_notify = engine._notify
    monkeypatch.setattr(engine, "_notify", lambda touched: notified.extend(touched) or real_notify(touched))
    with b.active():
        peer_snapshot.apply(manifest, tmp_path, a.device_id)
    assert any(c == "plugins_desired" and r == PID for c, r, _op in notified)
