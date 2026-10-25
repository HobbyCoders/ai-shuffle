"""Peer-terminal control between two workers over the real signed control endpoint (#196).

Each worker has its own database, device identity and network membership
(``tests.core.network.harness``). Calls travel through the real
``/api/v1/network/peer/control/{action}`` route with real device tokens, so the
receiving worker authenticates the caller itself. Nothing in the terminal relay
is stubbed: these are the refusals a forged, plaintext or unapproved request
meets on the worker that would run the shell.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import terminal_targets
from app.core import terminals as service
from app.core.config import settings
from app.db import network as store
from app.network import control, engine, transport
from app.network import terminal_relay as relay
from tests.core.network import harness
from tests.core.network.harness import Worker, converge, join
from tests.core.network.test_transport import ServedBy, _app

pytestmark = [
    pytest.mark.skipif(os.name == "nt" or not service.supported(), reason="needs a Unix PTY"),
    pytest.mark.timeout(60),
]

SOURCE_URL = "https://source.example"
RECIPIENT_URL = "https://recipient.example"
PLAIN_URL = "http://recipient.example"
FORGED_LEASE = "forged-lease-the-source-owner-never-issued"


class Served(ServedBy):
    """``ServedBy`` on the scheme of its base URL, so HTTPS reaches the route as HTTPS."""

    def __init__(self, server: Worker, base_url: str) -> None:
        super().__init__(server, base_url)
        scheme = base_url.split("://", 1)[0]
        self.client = TestClient(_app(), base_url=f"{scheme}://testserver")


# (source, recipient, endpoint URLs per device, served transport per base URL)
Pair = tuple[Worker, Worker, dict[str, list[str]], dict[str, Served]]


@pytest.fixture
def pair(
    workers: Callable[[str], Worker], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Pair]:
    """Source and recipient workers in one network, each serving its own control route."""
    monkeypatch.setattr(type(settings), "network_peer_terminals", property(lambda self: True))
    source, recipient = workers("source"), workers("recipient")
    served = {url: Served(worker, url) for url, worker in
              ((SOURCE_URL, source), (RECIPIENT_URL, recipient), (PLAIN_URL, recipient))}

    def handler(request: httpx.Request) -> httpx.Response:
        for base, server in served.items():
            if str(request.url).startswith(base + "/"):
                return server.handler(request)
        return httpx.Response(502, json={"detail": "unreachable"})

    real_async = httpx.AsyncClient

    def async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0)
    with source.active():
        engine.create_network("Home", device_name="Source")
    join(source, recipient, name="Recipient")
    converge([source, recipient])
    urls = {source.device_id: [SOURCE_URL], recipient.device_id: [RECIPIENT_URL]}
    from app.network import endpoints
    monkeypatch.setattr(endpoints, "peer_endpoints",
                        lambda member, presence: urls[member["device_id"]])
    with source.active():
        store.touch_member(recipient.device_id, endpoints=urls[recipient.device_id])
    with recipient.active():
        store.touch_member(source.device_id, endpoints=urls[source.device_id])
    for worker in (source, recipient):
        transport._links.pop(worker.device_id, None)
    yield source, recipient, urls, served
    relay._approvals.clear()
    relay._owners.clear()
    relay._sessions.clear()


async def _terminal(source: Worker, recipient: Worker, body: dict[str, Any]) -> dict[str, Any]:
    with source.active():
        return await control.call(recipient.device_id, "terminal", body, timeout=10, secure=True)


def _row(recipient: Worker, terminal_capability: int) -> dict[str, Any]:
    return {"id": recipient.device_id, "device_name": "Recipient", "is_self": False, "online": True,
            "capabilities": {"terminal": terminal_capability}, "endpoints": [RECIPIENT_URL]}


async def test_compatible_peer_is_offered_only_for_recipient_approval(pair: Pair) -> None:
    source, recipient, _, served = pair
    with source.active():
        state = await terminal_targets.peer_availability(_row(recipient, 2))
    assert state["reason_code"] == "recipient_approval_required" and not state["available"]
    assert "approve this terminal directly on the receiving worker" in state["reason"]
    assert ("POST", "/api/v1/network/peer/control/terminal") in served[RECIPIENT_URL].requests


async def test_old_peer_is_refused_without_a_peer_call(pair: Pair) -> None:
    source, recipient, _, served = pair
    with source.active():
        state = await terminal_targets.peer_availability(_row(recipient, 1))
    assert state["reason_code"] == "peer_unsupported_or_disabled" and not state["available"]
    assert served[RECIPIENT_URL].requests == []


async def test_peer_with_terminals_turned_off_answers_for_itself(
    pair: Pair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, recipient, _, _ = pair
    # Policy on for the source, off on the recipient: only the recipient's own
    # answer can report it (its advertisement may lag behind the change).
    monkeypatch.setattr(type(settings), "network_peer_terminals",
                        property(lambda self: harness._ACTIVE.get() is not recipient))
    with recipient.active():
        assert service.capability_version() == 1
    with source.active():
        state = await terminal_targets.peer_availability(_row(recipient, 2))
        refused = await control.call(recipient.device_id, "terminal",
                                     {"op": "request", "lease": FORGED_LEASE}, secure=True)
    assert state["reason_code"] == "peer_terminals_disabled" and not state["available"]
    assert refused["status"] == "unavailable"
    assert refused["reason_code"] == "peer_terminals_disabled"
    with recipient.active():
        assert relay.recipient_requests() == []


async def test_forged_request_is_refused_after_asking_the_source_owner(pair: Pair) -> None:
    source, recipient, _, served = pair
    with pytest.raises(control.ControlRefused, match="owner authorization"):
        await _terminal(source, recipient, {"op": "request", "lease": FORGED_LEASE})
    # The recipient asked the source over its own signed channel, and the
    # source had issued no such lease: no request waits for approval.
    assert ("POST", "/api/v1/network/peer/control/terminal-owner") in served[SOURCE_URL].requests
    with recipient.active():
        assert relay.recipient_requests() == []


@pytest.mark.parametrize("op", ["open", "read", "write", "resize"])
async def test_unapproved_lease_cannot_drive_a_shell(pair: Pair, op: str) -> None:
    source, recipient, _, _ = pair
    before = service.registry.count
    with pytest.raises(control.ControlRefused):
        await _terminal(source, recipient,
                        {"op": op, "lease": FORGED_LEASE, "data": "", "cols": 80})
    assert service.registry.count == before


async def test_plaintext_control_route_is_refused_by_the_recipient(pair: Pair) -> None:
    source, recipient, urls, served = pair
    urls[recipient.device_id][:] = [PLAIN_URL]
    transport._links.pop(recipient.device_id, None)
    with source.active():
        # The source never sends terminal control in the clear ...
        with pytest.raises(control.InsecureRoute):
            await control.call(recipient.device_id, "terminal", {"op": "probe"}, secure=True)
        # ... and the recipient refuses it if anything else does.
        with pytest.raises(control.ControlRefused, match="HTTPS"):
            await control.call(recipient.device_id, "terminal", {"op": "probe"})
    assert served[PLAIN_URL].requests


def test_unauthenticated_caller_is_refused_before_the_relay(pair: Pair) -> None:
    _, recipient, _, served = pair
    with recipient.active():
        response = served[RECIPIENT_URL].client.post("/api/v1/network/peer/control/terminal",
                                                      json={"op": "probe"})
        forged = served[RECIPIENT_URL].client.post(
            "/api/v1/network/peer/control/terminal", json={"op": "probe"},
            headers={"Authorization": "Bearer peer-worker-" + os.urandom(16).hex()})
    assert response.status_code in (401, 403) and forged.status_code in (401, 403)
