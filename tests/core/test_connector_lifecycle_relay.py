"""Owner relay contracts with a fake NOTIFY transport; never connects to PostgreSQL."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import event_gateway
from app.core.connectors import lifecycle_relay
from app.core.connectors.manager import ConnectorManager


@pytest.fixture
def gateway(monkeypatch):
    gateway = event_gateway.EventGateway()
    monkeypatch.setattr(gateway, "_ensure_listener", lambda: None)
    monkeypatch.setattr(event_gateway, "get_gateway", lambda: gateway)
    return gateway


def envelope(event_id="turn:session.complete", runner_id="owner", connector_id="c1"):
    return {"type": lifecycle_relay.MESSAGE_TYPE, "runner_id": runner_id,
            "connector_id": connector_id, "event_type": "session.complete",
            "data": {"session_id": "s1", "event_id": event_id}}


async def test_owner_delivery_dedup_and_no_session_tick(gateway):
    owner_send, other_send = AsyncMock(), AsyncMock()
    owner = lifecycle_relay.LifecycleRelay("owner", owner_send)
    other = lifecycle_relay.LifecycleRelay("other", other_send)
    ticks = Mock()
    gateway.add_global_listener(ticks)
    queue = gateway.subscribe("s1")
    owner.start()
    other.start()
    try:
        for _ in range(2):
            gateway._handle_payload(json.dumps(envelope()))
        await asyncio.sleep(0)
        await owner._queue.join()
        owner_send.assert_awaited_once_with("c1", "session.complete", envelope()["data"])
        other_send.assert_not_awaited()
        ticks.assert_not_called()
        assert queue.empty()
        # Same lifecycle event can legitimately reach another subscribed connector.
        gateway._handle_payload(json.dumps(envelope(connector_id="c2")))
        await asyncio.sleep(0)
        await owner._queue.join()
        assert owner_send.await_count == 2
    finally:
        await owner.stop()
        await other.stop()
    assert lifecycle_relay.MESSAGE_TYPE not in gateway._message_listeners


def test_publish_bounds_and_redacts_payload(monkeypatch):
    cursor = Mock()

    @contextmanager
    def fake_db():
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(lifecycle_relay, "get_db", fake_db)
    data = {**envelope()["data"], "title": "x" * 2000,
            "error_message": "e" * 2000, "tool_input": {"secret": "private"},
            "api_key": "secret", "output_tokens": 7}
    lifecycle_relay.publish("c1", "owner", "session.complete", data)
    sql, (channel, raw) = cursor.execute.call_args.args
    assert sql == "SELECT pg_notify(%s, %s)"
    assert channel == event_gateway.LISTEN_CHANNEL
    message = json.loads(raw)
    assert message["runner_id"] == "owner"
    assert len(raw.encode()) <= lifecycle_relay.MAX_PAYLOAD_BYTES
    assert len(message["data"]["title"]) == 512
    assert message["data"]["output_tokens"] == 7
    assert "private" not in raw and "secret" not in raw
    cursor.reset_mock()
    with pytest.raises(ValueError, match="bounded payload"):
        lifecycle_relay.publish("c1", "owner", "session.complete", {
            **data, "event_id": "x" * 8000,
        })
    cursor.execute.assert_not_called()
    with pytest.raises(ValueError, match="stable event/session IDs"):
        lifecycle_relay.publish("c1", "owner", "session.complete", {})


def test_queue_and_dedup_memory_bounded(monkeypatch):
    relay = lifecycle_relay.LifecycleRelay("owner", AsyncMock())
    relay._queue = asyncio.Queue(maxsize=2)
    monkeypatch.setattr(lifecycle_relay, "MAX_SEEN", 2)
    for n in range(4):
        relay._enqueue(envelope(event_id=str(n)))
    assert relay._queue.qsize() == 2
    assert len(relay._seen) == 2
    assert ("c1", "3") not in relay._seen
    relay._queue.get_nowait()
    relay._enqueue(envelope(event_id="3"))
    assert len(relay._seen) == 2
    assert ("c1", "3") in relay._seen
    for bad in ({}, {**envelope(), "event_type": []},
                {**envelope(), "data": {"event_id": "x", "session_id": ""}}):
        relay._enqueue(bad)
    assert relay._queue.qsize() == 2


async def test_dispatch_relays_remote_but_globals_only_once(monkeypatch):
    from app.core import automation_manager
    from app.core.config import settings
    from app.db import database

    manager = ConnectorManager()
    rows = [{"id": "remote", "runner_id": "another-owner"},
            {"id": "local", "runner_id": settings.effective_instance_id}]
    monkeypatch.setattr(database, "get_connectors_for_event", lambda event: rows)
    publish = Mock()
    local = AsyncMock()
    automation = AsyncMock()
    monkeypatch.setattr(lifecycle_relay, "publish", publish)
    monkeypatch.setattr(manager, "_deliver_connector_event", local)
    monkeypatch.setattr(automation_manager.automation_manager, "handle_connector_event", automation)
    data = envelope()["data"]
    await manager.dispatch_event("session.complete", data)
    publish.assert_called_once_with("remote", "another-owner", "session.complete", data)
    local.assert_awaited_once_with(rows[1], "session.complete", data)
    automation.assert_awaited_once_with("session.complete", data)


async def test_relay_receiver_rechecks_owner_active_subscription_and_chat(monkeypatch):
    from app.core import automation_manager
    from app.core.config import settings
    from app.db import database

    manager = ConnectorManager()
    row = {"id": "c1", "runner_id": settings.effective_instance_id,
           "is_active": True, "events": ["session.complete"]}
    monkeypatch.setattr(database, "get_connector", lambda cid: row)
    lookup = Mock(return_value=[{"connector_id": "c1", "platform_chat_id": "right"},
                               {"connector_id": "c2", "platform_chat_id": "wrong"}])
    monkeypatch.setattr(database, "get_connector_sessions_for_ai_session", lookup)
    monkeypatch.setattr(database, "create_connector_event_log", Mock())
    monkeypatch.setattr(database, "update_connector_event_dispatched", Mock())
    send = AsyncMock(return_value=True)
    manager._connectors["c1"] = SimpleNamespace(send_notification=send)
    automation = AsyncMock()
    monkeypatch.setattr(automation_manager.automation_manager, "handle_connector_event", automation)
    data = envelope()["data"]
    await manager._deliver_relayed_event("c1", "session.complete", data)
    send.assert_awaited_once_with("right", "session.complete", data)
    lookup.assert_called_once_with("s1")
    for key, value in (("runner_id", "other"), ("is_active", False), ("events", [])):
        original = row[key]
        row[key] = value
        await manager._deliver_relayed_event("c1", "session.complete", data)
        row[key] = original
    assert send.await_count == 1
    automation.assert_not_awaited()


async def test_manager_shutdown_removes_relay_listener(gateway):
    manager = ConnectorManager()
    manager.start_event_relay()
    assert lifecycle_relay.MESSAGE_TYPE in gateway._message_listeners
    await manager.shutdown_all()
    assert manager._event_relay is None
    assert lifecycle_relay.MESSAGE_TYPE not in gateway._message_listeners


def test_gateway_thread_ingress_is_bounded():
    relay = lifecycle_relay.LifecycleRelay("owner", AsyncMock())
    loop = Mock()
    relay._loop = loop
    for _ in range(300):
        relay._on_message(envelope())
    assert loop.call_soon_threadsafe.call_count == 256
    # A processed callback releases ingress capacity, even during shutdown.
    relay._accept_message(envelope())
    relay._on_message(envelope())
    assert loop.call_soon_threadsafe.call_count == 257


async def test_uncertain_send_not_retried_but_worker_continues(gateway):
    deliver = AsyncMock(side_effect=[RuntimeError("uncertain send"), None])
    relay = lifecycle_relay.LifecycleRelay("owner", deliver)
    relay.start()
    try:
        relay._enqueue(envelope("first"))
        await relay._queue.join()
        relay._enqueue(envelope("first"))
        relay._enqueue(envelope("next"))
        await relay._queue.join()
        assert deliver.await_count == 2
    finally:
        await relay.stop()
