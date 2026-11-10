"""Connector routes on a worker network follow the network owner (#1382).

Two in-process workers, both seen online, so only the lowest device id may
claim unowned work. The routes, the sync apply, the reconcile and the
``ConnectorManager`` are real; only the platform client is a stand-in that
records the config it was started with.
"""
import asyncio

import pytest
from cryptography.fernet import Fernet

from app.api import connectors as connectors_api
from app.core import connector_packages, encryption
from app.core.connectors import manager as manager_mod
from app.core.connectors.base import ConnectorStatus
from app.core.global_events import global_events
from app.core.models import VALID_CONNECTOR_EVENTS, ConnectorCreate, ConnectorUpdate
from app.db import database
from app.network import engine, ownership, startup, transport
from tests.core.network.harness import _ACTIVE, converge, exchange, join

HOOK = {"mode": "inbound", "inbound_path": "hook-1382"}


class _Client:
    """Stand-in platform client: connects at once, keeps its start config."""

    def __init__(self, connector_id, connector_type, config, **_callbacks):
        self.connector_id = connector_id
        self.config = dict(config)
        self.status = ConnectorStatus.DISCONNECTED

    async def connect(self):
        self.status = ConnectorStatus.CONNECTED
        return True

    async def disconnect(self):
        self.status = ConnectorStatus.DISCONNECTED


class _PerWorker:
    def __init__(self, managers):
        self.managers = managers

    def __getattr__(self, name):
        return getattr(self.managers[_ACTIVE.get().name], name)


@pytest.fixture(autouse=True)
def _connector_runtime(monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(connector_packages, "require_installed", lambda kind: None)
    monkeypatch.setattr(manager_mod, "get_connector_class", lambda kind: _Client)


@pytest.fixture
def net(workers, monkeypatch):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b, name="Worker B")
    converge([a, b])
    # Both workers online: only the lowest device id may claim unowned work.
    monkeypatch.setattr(transport, "online_devices", lambda: {a.device_id: 1.0, b.device_id: 1.0})
    low, high = sorted((a, b), key=lambda w: w.device_id)
    managers = {a.name: manager_mod.ConnectorManager(), b.name: manager_mod.ConnectorManager()}
    monkeypatch.setattr(manager_mod, "connector_manager", _PerWorker(managers))

    scheduled = []

    class _AppLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *_args):
            pass

    monkeypatch.setattr(global_events, "_loop", _AppLoop())
    monkeypatch.setattr(startup.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: scheduled.append((_ACTIVE.get(), coro)))
    unsubscribe = engine.on_applied(startup._on_applied)
    yield a, b, low, high, managers, scheduled
    unsubscribe()
    for _w, coro in scheduled:
        coro.close()


async def _drain(scheduled):
    while scheduled:
        batch = list(scheduled)
        scheduled.clear()
        for worker, coro in batch:
            with worker.active():
                await coro


def _sync(src, dst):
    with src.active():
        engine.scan_all()
    exchange(src, dst)


async def _create(worker, name="Hook"):
    with worker.active():
        return await connectors_api.create_connector_endpoint(
            ConnectorCreate(name=name, type="webhook", config=dict(HOOK), events=[]), "owner")


async def test_a_connector_made_on_a_worker_is_owned_there_and_activates(net):
    """Symptom 1: made on the higher-id worker, it had no owner and activate answered 500."""
    a, b, low, high, managers, scheduled = net
    created = await _create(high)
    assert created["owner_device_id"] == high.device_id
    with high.active():
        activated = await connectors_api.activate_connector(created["id"], "owner")
    assert activated["is_active"] and not activated.get("applies_on")
    assert managers[high.name].get_running_instance(created["id"]) is not None


async def _owned_by(owner, other, net):
    a, b, low, high, managers, scheduled = net
    created = await _create(owner)
    _sync(owner, other)
    await _drain(scheduled)
    return created["id"]


async def test_activate_on_a_non_owner_saves_it_and_names_the_owner(net):
    """Symptom 2: activate on a worker that does not own it answered 500."""
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    with a.active():
        answer = await connectors_api.activate_connector(cid, "owner")
    assert answer["is_active"] and answer["applies_on"] == b.device_id
    assert managers[a.name].get_running_instance(cid) is None
    _sync(a, b)
    await _drain(scheduled)
    assert managers[b.name].get_running_instance(cid) is not None


