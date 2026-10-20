# ruff: noqa: F811
"""Bounded cross-worker snapshots and worker-local Task inheritance."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import chat_access
from app.core.provider_agent import worker_session_fields
from app.db import database
from app.network import chat_access_routing as routing
from tests.core.network.test_chat_access_routing import (  # noqa: F401
    A_URL,
    BASE,
    SESSION,
    Network,
    _connection,
    _reach,
    _request,
    pair,
    ssh_target,
)
from tests.core.test_chat_access import _real_session_owner

pytestmark = pytest.mark.asyncio


async def test_unchanged_pending_card_backs_off_across_real_two_peer_control(pair, monkeypatch):
    network = Network(monkeypatch, {A_URL: pair.a})
    _reach(pair.b, pair.a, A_URL)
    with pair.a.active():
        await pair.broker.request(SESSION, "service", "Read inventory", service_provider="cloudflare")
    clock = [0.0]
    monkeypatch.setattr(routing, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    with pair.b.active():
        async with network.ui() as ui:
            # Simulate a panel whose safety interval is reset by a session
            # invalidation every 2 seconds, for half an hour.
            for second in range(0, 1800, 2):
                clock[0] = float(second)
                response = await ui.get(BASE)
                assert response.status_code == 200
                assert len(response.json()["requests"]) == 1
            assert len(network.control_paths()) <= 65  # used to be 900
            assert routing._snapshots[(pair.a.device_id, SESSION)].delay == 30
            # Owner mutation is never served from cache, even within 2 seconds.
            request_id = next(iter(pair.broker.requests))
            assert (await ui.delete(f"{BASE}/requests/{request_id}")).status_code == 200
            assert (await ui.get(BASE)).json()["requests"] == []


async def test_snapshot_singleflight_push_invalidation_and_inflight_fence(monkeypatch):
    routing._snapshots.clear()
    started, release = asyncio.Event(), asyncio.Event()
    value = {"requests": [{"id": "before"}]}
    async def call(*args, **kwargs):
        started.set()
        await release.wait()
        return value
    mocked = AsyncMock(side_effect=call)
    monkeypatch.setattr(routing.control, "call", mocked)
    a = asyncio.create_task(routing.forward("owner", "chat", "snapshot"))
    await started.wait()
    b = asyncio.create_task(routing.forward("owner", "chat", "snapshot"))
    await asyncio.sleep(0)
    assert mocked.await_count == 1
    routing.invalidate_snapshot("owner", "chat")
    release.set()
    assert await a == await b == value
    value = {"requests": [{"id": "after"}]}
    assert await routing.forward("owner", "chat", "snapshot") == value
    assert mocked.await_count == 2
    routing._snapshots.clear()


async def test_snapshot_cache_is_bounded_and_does_not_cache_denials(monkeypatch):
    routing._snapshots.clear()
    mocked = AsyncMock(return_value={"requests": []})
    monkeypatch.setattr(routing.control, "call", mocked)
    monkeypatch.setattr(routing, "SNAPSHOT_LIMIT", 3)
    for i in range(8):
        await routing.forward("owner", str(i), "snapshot")
    assert len(routing._snapshots) == 3
    mocked.side_effect = PermissionError("denied")
    for _ in range(2):
        with pytest.raises(PermissionError, match="denied"):
            await routing.forward("owner", "denied", "snapshot")
    assert mocked.await_count == 10
    routing._snapshots.clear()


async def test_snapshot_refetches_at_advertised_expiry(monkeypatch):
    routing._snapshots.clear()
    clock = [0.0]
    monkeypatch.setattr(routing, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]))
    mocked = AsyncMock(return_value={"grants": [{"id": "g", "expires_at": 1005}]})
    monkeypatch.setattr(routing.control, "call", mocked)
    await routing.forward("owner", "chat", "snapshot")
    clock[0] = 2
    await routing.forward("owner", "chat", "snapshot")
    assert mocked.await_count == 2
    clock[0] = 4
    await routing.forward("owner", "chat", "snapshot")
    assert mocked.await_count == 2  # unchanged result backed off, but only until expiry
    mocked.return_value = {"grants": []}
    clock[0] = 5
    assert await routing.forward("owner", "chat", "snapshot") == {"grants": []}
    assert mocked.await_count == 3
    routing._snapshots.clear()


def _child(child, parent):
    database.create_session(**worker_session_fields(
        worker_session_id=child, profile_id="default", title="Task", parent_session_id=parent,
        provider_id=None, model_id="test", effort=None))


async def test_viewer_approves_on_owner_local_descendants_inherit_remote_child_refuses(pair, ssh_target, monkeypatch):
    spec, commands, _ = ssh_target
    network = Network(monkeypatch, {A_URL: pair.a})
    _reach(pair.b, pair.a, A_URL)
    task, request_id = await _request(pair)
    with pair.b.active():
        async with network.ui() as ui:
            approved = await ui.post(f"{BASE}/requests/{request_id}", json={"connection": _connection(spec)})
            assert approved.status_code == 200
    handle = await task
    with pair.a.active():
        _child("local-child", SESSION)
        _child("grandchild", SESSION)  # actual dispatch stores routing root
        pair.broker.register_delegation("grandchild", "local-child", routing_parent="mirrored-chat")
        assert (await pair.broker.resources("local-child"))["grants"] == [handle]
        assert (await pair.broker.resources("grandchild"))["grants"] == [handle]
    # The mirror is not an authorization edge. Even with the same account,
    # B cannot claim the credentials held in A's broker.
    with pair.b.active():
        _child("remote-child", SESSION)
        monkeypatch.setattr(chat_access, "session_owner", _real_session_owner)
        remote_broker = chat_access.Broker()
        assert (await remote_broker.resources("remote-child"))["grants"] == []
        with pytest.raises(chat_access.AccessDenied):
            await pair.broker.get("remote-child", handle["id"])
    with pair.b.active():
        async with network.ui() as ui:
            assert (await ui.delete(f"{BASE}/grants/{handle['id']}")).status_code == 200
    with pair.a.active(), pytest.raises(chat_access.AccessDenied):
        await pair.broker.get("grandchild", handle["id"])
    assert not commands
