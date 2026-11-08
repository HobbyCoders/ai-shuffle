"""HTTP-ready update commands must not wait for the first periodic health probe."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import application_updates as producer
from docker import application_updates as consumer
from docker import container_runtime as runtime
from tests.test_update_candidate_harness import Clock
from tests.test_application_updates import A, B, FakeDatabase, feed


def test_actual_producer_and_consumer_install_before_first_readiness_probe(tmp_path, monkeypatch):
    clock = Clock()
    for module in (producer, consumer, runtime):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(producer, "allowed", lambda: True)
    monkeypatch.setattr(producer, "control_root", lambda: tmp_path)
    updater = consumer.ContainerUpdates(tmp_path, {"revision": "a" * 40}, b"unused",
                                        lambda: True, lambda _: {})
    updater.state = {"pending": "b" * 40}
    updater.next_check = float("inf")
    monkeypatch.setattr(updater, "tick", updater.service)
    producer.enqueue("install")  # Real fixed-action, bounded-expiry writer.
    assert updater.request_path.exists()
    stop = SimpleNamespace(is_set=lambda: clock.now >= 12, wait=clock.sleep)
    monkeypatch.setattr(runtime, "STOP", stop)
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: True)
    restarted = []
    app = SimpleNamespace(poll=lambda: None)

    def restart(current):
        assert current is app
        restarted.append(clock.now)
        clock.now = 12
        return app

    runtime.watch(app, SimpleNamespace(process=None), None, monitor=monitor,
                  updates=updater, restart=restart)
    assert restarted and restarted[0] < producer.CONTAINER_REQUEST_SECONDS
    assert restarted[0] < runtime.READY_PROBE_INTERVAL
    assert not updater.request_path.exists()


def test_startup_without_command_does_not_begin_automatic_download(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(runtime, "time", clock)
    updater = consumer.ContainerUpdates(tmp_path, {"revision": "a" * 40}, b"unused",
                                        lambda: True, lambda _: {})
    tick = Mock()
    monkeypatch.setattr(updater, "tick", tick)
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: clock.now >= 12, wait=clock.sleep))
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: False)
    runtime.watch(SimpleNamespace(poll=lambda: None), SimpleNamespace(process=None), None,
                  monitor=monitor, updates=updater)
    tick.assert_not_called()


@pytest.mark.parametrize("action", ["check", "download"])
def test_explicit_check_and_download_are_consumed_before_first_probe(tmp_path, monkeypatch, action):
    clock = Clock()
    for module in (producer, consumer, runtime):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(producer, "allowed", lambda: True)
    monkeypatch.setattr(producer, "control_root", lambda: tmp_path)
    updater = consumer.ContainerUpdates(tmp_path, {"revision": "a" * 40}, b"unused",
                                        lambda: True, lambda _: {})
    check = Mock()
    monkeypatch.setattr(updater, "check", check)
    monkeypatch.setattr(updater, "tick", updater.service)
    producer.enqueue(action)
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: clock.now >= 3, wait=clock.sleep))
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: True)
    runtime.watch(SimpleNamespace(poll=lambda: None), SimpleNamespace(process=None), None,
                  monitor=monitor, updates=updater)
    check.assert_called_once_with(download=action == "download")
    assert not updater.request_path.exists()
    assert not monitor.ever_ready
    assert not updater.install_requested.is_set()


@pytest.mark.parametrize("refusal", ["expired", "policy"])
def test_startup_does_not_accept_expired_or_disabled_install(tmp_path, monkeypatch, refusal):
    clock = Clock()
    for module in (producer, consumer, runtime):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(producer, "allowed", lambda: True)
    monkeypatch.setattr(producer, "control_root", lambda: tmp_path)
    updater = consumer.ContainerUpdates(tmp_path, {"revision": "a" * 40}, b"unused",
                                        lambda: refusal != "policy", lambda _: {})
    updater.state = {"pending": "b" * 40}
    check = Mock()
    monkeypatch.setattr(updater, "check", check)
    monkeypatch.setattr(updater, "tick", updater.service)
    producer.enqueue("install")
    if refusal == "expired":
        request = json.loads(updater.request_path.read_text())
        request["expires_at"] = clock.time() - 1
        updater.request_path.write_text(json.dumps(request))
    restart = Mock()
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: clock.now >= 12, wait=clock.sleep))
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: True)
    runtime.watch(SimpleNamespace(poll=lambda: None), SimpleNamespace(process=None), None,
                  monitor=monitor, updates=updater, restart=restart)
    restart.assert_not_called()
    check.assert_not_called()
    assert not updater.request_path.exists()
    assert not updater.install_requested.is_set()
    assert updater.state["pending"] == "b" * 40
    assert updater.image["revision"] == "a" * 40
    assert updater.running is None


def test_repeat_install_before_first_successful_probe_preserves_snapshot(feed, monkeypatch):
    updater, publish, *_ = feed
    publish()
    updater.database = FakeDatabase()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    snapshot = updater.state["snapshot"]
    updater.database.databases["ai_shuffle"] = "migrated by B"
    clock = Clock()
    for module in (producer, consumer, runtime):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(producer, "allowed", lambda: True)
    monkeypatch.setattr(producer, "control_root", lambda: updater.root)
    monkeypatch.setattr(updater, "tick", updater.service)
    # B's HTTP API is ready, but the supervisor has not resolved its first boot.
    producer.enqueue("install")
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: clock.now >= 12, wait=clock.sleep))
    monkeypatch.setattr(runtime, "stop_group", Mock())
    monkeypatch.setattr(runtime, "start_app", Mock(return_value=SimpleNamespace(poll=lambda: None)))
    monkeypatch.setattr(updater, "lifecycle", lambda action: {"active_sessions": 0, "stopped": True})
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: False)
    current = SimpleNamespace(poll=lambda: None)
    runtime.watch(current, SimpleNamespace(process=None), None, monitor=monitor,
                  updates=updater, restart=lambda app: runtime.restart_into_update(app, updater, {}))
    assert updater.state["booting"] == B
    assert updater.state["snapshot"] == snapshot
    assert updater.database.databases[snapshot] == "before"
    assert [call for call in updater.database.calls if call[0] == "snapshot"] == [("snapshot", snapshot)]
    runtime.stop_group.assert_not_called()
    runtime.start_app.assert_not_called()
    assert not updater.request_path.exists()
    assert not updater.install_requested.is_set()
    updater.failed()
    assert updater.database.databases["ai_shuffle"] == "before"
    assert updater.running is None


@pytest.mark.parametrize("entry", ["launch", "activating"])
def test_direct_repeat_activation_cannot_replace_first_boot_snapshot(feed, entry):
    updater, publish, *_ = feed
    publish()
    updater.database = FakeDatabase()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    snapshot = updater.state["snapshot"]
    updater.database.databases["ai_shuffle"] = "migrated by B"
    with pytest.raises(consumer.UpdateError, match="first start"):
        if entry == "launch":
            updater.launch({}, activate=True)
        else:
            updater.activating()
    assert updater.database.databases[snapshot] == "before"
    assert [call for call in updater.database.calls if call[0] == "snapshot"] == [("snapshot", snapshot)]
    updater.healthy()
    updater.require_resolved_first_boot()
    assert updater.state["active"] == B
    assert "booting" not in updater.state
    assert "snapshot" not in updater.state


def test_command_only_pass_does_not_start_due_automatic_check(tmp_path, monkeypatch):
    updater = consumer.ContainerUpdates(tmp_path, {"revision": "a" * 40}, b"unused",
                                        lambda: True, lambda _: {})
    check = Mock()
    monkeypatch.setattr(updater, "check", check)
    updater.next_check = 0
    updater.service(requested_only=True)
    check.assert_not_called()
    assert updater.next_check == 0
    updater.service()  # Existing periodic behavior is unchanged after readiness.
    check.assert_called_once_with(download=False)
