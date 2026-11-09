"""The receiving worker accepts only a linked device's signature (#674).

Requests go through the real owner HTTP authentication, the real relay and real
ECDSA verification. Only the hop between the two workers is in-process, and the
link table is a dict (its four statements are exercised on PostgreSQL by the
two-worker run recorded in docs/internal/terminals.md).
"""
import contextvars
import time as real_time
from types import SimpleNamespace

import pytest

from app.core import terminals as service
from app.core.config import settings
from app.db import terminal_device_links as link_store
from app.network import control, engine, runners, terminal_device_links as links, terminal_relay as relay
from tests.api.peer_device import Device
from tests.api.test_terminals_api import MEMBER_PAIRED, attach, client, ctx, owner, read_until, until, ws_url, wsc  # noqa: F401

ROW = {"id": "destination", "device_name": "Destination", "is_self": False, "online": True,
       "capabilities": {"terminal": service.PEER_TERMINAL_VERSION}, "endpoints": ["https://peer.example"]}
_here = contextvars.ContextVar("here", default="source")


class Context:
    """Which worker the in-process call is on decides the identity it sees."""

    members = {"source", "destination"}

    @property
    def device_id(self):
        return _here.get()

    def member(self, device):
        if device in self.members:
            return SimpleNamespace(kind="worker", status="active", name=device.title())
        return None


def forget_state():
    relay._owners.clear()
    for entry in relay._sessions.values():
        entry[3].cancel()
    relay._sessions.clear()
    links._challenges.clear()
    links._requests.clear()


@pytest.fixture
def peer(ctx, monkeypatch):  # noqa: F811
    from app.api import terminals as api

    monkeypatch.setattr(type(settings), "network_peer_terminals", property(lambda self: True))
    monkeypatch.setattr(engine, "context", Context)
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [dict(ROW)]})
    rows = {}
    monkeypatch.setattr(link_store, "get", lambda key_id: rows.get(key_id))
    monkeypatch.setattr(link_store, "list_links", lambda: list(rows.values()))
    monkeypatch.setattr(link_store, "touch", lambda key_id: rows[key_id].update(last_used_at="now"))
    monkeypatch.setattr(link_store, "delete", lambda key_id: rows.pop(key_id, None) is not None)

    def insert(**row):
        rows[row["key_id"]] = {**row, "created_at": "then", "last_used_at": None}

    monkeypatch.setattr(link_store, "insert", insert)
    source = service.TerminalRegistry()
    monkeypatch.setattr(api, "registry", source)

    async def transport(device, action, body, **options):
        assert options["secure"] is True
        secure = control.inbound_https.set(True)
        here = _here.set(device)
        try:
            if action == "terminal-owner":
                return await relay.handle_owner(body, "destination")
            return await relay.handle(body, "source")
        finally:
            _here.reset(here)
            control.inbound_https.reset(secure)

    monkeypatch.setattr(control, "call", transport)
    yield SimpleNamespace(ctx=ctx, source=source, rows=rows, tc=owner(ctx))
    forget_state()


def link(tc, device):
    asked = tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": device.public_key,
                                                         "label": device.label})
    assert asked.status_code == 201 and asked.json()["status"] == "pending", asked.text
    approved = tc.post("/api/v1/terminals/device-links/approve", json={"code": device.code})
    assert approved.status_code == 201, approved.text


def challenge(tc, device):
    answer = tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "destination", "key_id": device.key_id}).json()
    assert answer["status"] == "linked", answer
    assert answer["receiver"] == "destination" and answer["via"] == "source"
    return answer["challenge"]


def open_with(tc, proof, session_id=None):
    return tc.post("/api/v1/terminals", json={"session_id": session_id,
                                              "target": {"kind": "worker", "worker_id": "destination", "proof": proof}})


def opens(peer, device, **options):
    response = open_with(peer.tc, device.proof(challenge(peer.tc, device), **options))
    assert response.status_code == 201, response.text
    return response.json()["terminal"]


def refused(peer, proof, code):
    before = peer.ctx.reg.count
    response = open_with(peer.tc, proof)
    assert response.status_code == 403 and response.json()["code"] == code, response.text
    assert peer.ctx.reg.count == peer.source.count == before == 0


@pytest.fixture
def device(peer):
    linked = Device()
    link(peer.tc, linked)
    return linked


