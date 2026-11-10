"""Signed two-worker boundary protocol; the live app matrix is separate QA."""
import asyncio

import pytest

from app.core import native_providers, provider_agent_supervisor, remote_task, runner_capabilities, scoped_remote_stop, turn_scheduler, turn_manager
from app.core.task_persistence import flush
from app.core.query_session_lifecycle import stop_all_background_tasks
from app.core.task_registry import task_registry
from app.db import remote_tasks, session_events, turns
from app.network import control, engine, memory_records
from tests.core.network.harness import converge
from tests.core.network.test_remote_task_control import network, started_child, all_turns, UNRAID_URL
from tests.core.network.test_remote_task_messaging import body


@pytest.fixture
def kicks(monkeypatch):
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    # This synchronous HTTP harness closes asyncio.run after each dispatch.
    # Drain the actual producer before that artificial loop shutdown can cancel
    # its fire-and-forget writer. A running app keeps that loop alive.
    original = remote_task.dispatch_remote
    async def dispatched(**kwargs):
        result = await original(**kwargs)
        await flush()
        return result
    monkeypatch.setattr(remote_task, "dispatch_remote", dispatched)
    yield
    provider_agent_supervisor.reset()


def publish(a, b, caps=None):
    with a.active():
        actual = runner_capabilities.detect_capabilities()
        assert actual[scoped_remote_stop.CAPABILITY] == 1
        if caps is None:
            assert engine.publish_presence(version="scoped-test")
        else:
            assert engine.publish_presence(capabilities=caps)
    converge([a, b])


def test_bare_presence_publishes_enforced_scope_before_any_heartbeat(workers):
    from app.network.nested_secret_sync import CAPABILITY as SECRET_CAPABILITY
    a = workers("publisher")
    with a.active():
        engine.create_network("Home")
        # Enrollment/hub/API callers omit capabilities. No presence loop or
        # manually supplied detector output may be needed for the first Stop.
        engine.publish_presence(version="initial")
        caps = engine.presence_of(a.device_id)["capabilities"]
        assert caps[scoped_remote_stop.CAPABILITY] == 1
        assert caps[SECRET_CAPABILITY] == 1
        assert caps[native_providers.CAPABILITY] == 1
        assert control.known_action(scoped_remote_stop.ACTION)
        assert scoped_remote_stop.advertised(a.device_id)


def test_unenrolled_node_does_not_probe_or_advertise(workers, monkeypatch):
    a = workers("unenrolled")
    def unavailable():
        pytest.fail("A node without a network must not probe or advertise")
    monkeypatch.setattr(runner_capabilities, "detect_capabilities", unavailable)
    with a.active():
        assert engine.context() is None
        assert engine.publish_presence() is False
        assert engine.all_presence() == {}


@pytest.mark.parametrize("explicit", [{}, {"scoped_task_stop": "1"}, {native_providers.CAPABILITY: 0}])
def test_explicit_old_or_malformed_capabilities_are_not_replaced(workers, monkeypatch, explicit):
    from app.network.nested_secret_sync import CAPABILITY as SECRET_CAPABILITY
    a = workers("explicit-publisher")
    def unavailable():
        pytest.fail("An explicit capability publication must not call the detector")
    monkeypatch.setattr(runner_capabilities, "detect_capabilities", unavailable)
    with a.active():
        engine.create_network("Home")
        engine.publish_presence(capabilities=explicit)
        assert engine.presence_of(a.device_id)["capabilities"] == {
            SECRET_CAPABILITY: 1, native_providers.CAPABILITY: 1,
            memory_records.CAPABILITY: 1, **explicit}
        assert not scoped_remote_stop.advertised(a.device_id)


def test_bare_presence_with_unavailable_detection_is_unknown(workers, monkeypatch):
    from app.network.nested_secret_sync import CAPABILITY as SECRET_CAPABILITY
    a = workers("unknown-publisher")
    def unavailable():
        raise RuntimeError("capability cache is not ready")
    monkeypatch.setattr(runner_capabilities, "detect_capabilities", unavailable)
    with a.active():
        engine.create_network("Home")
        engine.publish_presence()
        assert engine.presence_of(a.device_id)["capabilities"] == {
            SECRET_CAPABILITY: 1, native_providers.CAPABILITY: 1,
            memory_records.CAPABILITY: 1}
        assert not scoped_remote_stop.advertised(a.device_id)


def stop(b):
    with b.active():
        asyncio.run(flush())  # real async producer's durable projection, not a seeded proof
        return asyncio.run(stop_all_background_tasks("caller", user_stop=True))


@pytest.mark.parametrize("caps", [{}, {"scoped_task_stop": 0}, {"scoped_task_stop": "1"}])
def test_old_or_unreadable_publication_never_uses_legacy_stop(workers, monkeypatch, kicks, caps):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        admitted = turns.enqueue_turn("caller", "user", prompt="Start the background work")
        turns.start_turn(admitted["id"])
        task_registry.note_turn_live("caller", admitted["insert_order"])
    child = started_child(a, b)
    publish(a, b, caps)
    results = stop(b)
    assert any(r.get("incomplete") and "ServerWorker" in r.get("detail", "") and "STILL BE RUNNING" in r.get("detail", "")
               and "Update" in r.get("detail", "") and "open" in r.get("detail", "") for r in results), results
    assert not [r for r in router.requests(UNRAID_URL) if r[1].endswith("/task-stop") or r[1].endswith("/task-stop-scoped")]
    with a.active():
        assert not remote_tasks.get_child(child)["stop_requested_at"]
        assert all_turns(child)[0]["state"] == "queued"
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "running"
        events = session_events.get_events("caller")
        assert any("STILL BE RUNNING" in str(e) and "ServerWorker" in str(e) for e in events)


