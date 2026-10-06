"""Peer routes that act with the owner's authority admit only another worker.

A consumer member (a phone, a paired browser) can hold a valid device token:
``tokens.issue_token`` does not check the member kind. The routes below run
this worker's browser, serve any file in its workspace or drive its chats, so
they use ``tokens.require_worker_device``: only another ACTIVE ``worker``
member of this network is admitted. Every test here uses real device tokens.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import browser_panel as browser_api
from app.api import generated_images
from app.api import network as network_api
from app.api import shared_files
from app.network import browser_panel as remote_panel
from app.network import control, tokens


class FakeContext:
    device_id = "self"
    network_id = "net"
    generation = 3

    def member(self, device_id):
        return {
            "self": SimpleNamespace(status="active", kind="worker"),
            "relay": SimpleNamespace(status="active", kind="worker"),
            "phone": SimpleNamespace(status="active", kind="consumer"),
            "pending": SimpleNamespace(status="pending", kind="worker"),
            "gone": SimpleNamespace(status="revoked", kind="worker"),
        }.get(device_id)


REFUSED = ["phone", "self", "pending", "gone", "stranger"]

ROUTES = [
    ("GET", "/api/v1/network/peer/browser/chat"),
    ("POST", "/api/v1/network/peer/browser/chat/start"),
    ("POST", "/api/v1/network/peer/browser/chat/upload?viewer_id=v&runtime_id=r&tab_id=t&field=f"),
    ("GET", "/api/files/peer/chat?path=/etc/hostname"),
    ("GET", "/api/generated-images/peer/chat?path=/workspace/generated-images/a.png"),
    ("POST", "/api/v1/network/peer/control/interrupt"),
]


@pytest.fixture
def peer(monkeypatch):
    tokens.reset_for_tests()
    ctx = FakeContext()
    monkeypatch.setattr(tokens.engine, "context", lambda: ctx)
    # Past authentication every route asks whether this worker owns the chat
    # (404 here) or dispatches a control action (200 here).
    owned = Mock(side_effect=LookupError())
    monkeypatch.setattr(control, "_require_owned", owned)
    handle = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(control, "handle", handle)
    app = FastAPI()
    for router in (browser_api.router, shared_files.router, generated_images.router, network_api.router):
        app.include_router(router)

    def token_for(device_id):
        return {"Authorization": "Bearer " + tokens._mint(device_id, int(time.time()) + 60, tokens._scope(ctx))}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, token_for=token_for, owned=owned, handle=handle, ctx=ctx)
    tokens.reset_for_tests()


@pytest.mark.parametrize("method,url", ROUTES)
@pytest.mark.parametrize("device", REFUSED)
def test_only_another_active_worker_reaches_peer_route(peer, device, method, url):
    response = peer.client.request(method, url, headers=peer.token_for(device), json={})
    assert response.status_code == 403
    peer.owned.assert_not_called()
    peer.handle.assert_not_awaited()


@pytest.mark.parametrize("method,url", ROUTES)
def test_peer_route_needs_a_device_token(peer, method, url):
    assert peer.client.request(method, url, json={}).status_code == 401
    peer.owned.assert_not_called()
    peer.handle.assert_not_awaited()


@pytest.mark.parametrize("method,url", ROUTES)
def test_active_worker_reaches_peer_route(peer, method, url):
    response = peer.client.request(method, url, headers=peer.token_for("relay"), json={})
    if "/peer/control/" in url:
        assert response.status_code == 200
        assert peer.handle.await_args.kwargs["from_device"] == "relay"
    else:
        assert response.status_code == 404
        peer.owned.assert_called_once_with("chat")


def _socket(headers):
    return SimpleNamespace(headers=headers, state=SimpleNamespace(), send_denial_response=AsyncMock(),
                           accept=AsyncMock(), close=AsyncMock())


@pytest.mark.parametrize("device", REFUSED)
def test_peer_browser_socket_refuses_non_worker(peer, monkeypatch, device):
    serve = AsyncMock()
    monkeypatch.setattr(browser_api, "serve_browser_panel", serve)
    ws = _socket(peer.token_for(device))
    asyncio.run(remote_panel.serve_peer(ws, "chat"))
    assert ws.send_denial_response.await_args.args[0].status_code == 403
    ws.accept.assert_not_awaited()
    serve.assert_not_awaited()
    peer.owned.assert_not_called()


def test_peer_browser_socket_admits_active_worker(peer, monkeypatch):
    serve = AsyncMock()
    monkeypatch.setattr(browser_api, "serve_browser_panel", serve)
    monkeypatch.setattr(control, "_require_owned", Mock(return_value={"id": "chat"}))
    ws = _socket(peer.token_for("relay"))
    asyncio.run(remote_panel.serve_peer(ws, "chat"))
    ws.send_denial_response.assert_not_awaited()
    ws.accept.assert_awaited_once()
    serve.assert_awaited_once()
