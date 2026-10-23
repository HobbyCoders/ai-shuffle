"""Refusal matrix and authorization lifetime for the peer-terminal routes (#196).

Every request goes through the real owner HTTP authentication and the real
relay checks. Only the network hop between the two workers is in-process.
"""
import contextvars
import os
import time as real_time
from types import SimpleNamespace

import pytest

from app.api import auth, terminals as api
from app.core import terminals as service
from app.core.config import settings
from app.network import control, engine, runners, terminal_relay as relay
from tests.api.test_peer_terminal_authorization import peer  # noqa: F401 — fixture
from tests.api.test_terminals_api import (  # noqa: F401 — ctx is a fixture
    MEMBER_PAIRED, OWNER_PAIRED, attach, client, ctx, owner, until, ws_url, wsc,
)

ROWS = {
    "destination": {"id": "destination", "device_name": "Destination", "is_self": False, "online": True,
                    "capabilities": {"terminal": 2}, "endpoints": ["https://destination.example"]},
    "other": {"id": "other", "device_name": "Other", "is_self": False, "online": True,
              "capabilities": {"terminal": 2}, "endpoints": ["https://other.example"]},
}
MEMBERS = {"source", "destination", "other"}
_recipient = contextvars.ContextVar("recipient", default="destination")


@pytest.fixture
def wired(peer, monkeypatch):  # noqa: F811 — pytest fixture argument
    """Source and recipients share one process; the relay checks stay real."""
    state = SimpleNamespace(ctx=peer, calls=[], rows={"destination": dict(ROWS["destination"])},
                            members=set(MEMBERS))
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active") if device in state.members else None))
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [dict(r) for r in state.rows.values()]})

    async def transport(device, action, body, **options):
        assert options["secure"] is True
        state.calls.append((device, action, body.get("op")))
        secure = control.inbound_https.set(True)
        try:
            if action == "terminal-owner":
                # The recipient asks the source whether its owner still holds the lease.
                return await relay.handle_owner(body, _recipient.get())
            token = _recipient.set(device)
            try:
                return await relay.handle(body, "source")
            finally:
                _recipient.reset(token)
        finally:
            control.inbound_https.reset(secure)

    monkeypatch.setattr(control, "call", transport)
    return state


@pytest.fixture
def clock(monkeypatch):
    """The relay's monotonic clock, under test control."""
    now = [real_time.monotonic()]
    monkeypatch.setattr(relay, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _policy(monkeypatch, enabled):
    monkeypatch.setattr(type(settings), "network_peer_terminals", property(lambda self, value=enabled: value))


def _request(tc, worker_id="destination"):
    response = tc.post("/api/v1/terminals/peer-authorizations", json={"worker_id": worker_id})
    assert response.status_code == 201, response.text
    return response.json()


def _approve(tc, request_id):
    response = tc.post(f"/api/v1/terminals/peer-requests/{request_id}/approve")
    assert response.status_code == 200, response.text
    return response.json()


def _routes(authorization_id, request_id, worker_id="destination"):
    target = {"kind": "worker", "worker_id": worker_id, "authorization_id": authorization_id}
    return [
        ("post", "/api/v1/terminals/peer-authorizations", {"worker_id": worker_id}),
        ("get", f"/api/v1/terminals/peer-authorizations/{authorization_id}", None),
        ("delete", f"/api/v1/terminals/peer-authorizations/{authorization_id}", None),
        ("get", "/api/v1/terminals/peer-requests", None),
        ("post", f"/api/v1/terminals/peer-requests/{request_id}/approve", None),
        ("delete", f"/api/v1/terminals/peer-requests/{request_id}", None),
        ("post", "/api/v1/terminals", {"target": target}),
    ]


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
    raise AssertionError(label)


# ---------------------------------------------------------------------------
# Callers without terminal permission
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["anonymous-lan", "passwordless-loopback", "owner-cookie-on-remote-listener",
                                   "member-paired-device", "api-key", "peer-worker-token"])