def test_linked_device_gets_a_real_shell_that_types_resizes_reconnects_and_closes(peer, device):
    terminal = opens(peer, device)
    with wsc(peer.tc, ws_url(terminal["id"])) as ws:
        attach(ws)
        ws.send_json({"type": "input", "data": "printf 'PEER_%s' OK\n"})
        read_until(ws, b"PEER_OK")
        ws.send_json({"type": "resize", "cols": 100, "rows": 40})
        assert until(lambda: peer.ctx.reg.list()[0].cols == 100)
    with wsc(peer.tc, ws_url(terminal["id"])) as ws:  # A reload: output replays and input still works.
        _, replay = attach(ws)
        assert b"PEER_OK" in replay
        ws.send_json({"type": "input", "data": "printf 'AGAIN_%s' OK\n"})
        read_until(ws, b"AGAIN_OK")
    assert peer.tc.delete(f"/api/v1/terminals/{terminal['id']}").status_code == 200
    assert until(lambda: peer.source.count == peer.ctx.reg.count == 0)
    assert peer.rows[device.key_id]["last_used_at"] == "now"


def test_a_chat_opens_in_its_own_folder_on_the_receiver(peer, device, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "chat_folder", lambda session_id: (str(tmp_path), "project") if session_id == "chat-b" else None)
    terminal = opens(peer, device, session_id="chat-b")
    assert terminal["cwd"] == str(tmp_path) and peer.ctx.reg.list()[0].cwd == str(tmp_path)


def test_open_and_paste_runs_nothing_until_enter(peer, device):
    terminal = opens(peer, device, action=links.ACTION_OPEN_AND_PASTE, shell="bash")
    with wsc(peer.tc, ws_url(terminal["id"])) as ws:
        attach(ws)
        pasted = peer.tc.post(f"/api/v1/terminals/{terminal['id']}/paste", json={"text": "printf 'RAN_%s' NOW"})
        assert pasted.status_code == 200, pasted.text
        echoed = read_until(ws, b"RAN_%s")
        real_time.sleep(0.3)
        assert b"RAN_NOW" not in echoed  # At the prompt, unrun.
        ws.send_json({"type": "input", "data": "\n"})
        read_until(ws, b"RAN_NOW")


def test_clipboard_paste_into_an_open_peer_shell_is_ordinary_input(peer, device):
    terminal = opens(peer, device, shell="bash")
    with wsc(peer.tc, ws_url(terminal["id"])) as ws:
        attach(ws)
        response = peer.tc.post(f"/api/v1/terminals/{terminal['id']}/paste", json={"text": "echo PASTED_$((40+2))"})
        assert response.status_code == 200, response.text
        echoed = read_until(ws, b"PASTED_$((40+2))")
        real_time.sleep(0.3)
        assert b"PASTED_42" not in echoed  # Still only at the prompt.


def test_agent_signing_with_the_worker_key_is_refused(peer, device):
    from app.network import crypto

    signing_key = crypto.generate_signing_key()
    proof = device.proof(challenge(peer.tc, device))
    proof["signature"] = crypto.b64u_encode(crypto.sign(signing_key, proof["payload"].encode()))
    refused(peer, proof, "device_proof_invalid")


def test_a_different_key_claiming_the_linked_id_is_refused(peer, device):
    refused(peer, device.proof(challenge(peer.tc, device), signer=Device()), "device_proof_invalid")


def test_an_unlinked_device_gets_no_challenge_and_cannot_open(peer):
    stranger = Device()
    answer = peer.tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "destination", "key_id": stranger.key_id})
    assert answer.json()["status"] == "unlinked" and "challenge" not in answer.json()
    refused(peer, stranger.proof("guessed-challenge"), "device_proof_expired")


def test_no_proof_is_refused(peer):
    refused(peer, None, "device_link_required")


def test_a_replayed_request_is_refused(peer, device):
    proof = device.proof(challenge(peer.tc, device))
    assert open_with(peer.tc, proof).status_code == 201
    response = open_with(peer.tc, proof)
    assert response.status_code == 403 and response.json()["code"] == "device_proof_expired"


def test_an_expired_request_is_refused(peer, device, monkeypatch):
    proof = device.proof(challenge(peer.tc, device))
    later = real_time.monotonic() + links.CHALLENGE_SECONDS + 1
    monkeypatch.setattr(links, "time", SimpleNamespace(monotonic=lambda: later))
    refused(peer, proof, "device_proof_expired")


@pytest.mark.parametrize("overrides", [{"receiver": "other-worker"}, {"via": "other-source"}, {"action": "terminal.exec"},
                                       {"purpose": "something-else"}])