async def test_a_config_edit_on_a_non_owner_restarts_the_owners_client(net):
    """Symptom 3: the owner kept running the old config."""
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    with b.active():
        await connectors_api.activate_connector(cid, "owner")
    first = managers[b.name].get_running_instance(cid)
    assert first.config["inbound_path"] == "hook-1382"
    _sync(b, a)
    await _drain(scheduled)

    with a.active():
        edited = await connectors_api.update_connector_endpoint(
            cid, ConnectorUpdate(config={"inbound_path": "hook-moved"}), "owner")
    assert edited["config"]["inbound_path"] == "hook-moved"
    assert managers[a.name].get_running_instance(cid) is None
    _sync(a, b)  # the real apply on B schedules the reconcile
    await _drain(scheduled)
    second = managers[b.name].get_running_instance(cid)
    assert second is not first and second.config["inbound_path"] == "hook-moved"
    assert first.status == ConnectorStatus.DISCONNECTED


async def test_an_unrelated_synced_change_does_not_restart_the_owner(net):
    """A rename, or the owner's own status writes, must not bounce a live client."""
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    with b.active():
        await connectors_api.activate_connector(cid, "owner")
    live = managers[b.name].get_running_instance(cid)
    _sync(b, a)
    await _drain(scheduled)

    with b.active():  # the owner's own status writes (local fields)
        database.update_connector_status(cid, "connected", status_message="ok")
        database.update_connector_status(cid, "error", error="blip")
    _sync(b, a)
    await _drain(scheduled)
    with a.active():
        await connectors_api.update_connector_endpoint(cid, ConnectorUpdate(name="Renamed"), "owner")
        await connectors_api.update_connector_endpoint(cid, ConnectorUpdate(events=[VALID_CONNECTOR_EVENTS[0]]), "owner")
    _sync(a, b)
    await _drain(scheduled)
    with b.active():
        assert database.get_connector(cid)["name"] == "Renamed"
    assert managers[b.name].get_running_instance(cid) is live
    assert live.status == ConnectorStatus.CONNECTED


async def test_deactivate_and_restart_on_a_non_owner(net):
    """Symptom 4: both used the install runner, not the network owner."""
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    with b.active():
        await connectors_api.activate_connector(cid, "owner")
    live = managers[b.name].get_running_instance(cid)
    _sync(b, a)
    await _drain(scheduled)

    with a.active():
        restarted = await connectors_api.restart_connector(cid, "owner")
    assert restarted["applies_on"] == b.device_id
    assert managers[a.name].get_running_instance(cid) is None

    with a.active():
        stopped = await connectors_api.deactivate_connector(cid, "owner")
    assert not stopped["is_active"] and stopped["applies_on"] == b.device_id
    _sync(a, b)
    await _drain(scheduled)
    assert managers[b.name].get_running_instance(cid) is None
    assert live.status == ConnectorStatus.DISCONNECTED


async def test_a_connector_with_no_owner_is_claimed_only_by_the_claimant(net):
    """A row made before this fix has no owner: the claimant takes it, another worker reports it unowned."""
    a, b, low, high, managers, scheduled = net
    with low.active():
        database.create_connector("conn-legacy", "webhook", "Old", dict(HOOK), [], is_active=False)
    _sync(low, high)
    await _drain(scheduled)
    with high.active():
        answer = await connectors_api.activate_connector("conn-legacy", "owner")
    assert answer["is_active"] and answer.get("applies_on") is None
    assert managers[high.name].get_running_instance("conn-legacy") is None
    with low.active():
        claimed = await connectors_api.activate_connector("conn-legacy", "owner")
    assert claimed["owner_device_id"] == low.device_id
    assert managers[low.name].get_running_instance("conn-legacy") is not None


_VOLATILE = {"id", "created_at", "updated_at", "last_connected_at", "last_event_at", "api_user_id"}


def _shape(answer):
    return {k: v for k, v in sorted(dict(answer).items()) if k not in _VOLATILE}


async def _single_install_answers(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "instance_id", "install-here", raising=False)
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _s: "install-here"))
    answers = []
    created = await connectors_api.create_connector_endpoint(
        ConnectorCreate(name="Hook", type="webhook", config=dict(HOOK), events=[]), "owner")
    cid = created["id"]
    answers.append(_shape(created))
    answers.append(_shape(await connectors_api.activate_connector(cid, "owner")))
    answers.append(_shape(await connectors_api.update_connector_endpoint(
        cid, ConnectorUpdate(config={"inbound_path": "hook-2"}), "owner")))
    answers.append(_shape(await connectors_api.restart_connector(cid, "owner")))
    answers.append(_shape(await connectors_api.update_connector_endpoint(
        cid, ConnectorUpdate(is_active=False), "owner")))
    answers.append(_shape(await connectors_api.deactivate_connector(cid, "owner")))
    # A connector another install sharing the database runs.
    database.create_connector("conn-elsewhere", "webhook", "Theirs", dict(HOOK), [], is_active=False,
                              runner_id="install-there")
    answers.append(_shape(await connectors_api.activate_connector("conn-elsewhere", "owner")))
    answers.append(_shape(await connectors_api.restart_connector("conn-elsewhere", "owner")))
    answers.append(_shape(await connectors_api.update_connector_endpoint(
        "conn-elsewhere", ConnectorUpdate(config={"inbound_path": "x"}), "owner")))
    answers.append(_shape(await connectors_api.deactivate_connector("conn-elsewhere", "owner")))
    return answers