def test_every_peer_route_refuses_callers_without_terminal_permission(wired, label):
    pending = _request(owner(wired.ctx))
    authorization_id, request_id = pending["authorization_id"], pending["request_id"]
    calls, owners = len(wired.calls), dict(relay._owners)
    tc = _caller(wired.ctx, label)
    for method, path, body in _routes(authorization_id, request_id):
        response = getattr(tc, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code in (401, 403), (label, method, path, response.status_code)
    # Refused before any peer call: nothing requested, approved, revoked or opened.
    assert len(wired.calls) == calls
    assert relay._owners == owners
    [listed] = relay.recipient_requests()
    assert listed["request_id"] == request_id and listed["status"] == "pending"
    assert wired.ctx.reg.count == 0


def test_authorization_is_bound_to_the_owner_principal_that_requested_it(wired):
    pending = _request(owner(wired.ctx))
    authorization_id = pending["authorization_id"]
    other_owner_device = client(wired.ctx, bearer=OWNER_PAIRED)
    for method in ("get", "delete"):
        response = getattr(other_owner_device, method)(f"/api/v1/terminals/peer-authorizations/{authorization_id}")
        assert response.status_code == 403
    opened = other_owner_device.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": authorization_id}})
    assert opened.status_code == 403 and opened.json()["code"] == "recipient_approval_required"
    assert authorization_id in relay._owners
    assert ("destination", "terminal", "open") not in wired.calls


# ---------------------------------------------------------------------------
# Peers that are unknown, not usable, or not approved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, status, code", [
    (None, 404, None),
    ({"online": False}, 409, "peer_offline"),
    ({"capabilities": {"terminal": 1}}, 409, "peer_unsupported_or_disabled"),
    ({"endpoints": ["http://destination.example"]}, 409, "https_required"),
])
def test_request_for_an_unusable_peer_is_refused_before_any_peer_call(wired, row, status, code):
    if row is None:
        wired.rows.clear()
    else:
        wired.rows["destination"].update(row)
    response = owner(wired.ctx).post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert response.status_code == status
    if code:
        assert response.json()["detail"]["reason_code"] == code
    assert wired.calls == [] and relay._owners == {} and relay._approvals == {}


def test_insecure_control_route_is_refused_and_leaves_no_authority(wired, monkeypatch):
    async def insecure(*args, **kwargs):
        raise control.InsecureRoute("plaintext")

    monkeypatch.setattr(control, "call", insecure)
    response = owner(wired.ctx).post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "https_required"
    assert relay._owners == {} and relay._approvals == {}


def test_recipient_refuses_a_source_that_is_not_an_active_member(wired):
    tc = owner(wired.ctx)
    wired.members.discard("source")
    response = tc.post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "peer_unavailable"
    assert relay._owners == {} and relay._approvals == {}
    # Membership lost after the request: the recipient owner cannot approve it.
    wired.members.add("source")
    pending = _request(tc)
    wired.members.discard("source")
    assert tc.post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve").status_code == 403
    assert relay._approvals[("source", pending["authorization_id"])]["request"] is None


def test_ids_that_were_never_issued_are_refused(wired):
    tc = owner(wired.ctx)
    forged = "forged-authorization-1234567890"
    assert tc.get(f"/api/v1/terminals/peer-authorizations/{forged}").status_code == 403
    assert tc.delete(f"/api/v1/terminals/peer-authorizations/{forged}").status_code == 403
    assert tc.post("/api/v1/terminals/peer-requests/forged/approve").status_code == 403
    assert tc.delete("/api/v1/terminals/peer-requests/forged").status_code == 403
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": forged}})
    assert opened.status_code == 403 and opened.json()["code"] == "recipient_approval_required"
    assert all(call[2] != "open" for call in wired.calls)
    assert wired.ctx.reg.count == 0


