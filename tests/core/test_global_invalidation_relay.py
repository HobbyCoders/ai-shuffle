"""Two process-local hubs sharing only a fake PostgreSQL notification channel."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core.global_events import GlobalEvents


class Gateway:
    def __init__(self):
        self.listeners = []
        self.messages = []

    def add_message_listener(self, kind, listener):
        self.listeners.append(listener)

    def remove_message_listener(self, kind, listener):
        self.listeners.remove(listener)

    def notify(self, message):
        self.messages.append(message)
        for listener in self.listeners.copy():
            listener(message)


@pytest.mark.asyncio
async def test_cross_worker_identity_delivery_deduplication_and_shutdown(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr('app.core.event_gateway.get_gateway', lambda: gateway)
    monkeypatch.setattr(GlobalEvents, '_notify', staticmethod(gateway.notify))
    first, second = GlobalEvents(), GlobalEvents()
    first.start()
    second.start()
    alice, bob = AsyncMock(), AsyncMock()
    second.register(alice, 'account', 'hub#alice')
    second.register(bob, 'account', 'hub#bob')
    try:
        first.preference_changed('account', 'hub#alice', 'composer')
        # Enqueue callbacks precede the worker; join is the completion boundary.
        await asyncio.sleep(0)
        await first._queue.join()
        for _ in range(12):
            await asyncio.sleep(0)
        assert json.loads(alice.send_text.call_args.args[0]) == {'type': 'preferences_changed', 'keys': ['composer']}
        assert bob.send_text.call_count == 1  # sync_ready only
        assert len(gateway.messages) == 1  # peer delivery did not publish again
        gateway.notify(gateway.messages[0])
        await asyncio.sleep(0)
        assert alice.send_text.call_count == 2  # duplicate was suppressed
        first.lifecycle('alice', {'type': 'user_session_updated', 'session_id': 'secret'}, user_type='api_user')
        await asyncio.sleep(0)
        await first._queue.join()
        assert 'secret' not in json.dumps(gateway.messages[-1])
    finally:
        await first.shutdown()
        await second.shutdown()
    assert gateway.listeners == []


def test_relay_rejects_payloads_other_than_bounded_descriptors():
    assert GlobalEvents._invalidation({'type': 'resources_changed', 'resources': ['projects'], 'secret': 'x'}) == {
        'type': 'resources_changed', 'resources': ['projects']}
    assert GlobalEvents._invalidation({'type': 'preferences_changed', 'keys': ['x'] * 65}) is None
    assert GlobalEvents._invalidation({'type': 'token', 'value': 'secret'}) is None


@pytest.mark.asyncio
async def test_listener_gap_recovers_without_socket_reconnect_or_peer_message(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr('app.core.event_gateway.get_gateway', lambda: gateway)
    monkeypatch.setattr('app.core.global_events.RECONCILE_SECONDS', 0.01)
    monkeypatch.setattr(GlobalEvents, '_notify', staticmethod(lambda _: None))  # lost PG notify
    first, second = GlobalEvents(), GlobalEvents()
    first.start()
    second.start()
    socket = AsyncMock()
    received = asyncio.Event()
    async def capture(text):
        if json.loads(text)['type'] == 'sync_reconcile':
            received.set()
    socket.send_text.side_effect = capture
    socket_id = second.register(socket, 'account', 'hub#alice')
    try:
        first.preference_changed('account', 'hub#alice', 'selected-agent')
        await asyncio.wait_for(received.wait(), 1)
        assert socket_id in second.connections
        socket.close.assert_not_called()
        assert gateway.messages == []
    finally:
        await first.shutdown()
        await second.shutdown()
    assert first._reconciler is second._reconciler is None


@pytest.mark.asyncio
async def test_local_thread_ingress_is_bounded_and_overflow_requests_snapshot():
    hub = GlobalEvents()
    socket = AsyncMock()
    socket_id = hub.register(socket, 'account', 'alice')
    try:
        for _ in range(4):
            await asyncio.sleep(0)
        # Do not yield: this is the same backlog a busy loop sees when a DB
        # thread publishes faster than loop callbacks can be processed.
        for index in range(10000):
            hub.preference_changed('account', 'alice', str(index))
        assert len(hub._deliveries) == 1
        assert len(hub._deliveries[socket_id]) <= 256
        assert '{"type":"sync_reconcile"}' in hub._deliveries[socket_id]
        for _ in range(12):
            await asyncio.sleep(0)
        assert not hub._deliveries
    finally:
        await hub.shutdown()


@pytest.mark.asyncio
async def test_relay_cannot_broadcast_an_account_preference_or_leak_legacy_payload():
    hub = GlobalEvents()
    alice, bob = AsyncMock(), AsyncMock()
    hub.register(alice, 'account', 'alice')
    hub.register(bob, 'account', 'bob')
    try:
        hub._receive({'id': 'a' * 32, 'source': 'b' * 32, 'user_type': 'account',
                      'user_id': None, 'message': {'type': 'preferences_changed', 'keys': ['private-key']}})
        hub.lifecycle('admin', {'type': 'user_session_updated', 'session_id': 'private', 'title': 'secret'}, user_type='admin')
        for _ in range(12):
            await asyncio.sleep(0)
        for socket in (alice, bob):
            assert [json.loads(c.args[0]) for c in socket.send_text.call_args_list] == [
                {'type': 'sync_ready'}, {'type': 'resources_changed', 'resources': ['sessions']}]
    finally:
        await hub.shutdown()


@pytest.mark.asyncio
async def test_oversized_descriptor_uses_bounded_reconciliation_marker(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr('app.core.event_gateway.get_gateway', lambda: gateway)
    monkeypatch.setattr(GlobalEvents, '_notify', staticmethod(gateway.notify))
    hub = GlobalEvents()
    hub.start()
    try:
        hub.publish({'type': 'preferences_changed', 'keys': ['漢' * 256] * 64},
                    user_type='account', user_id='alice')
        await asyncio.sleep(0)
        await hub._queue.join()
        assert len(gateway.messages) == 1
        assert gateway.messages[0]['message'] == {'type': 'sync_reconcile'}
        assert gateway.messages[0]['user_id'] == 'alice'
        assert len(json.dumps(gateway.messages[0]).encode('utf-8')) <= 7000
    finally:
        await hub.shutdown()