def test_a_signature_made_for_something_else_is_refused(peer, device, overrides):
    refused(peer, device.proof(challenge(peer.tc, device), **overrides), "device_proof_invalid")


def test_swapping_the_signed_chat_or_action_breaks_the_signature(peer, device):
    import json

    for field, value in (("target", {"session_id": "other-chat", "cwd": None, "shell": None}),
                         ("action", links.ACTION_OPEN_AND_PASTE)):
        proof = device.proof(challenge(peer.tc, device))
        proof["payload"] = json.dumps({**json.loads(proof["payload"]), field: value}, separators=(",", ":"))
        refused(peer, proof, "device_proof_invalid")


def test_a_challenge_issued_to_another_key_is_refused(peer, device):
    other = Device()
    link(peer.tc, other)
    refused(peer, other.proof(challenge(peer.tc, device)), "device_proof_invalid")


def test_a_link_made_through_one_worker_does_not_work_through_another(peer, device):
    peer.rows[device.key_id]["via_device"] = "elsewhere"
    answer = peer.tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "destination", "key_id": device.key_id})
    assert answer.json()["status"] == "unlinked"


def test_a_revoked_device_is_refused_and_its_open_shell_closes(peer, device):
    terminal = opens(peer, device)
    pending = challenge(peer.tc, device)
    assert peer.tc.delete(f"/api/v1/terminals/device-links/{device.key_id}").json() == {"revoked": True}
    assert peer.ctx.reg.count == 0 or not peer.ctx.reg.list()
    assert terminal["id"]
    refused_open = open_with(peer.tc, device.proof(pending))
    assert refused_open.status_code == 403 and refused_open.json()["code"] == "device_proof_expired"
    answer = peer.tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "destination", "key_id": device.key_id})
    assert answer.json()["status"] == "unlinked"


def test_the_owner_sees_waiting_devices_without_their_code(peer):
    device = Device("Phone")
    peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": device.public_key,
                                                       "label": "Phone\u0007"})
    listed = peer.tc.get("/api/v1/terminals/device-links").json()
    [waiting] = listed["requests"]
    assert waiting["worker_id"] == "source" and waiting["label"] == "Phone" and listed["links"] == []
    assert device.code not in str(listed) and device.public_key not in str(listed)


def test_a_wrong_code_links_nothing(peer):
    device = Device()
    peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": device.public_key})
    wrong = "ABCD-EFGH-2345" if device.code != "ABCDEFGH2345" else "ZZZZ-ZZZZ-2345"
    assert peer.tc.post("/api/v1/terminals/device-links/approve", json={"code": wrong}).status_code == 404
    assert peer.rows == {}


def test_a_code_with_separators_and_lowercase_still_matches(peer):
    device = Device()
    peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": device.public_key})
    typed = f"{device.code[:4]}-{device.code[4:8]} {device.code[8:]}".lower()
    assert peer.tc.post("/api/v1/terminals/device-links/approve", json={"code": typed}).status_code == 201
    assert device.key_id in peer.rows


def test_a_colliding_request_links_nothing(peer, monkeypatch):
    first, second = Device(), Device()
    for device in (first, second):
        peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": device.public_key})
    monkeypatch.setattr(links, "link_code", lambda raw: "SAMECODE2345")
    assert peer.tc.post("/api/v1/terminals/device-links/approve", json={"code": "SAMECODE2345"}).status_code == 409
    assert peer.rows == {}


@pytest.mark.parametrize("public_key", ["not-base64!", "AAAA", "BA" + "A" * 85])
def test_a_key_that_is_not_a_curve_point_is_refused(peer, public_key):
    response = peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": public_key})
    assert response.status_code == 503 and not links._requests


def test_turning_the_policy_off_refuses_opens(peer, device, monkeypatch):
    proof = device.proof(challenge(peer.tc, device))
    monkeypatch.setattr(type(settings), "network_peer_terminals", property(lambda self: False))
    response = open_with(peer.tc, proof)
    assert response.status_code == 400 and peer.ctx.reg.count == 0
    assert peer.tc.post("/api/v1/terminals/device-links/approve", json={"code": "X"}).status_code == 403


def test_policy_off_on_the_receiver_alone_refuses_the_open(peer, device, monkeypatch):
    proof = device.proof(challenge(peer.tc, device))
    monkeypatch.setattr(type(settings), "network_peer_terminals", property(lambda self: _here.get() != "destination"))
    response = open_with(peer.tc, proof)
    assert response.status_code == 400 and peer.ctx.reg.count == 0