def test_published_upgraded_owner_enforces_stop_without_legacy_fallback(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        admitted = turns.enqueue_turn("caller", "user", prompt="Start the background work")
        turns.start_turn(admitted["id"])
        task_registry.note_turn_live("caller", admitted["insert_order"])
    child = started_child(a, b)
    publish(a, b)
    with b.active():
        scope = task_registry.remote_stop_scope("caller", child)
        assert scoped_remote_stop.valid_scope(scope)
        asyncio.run(flush())
        from app.db import background_tasks
        proof = background_tasks.get_background_task(child, "caller")
        assert proof is not None and proof.get("turn_order") == task_registry.task_turn_order("caller", child), str(proof)
        from app.db.base import parse_jsonb
        assert parse_jsonb(proof.get("remote_stop_scope")) == scope, str(proof)
    results = stop(b)
    assert results and all(r.get("ok") for r in results), str(results)
    calls = router.requests(UNRAID_URL)
    assert any(r[1].endswith("/task-stop-scoped") for r in calls)
    assert not any(r[1].endswith("/task-stop") for r in calls)
    with a.active():
        assert remote_tasks.get_child(child)["stop_requested_at"]
        assert all_turns(child)[0]["state"] == "aborted"
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "killed"


def test_delayed_scope_cannot_cancel_message_produced_generation(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        admitted = turns.enqueue_turn("caller", "user", prompt="Start the background work")
        turns.start_turn(admitted["id"])
        task_registry.note_turn_live("caller", admitted["insert_order"])
    child = started_child(a, b)
    publish(a, b)
    with b.active():
        old_scope = task_registry.remote_stop_scope("caller", child)
        reply = asyncio.run(control.call(a.device_id, remote_task.TASK_MESSAGE_ACTION, body(child)))
        assert reply["stop_scope"]["epoch"] > old_scope["epoch"]
        refused = asyncio.run(control.call(a.device_id, scoped_remote_stop.ACTION,
            {"version": 1, "worker_session_id": child, "scope": old_scope}))
        assert refused["scope_enforced"] and refused["incomplete"] and not refused["ok"]
    with a.active():
        assert not remote_tasks.get_child(child)["stop_requested_at"]
        assert all(t["state"] not in turns.TERMINAL_STATES for t in all_turns(child))


def test_delayed_scope_spares_real_new_human_input(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        admitted = turns.enqueue_turn("caller", "user", prompt="Start the background work")
        turns.start_turn(admitted["id"])
        task_registry.note_turn_live("caller", admitted["insert_order"])
    child = started_child(a, b)
    publish(a, b)
    with b.active():
        scope = task_registry.remote_stop_scope("caller", child)
    with a.active():
        newer_id = turn_manager.submit(child, "user", prompt="a new independent instruction", no_kick=True)
        newer = turns.get_turn(newer_id)
        assert newer["payload"]["person_input"]
    with b.active():
        refused = asyncio.run(control.call(a.device_id, scoped_remote_stop.ACTION,
            {"version": 1, "worker_session_id": child, "scope": scope}))
        assert refused["incomplete"] and not refused["ok"]
    with a.active():
        assert turns.get_turn(newer["id"])["state"] == "queued"
        assert not remote_tasks.get_child(child)["stop_requested_at"]


@pytest.mark.parametrize("failure", ["transport", "unsupported"])
def test_failed_capable_receiver_does_not_fall_back_or_settle_chip(workers, monkeypatch, kicks, failure):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        admitted = turns.enqueue_turn("caller", "user", prompt="Start the background work")
        turns.start_turn(admitted["id"])
        task_registry.note_turn_live("caller", admitted["insert_order"])
    child = started_child(a, b)
    publish(a, b)
    original = control.call
    async def fault(device, action, payload):
        if action == scoped_remote_stop.ACTION:
            if failure == "unsupported":
                return {"ok": True}  # no affirmative enforcement acknowledgement
            raise control.RunnerUnreachable("controlled transport failure")
        return await original(device, action, payload)
    monkeypatch.setattr(control, "call", fault)
    results = stop(b)
    assert any(r.get("incomplete") for r in results)
    assert not any(r[1].endswith("/task-stop") for r in router.requests(UNRAID_URL))
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "running"


@pytest.mark.parametrize("origin", [None, "", "explicit_task_stop_v2", '{"explicit_task_stop":true}'])
def test_preupgrade_user_stop_retry_is_durable_incomplete_not_legacy_rpc(workers, monkeypatch, kicks, origin):
    from app.core.task_registry import USER_STOP_REASON
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        remote_tasks.request_stop(child, USER_STOP_REASON)
        from app.db.base import get_db
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET stop_origin=%s WHERE worker_session_id=%s", (origin, child))
        from tests.core.network.test_remote_task_tracking import restart_parent
        restart_parent()
        assert asyncio.run(remote_task.deliver_stops()) == 0
        assert asyncio.run(remote_task.deliver_stops()) == 0
        row = remote_tasks.get_dispatch(child)
        assert row["stop_origin"] == origin
        assert row["stop_state"] == remote_tasks.STOP_INCOMPLETE
        assert "STILL BE RUNNING" in row["stop_error"] and "ServerWorker" in row["stop_error"]
        assert not remote_tasks.pending_stops()
        notes = [e for e in session_events.get_events("caller")
                 if e["event_type"] == "system.note" and e["payload"].get("subtype") == "stop_incomplete"]
        assert len(notes) == 1 and "STILL BE RUNNING" in str(notes[0]) and "ServerWorker" in str(notes[0])
    assert not any(r[1].endswith("/task-stop") for r in router.requests(UNRAID_URL))
    with a.active():
        assert not remote_tasks.get_child(child)["stop_requested_at"]