async def test_single_install_answers_are_unchanged(workers, monkeypatch):
    """No network: the old install-runner rule, response for response."""
    solo = workers("worker-solo")
    manager = manager_mod.ConnectorManager()
    monkeypatch.setattr(manager_mod, "connector_manager", manager)
    with solo.active():
        answers = await _single_install_answers(monkeypatch)
        assert manager.get_running_instance("conn-elsewhere") is None
    assert answers == SINGLE_INSTALL_ANSWERS


# Recorded on the code before #1382 (commit 5ca3092c): the single-install
# responses must not change.
SINGLE_INSTALL_ANSWERS = [
    {'config': {'inbound_path': 'hook-1382', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 0,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'hook-1382', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'connecting',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'hook-2', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'connecting',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'hook-2', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'connecting',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'hook-2', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 0,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'hook-2', 'mode': 'inbound', 'runtime_plugin_id': None},
      'events': [],
      'failure_count': 0,
      'is_active': 0,
      'last_error': None,
      'name': 'Hook',
      'owner_device_id': None,
      'runner_id': 'install-here',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'applies_on': 'install-there',
      'config': {'inbound_path': 'hook-1382', 'mode': 'inbound'},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Theirs',
      'owner_device_id': None,
      'runner_id': 'install-there',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'applies_on': 'install-there',
      'config': {'inbound_path': 'hook-1382', 'mode': 'inbound'},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Theirs',
      'owner_device_id': None,
      'runner_id': 'install-there',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'config': {'inbound_path': 'x', 'mode': 'inbound'},
      'events': [],
      'failure_count': 0,
      'is_active': 1,
      'last_error': None,
      'name': 'Theirs',
      'owner_device_id': None,
      'runner_id': 'install-there',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'},
     {'applies_on': 'install-there',
      'config': {'inbound_path': 'x', 'mode': 'inbound'},
      'events': [],
      'failure_count': 0,
      'is_active': 0,
      'last_error': None,
      'name': 'Theirs',
      'owner_device_id': None,
      'runner_id': 'install-there',
      'status': 'disconnected',
      'status_message': None,
      'type': 'webhook'}]


class _Body:
    async def json(self):
        return {}


async def _execute(worker, cid):
    from fastapi import HTTPException
    with worker.active():
        with pytest.raises(HTTPException) as raised:
            await connectors_api.execute_connector_action(cid, "send_webhook", _Body(), "owner")
    return raised.value


async def test_an_action_on_a_non_owner_names_the_worker_that_runs_it(net):
    """It answered "not running. Activate it first." although the connector ran on another worker."""
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    with b.active():
        await connectors_api.activate_connector(cid, "owner")
    _sync(b, a)
    await _drain(scheduled)
    with b.active():
        b_label = ownership.owner_label(b.device_id)
    error = await _execute(a, cid)
    assert error.status_code == 409
    assert error.detail == f"Connector '{cid}' runs on worker {b_label}; run the action there."
    assert error.headers == {"X-Connector-Applies-On": b.device_id}


async def test_an_action_on_an_inactive_connector_still_says_activate_it(net):
    a, b, low, high, managers, scheduled = net
    cid = await _owned_by(b, a, net)
    error = await _execute(a, cid)
    assert (error.status_code, error.detail, error.headers) == (
        409, f"Connector '{cid}' is not running. Activate it first.", None)


async def test_single_install_action_answer_is_unchanged(workers, monkeypatch):
    """No network: the 409 is the same, byte for byte, even for another install's connector."""
    solo = workers("worker-solo")
    monkeypatch.setattr(manager_mod, "connector_manager", manager_mod.ConnectorManager())
    from app.core.config import settings
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _s: "install-here"))
    with solo.active():
        database.create_connector("conn-elsewhere", "webhook", "Theirs", dict(HOOK), [], is_active=True,
                                  runner_id="install-there")
    error = await _execute(solo, "conn-elsewhere")
    assert (error.status_code, error.detail, error.headers) == (
        409, "Connector 'conn-elsewhere' is not running. Activate it first.", None)