def test_a_pending_request_cannot_open_a_shell(wired):
    tc = owner(wired.ctx)
    pending = _request(tc)
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": pending["authorization_id"]}})
    assert opened.status_code in (403, 503), opened.text
    assert wired.ctx.reg.count == 0
    assert relay._approvals[("source", pending["authorization_id"])]["request"] is None


# ---------------------------------------------------------------------------
# Owner policy off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_off_refuses_every_peer_route_but_still_allows_revocation(wired, monkeypatch):
    tc = owner(wired.ctx)
    approved = _request(tc)
    _approve(tc, approved["request_id"])
    pending = _request(tc)
    _policy(monkeypatch, False)

    refused = tc.post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert refused.status_code == 409 and refused.json()["detail"]["reason_code"] == "peer_terminals_disabled"
    assert set(relay._owners) == {approved["authorization_id"], pending["authorization_id"]}
    target = {"kind": "worker", "worker_id": "destination", "authorization_id": approved["authorization_id"]}
    opened = tc.post("/api/v1/terminals", json={"target": target})
    assert opened.status_code == 400 and opened.json()["code"] == "target_unavailable"
    assert not [t for t in tc.get("/api/v1/terminals/targets").json()["targets"] if t.get("worker_id") == "destination"]
    assert tc.get("/api/v1/terminals/peer-requests").json() == {"requests": []}
    assert tc.post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve").status_code == 403
    status = tc.get(f"/api/v1/terminals/peer-authorizations/{approved['authorization_id']}")
    assert status.status_code == 403 and approved["authorization_id"] not in relay._owners
    # The recipient enforces its own policy, whatever the source sends.
    secure = control.inbound_https.set(True)
    try:
        assert (await relay.handle({"op": "request", "lease": "new-lease-while-off-123456"}, "source"))[
            "status"] == "unavailable"
        with pytest.raises(PermissionError):
            await relay.handle({"op": "open", "lease": approved["authorization_id"]}, "source")
    finally:
        control.inbound_https.reset(secure)
    assert wired.ctx.reg.count == 0
    # Revocation only removes access, so it keeps working with the policy off.
    assert tc.delete(f"/api/v1/terminals/peer-requests/{pending['request_id']}").json() == {"revoked": True}
    assert tc.delete(f"/api/v1/terminals/peer-authorizations/{pending['authorization_id']}").json() == {
        "revoked": True}
    assert relay._owners == {} and ("source", pending["authorization_id"]) not in relay._approvals


# ---------------------------------------------------------------------------
# Authorization lifetime and binding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start", [None, 199.476812733])
def test_pending_request_expires_after_two_minutes(wired, clock, start):
    # 199.476812733: a fresh CI runner's monotonic clock, where (t + 120) - t
    # is 119.99999999999997 and a truncated expires_in read 119.
    if start is not None:
        clock[0] = start
    tc = owner(wired.ctx)
    pending = _request(tc)
    assert pending["expires_in"] == relay.PENDING_SECONDS
    clock[0] += relay.PENDING_SECONDS - 1
    assert [r["request_id"] for r in tc.get("/api/v1/terminals/peer-requests").json()["requests"]] == [
        pending["request_id"]]
    clock[0] += 2
    assert tc.get("/api/v1/terminals/peer-requests").json() == {"requests": []}
    assert tc.post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve").status_code == 403


def test_approved_authorization_expires_after_its_maximum_lifetime(wired, clock):
    tc = owner(wired.ctx)
    pending = _request(tc)
    authorization_id = pending["authorization_id"]
    approved = _approve(tc, pending["request_id"])
    assert approved["status"] == "approved" and approved["expires_in"] == relay.MAX_LIFETIME_SECONDS
    clock[0] += relay.MAX_LIFETIME_SECONDS - 1
    status = tc.get(f"/api/v1/terminals/peer-authorizations/{authorization_id}").json()
    assert status["status"] == "approved" and status["used"] is False
    clock[0] += 2
    assert tc.get(f"/api/v1/terminals/peer-authorizations/{authorization_id}").status_code == 403
    target = next(t for t in tc.get("/api/v1/terminals/targets").json()["targets"]
                  if t.get("worker_id") == "destination")
    assert target["available"] is False and "authorization_id" not in target
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": authorization_id}})
    assert opened.status_code == 403 and opened.json()["code"] == "recipient_approval_required"
    assert wired.ctx.reg.count == 0
    assert relay.recipient_requests() == []


