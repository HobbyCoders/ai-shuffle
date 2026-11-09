"""Who may reach the device-link and peer-open routes (#674).

Every request goes through the real owner HTTP authentication. A caller refused
here cannot approve a link, read the waiting list, revoke, or relay an open.
"""
import os

import pytest

from app.api import auth
from app.network import runners, terminal_device_links as links
from tests.api.peer_device import Device
from tests.api.test_peer_terminal_authorization import ROW, challenge, ctx, forget_state, link, open_with, peer  # noqa: F401
from tests.api.test_terminals_api import MEMBER_PAIRED, client, owner  # noqa: F401


def _caller(c, label):
    if label == "anonymous-lan":
        return client(c, host="192.168.1.20:8000", peer="192.168.1.50")
    if label == "passwordless-loopback":
        return client(c)
    if label == "owner-cookie-on-remote-listener":
        return client(c, ingress="remote", cookie=c.owner)
    if label == "member-paired-device":
        return client(c, bearer=MEMBER_PAIRED)
    if label == "api-key":
        from app.db import api_users

        key = "aiu_" + os.urandom(16).hex()
        api_users.create_api_user("user-1", "Integration", auth.hash_api_key(key))
        return client(c, bearer=key)
    if label == "peer-worker-token":
        return client(c, bearer="peer-worker-" + os.urandom(16).hex())
    if label in ("preview-cookie", "preview-cookie-with-owner"):
        from app.core import preview_access

        tc = client(c, cookie=c.owner) if label.endswith("owner") else client(c)
        tc.cookies.set(preview_access.cookie_name(), preview_access.mint("agent-session"))
        return tc
    raise AssertionError(label)


@pytest.mark.parametrize("label", ["anonymous-lan", "passwordless-loopback", "owner-cookie-on-remote-listener",
                                   "member-paired-device", "api-key", "peer-worker-token",
                                   "preview-cookie", "preview-cookie-with-owner"])
def test_no_one_but_the_owner_can_approve_list_revoke_or_open(peer, label, monkeypatch):  # noqa: F811
    from app.core import preview_access

    monkeypatch.setattr(preview_access, "_session_exists", lambda session_id: True)
    linked, waiting = Device(), Device("Waiting")
    link(peer.tc, linked)
    peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": waiting.public_key})
    proof = linked.proof(challenge(peer.tc, linked))
    request_id = next(iter(links._requests))
    target = {"kind": "worker", "worker_id": "destination", "proof": proof}
    tc = _caller(peer.ctx, label)
    for method, path, body in (
        ("post", "/api/v1/terminals/peer-challenge", {"worker_id": "destination", "key_id": linked.key_id}),
        ("post", "/api/v1/terminals/peer-links", {"worker_id": "destination", "public_key": Device().public_key}),
        ("get", "/api/v1/terminals/device-links", None),
        ("post", "/api/v1/terminals/device-links/approve", {"code": waiting.code}),
        ("delete", f"/api/v1/terminals/device-links/requests/{request_id}", None),
        ("delete", f"/api/v1/terminals/device-links/{linked.key_id}", None),
        ("post", "/api/v1/terminals", {"target": target}),
    ):
        response = getattr(tc, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code in (401, 403), (label, method, path, response.status_code)
    assert list(peer.rows) == [linked.key_id] and request_id in links._requests
    assert peer.ctx.reg.count == 0
    # The proof was never relayed, so the owner can still spend it.
    assert open_with(peer.tc, proof).status_code == 201


@pytest.mark.parametrize("change,status", [({"online": False}, 409), ({"capabilities": {"terminal": 2}}, 409),
                                           ({"endpoints": ["http://peer.example"]}, 409)])
def test_an_unusable_peer_is_refused_before_any_peer_call(peer, change, status, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [{**ROW, **change}]})
    response = peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": Device().public_key})
    assert response.status_code == status and not links._requests


def test_an_unknown_worker_is_refused(peer):  # noqa: F811
    response = peer.tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "nobody", "key_id": "x"})
    assert response.status_code == 404


def test_a_source_that_is_no_longer_a_member_is_refused(peer):  # noqa: F811
    device = Device()
    link(peer.tc, device)
    proof = device.proof(challenge(peer.tc, device))
    from tests.api.test_peer_terminal_authorization import Context
    Context.members = {"destination"}
    try:
        assert open_with(peer.tc, proof).status_code >= 400
        assert peer.ctx.reg.count == 0
    finally:
        Context.members = {"source", "destination"}
        forget_state()