@pytest.mark.asyncio
async def test_recipient_expiry_holds_even_when_the_source_still_trusts_the_lease(wired, clock):
    tc = owner(wired.ctx)
    pending = _request(tc)
    lease = pending["authorization_id"]
    _approve(tc, pending["request_id"])
    clock[0] += relay.MAX_LIFETIME_SECONDS + 1
    entry = relay._owners[lease]
    relay._owners[lease] = (*entry[:3], clock[0] + relay.MAX_LIFETIME_SECONDS)  # source deadline not reached
    secure = control.inbound_https.set(True)
    try:
        assert (await relay.handle({"op": "status", "lease": lease}, "source"))["status"] == "expired"
        with pytest.raises(PermissionError):
            await relay.handle({"op": "open", "lease": lease}, "source")
    finally:
        control.inbound_https.reset(secure)
    assert wired.ctx.reg.count == 0


def test_reconnect_after_expiry_is_refused(wired, clock, monkeypatch):
    source = service.TerminalRegistry()
    monkeypatch.setattr(api, "registry", source)
    tc = owner(wired.ctx)
    pending = _request(tc)
    lease = pending["authorization_id"]
    _approve(tc, pending["request_id"])
    target = {"kind": "worker", "worker_id": "destination", "authorization_id": lease}
    opened = tc.post("/api/v1/terminals", json={"target": target})
    assert opened.status_code == 201, opened.text
    terminal = opened.json()["terminal"]
    with wsc(tc, ws_url(terminal["id"])) as ws:
        attach(ws)
    assert wired.ctx.reg.count == 1
    try:
        clock[0] += relay.MAX_LIFETIME_SECONDS + 1
        # The next relay read hits the recipient's deadline: the PTY closes.
        assert until(lambda: wired.ctx.reg.count == 0)
        assert until(lambda: source.get(terminal["id"]).state == service.STATE_EXITED)
        assert until(lambda: lease not in relay._owners)
        # Neither the same authorization nor an in-place restart reconnects.
        again = tc.post("/api/v1/terminals", json={"target": target})
        assert again.status_code == 403 and again.json()["code"] == "recipient_approval_required"
        restart = tc.post(f"/api/v1/terminals/{terminal['id']}/restart")
        assert restart.status_code == 409 and restart.json()["code"] == "target_pick_required"
        assert ("source", lease) not in relay._sessions and wired.ctx.reg.count == 0
    finally:
        source.kill_all_now()


def test_approval_does_not_carry_over_to_another_target_or_request(wired):
    wired.rows["other"] = dict(ROWS["other"])
    tc = owner(wired.ctx)
    first = _request(tc)
    _approve(tc, first["request_id"])
    rows = {t.get("worker_id"): t for t in tc.get("/api/v1/terminals/targets").json()["targets"]}
    assert rows["destination"]["authorization_id"] == first["authorization_id"]
    assert rows["other"]["available"] is False and "authorization_id" not in rows["other"]
    assert rows["other"]["reason_code"] == "recipient_approval_required"
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "other", "authorization_id": first["authorization_id"]}})
    assert opened.status_code == 403 and opened.json()["code"] == "recipient_approval_required"
    assert ("other", "terminal", "open") not in wired.calls
    assert relay._approvals[("source", first["authorization_id"])]["used"] is False
    # A new request to the same worker needs its own approval.
    second = _request(tc)
    assert second["status"] == "pending"
    status = tc.get(f"/api/v1/terminals/peer-authorizations/{second['authorization_id']}").json()
    assert status["status"] == "pending"
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": second["authorization_id"]}})
    assert opened.status_code in (403, 503) and wired.ctx.reg.count == 0
