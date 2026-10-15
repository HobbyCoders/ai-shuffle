"""In-app terminal routes, the terminal WebSocket and who may use them (#1945).

Real shells on real PTYs behind the real auth checks. Credentials here are
generated per test; no secret value is ever printed.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import auth
from app.api import terminals as api
from app.core import desktop_listener, owner_auth, preview_access
from app.core import terminals as service
from app.core.config import settings

pytestmark = [
    pytest.mark.skipif(os.name == "nt" or not service.pty_supported(), reason="needs a Unix PTY"),
    pytest.mark.timeout(90),
]

WORKER = {"id": "dev_test", "name": "TestBox", "platform": "linux", "container": True}
LOOPBACK = "127.0.0.1:8000"
DESKTOP_TOKEN = "d" * 64
OWNER_PAIRED = "shc_owner-device-session"
MEMBER_PAIRED = "shc_member-device-session"


class Surface:
    """Marks the trusted local listener, as ``app.serve`` does; HTTP headers
    cannot set this."""

    def __init__(self, app, ingress="local"):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        if self.ingress is not None:
            scope["shuffle.ingress"] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def ctx(mock_db, tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    monkeypatch.setattr(service, "worker_identity", lambda: dict(WORKER))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(service, "default_folder", lambda: (str(work), "workspace"))
    paired = {OWNER_PAIRED: {"id": "pc-owner", "account_owner_admin": True},
              MEMBER_PAIRED: {"id": "pc-member", "account_owner_admin": False}}
    monkeypatch.setattr(auth, "get_paired_client_from_token",
                        lambda token, touch=True: dict(paired[token]) if token in paired else None)
    reg = service.TerminalRegistry()
    monkeypatch.setattr(service, "registry", reg)
    monkeypatch.setattr(api, "registry", reg)
    desktop_listener.configure(None)
    token, _ = owner_auth.issue_session()

    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.ws_router)

    @app.get("/probe-require-admin")
    def probe(who=Depends(auth.require_admin)):
        return {"who": "set" if who else ""}

    class Ctx:
        pass

    c = Ctx()
    c.app, c.reg, c.owner, c.work, c.tmp = app, reg, token, work, tmp_path
    c.clients = []
    yield c
    reg.kill_all_now()
    for client in c.clients:
        client.__exit__(None, None, None)
    desktop_listener.configure(None)


def client(c, *, host=LOOPBACK, peer="127.0.0.1", origin=True, ingress="local", cookie=None,
           bearer=None, headers=None):
    base = f"http://{host}"
    hdrs = dict(headers or {})
    if origin is True:
        hdrs["Origin"] = base
    elif origin:
        hdrs["Origin"] = origin
    if bearer:
        hdrs["Authorization"] = "Bearer " + bearer
    tc = TestClient(Surface(c.app, ingress), base_url=base, client=(peer, 12345), headers=hdrs)
    tc.__enter__()
    c.clients.append(tc)
    if cookie:
        tc.cookies.set("session", cookie)
    return tc


def owner(c):
    return client(c, cookie=c.owner)


def until(fn, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def create(tc, **body):
    response = tc.post("/api/v1/terminals", json=body)
    assert response.status_code == 201, response.text
    return response.json()["terminal"]


def wait_live(tc, tid):
    assert until(lambda: tc.get(f"/api/v1/terminals/{tid}").json()["terminal"]["state"] == "live")


def wsc(tc, path):
    """Connect on the client's own host (the TestClient default is
    ``testserver``, which no Origin check would accept)."""
    return tc.websocket_connect(str(tc.base_url).rstrip("/").replace("http://", "ws://", 1) + path)


def ws_url(tid, token=None):
    return f"/api/v1/ws/terminals/{tid}" + (f"?token={token}" if token else "")


def read_until(ws, needle: bytes, out=None):
    out = out if out is not None else bytearray()
    while needle not in out:
        message = ws.receive()
        assert message["type"] == "websocket.send", message
        if message.get("bytes") is not None:
            out.extend(message["bytes"])
    return out


def close_code(ws):
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return message["code"]


def next_json(ws, kind):
    while True:
        message = ws.receive()
        assert message["type"] == "websocket.send", message
        if message.get("text"):
            payload = json.loads(message["text"])
            if payload["type"] == kind:
                return payload


def attach(ws):
    first = json.loads(ws.receive_text())
    assert first["type"] == "attached" and first["protocol"] == 1
    replay = bytearray()
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            replay.extend(message["bytes"])
            continue
        payload = json.loads(message["text"])
        if payload["type"] == "replayed":
            break
    assert len(replay) == first["replay_bytes"]
    return first, bytes(replay)


def strip(data: bytes) -> bytes:
    return re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", b"", bytes(data))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_targets_describe_this_worker(ctx):
    body = owner(ctx).get("/api/v1/terminals/targets").json()
    assert body["supported"] is True and body["unsupported_reason"] is None
    assert body["worker"] == WORKER
    assert body["targets"] == [{"kind": "worker", "worker_id": "dev_test", "name": "TestBox", "available": True}]
    assert body["shells"][0] == {"name": "bash", "default": True} and body["default_shell"] == "bash"
    assert {s["name"] for s in body["shells"]} <= {"bash", "sh"}
    assert body["default_folder"] == {"path": str(ctx.work), "source": "workspace"}
    assert (body["limit"], body["open"]) == (8, 0)


def test_targets_for_a_chat_and_an_unknown_chat(ctx, monkeypatch):
    monkeypatch.setattr(service, "chat_folder", lambda sid: (str(ctx.tmp), "worktree") if sid == "s1" else None)
    tc = owner(ctx)
    assert tc.get("/api/v1/terminals/targets?session_id=s1").json()["default_folder"] == {
        "path": str(ctx.tmp), "source": "worktree"}
    monkeypatch.setattr(service, "chat_folder", lambda sid: (_ for _ in ()).throw(
        service.TerminalError("chat_not_found", "That chat doesn't exist.", 404)))
    response = tc.get("/api/v1/terminals/targets?session_id=nope")
    assert response.status_code == 404 and response.json()["code"] == "chat_not_found"


def test_create_list_get_and_filter_by_chat(ctx, monkeypatch):
    monkeypatch.setattr(service, "chat_folder", lambda sid: (str(ctx.tmp), "worktree"))
    tc = owner(ctx)
    plain = create(tc)
    assert plain["id"].startswith("term_") and plain["target"] == {"kind": "worker", "worker_id": "dev_test"}
    assert plain["shell"] == "bash" and plain["cwd"] == str(ctx.work) and plain["folder_source"] == "workspace"
    assert plain["state"] in ("starting", "live") and plain["exit_code"] is None and plain["busy"] is False
    chat = create(tc, session_id="s1", shell="sh", cols=120, rows=40,
                  target={"kind": "worker", "worker_id": "dev_test"})
    assert (chat["cwd"], chat["folder_source"], chat["session_id"]) == (str(ctx.tmp), "worktree", "s1")
    assert (chat["shell"], chat["cols"], chat["rows"]) == ("sh", 120, 40)
    listing = tc.get("/api/v1/terminals").json()
    assert {t["id"] for t in listing["terminals"]} == {plain["id"], chat["id"]}
    assert (listing["open"], listing["limit"], listing["worker"]["name"]) == (2, 8, "TestBox")
    assert [t["id"] for t in tc.get("/api/v1/terminals?session_id=s1").json()["terminals"]] == [chat["id"]]
    assert tc.get(f"/api/v1/terminals/{chat['id']}").json()["terminal"]["id"] == chat["id"]
    missing = tc.get("/api/v1/terminals/term_0000000000000000")
    assert missing.status_code == 404 and missing.json() == {
        "detail": "That terminal is closed or never existed.", "code": "not_found"}


def test_a_terminal_needs_no_chat(ctx, monkeypatch):
    """The Terminal page: the owner opens a shell before any chat exists (to
    sign in to a CLI, say). No chat is looked up, it opens in the workspace,
    and it belongs to no chat's list."""
    def no_chat_lookup(sid):
        raise AssertionError(f"a chatless terminal looked up chat {sid!r}")

    monkeypatch.setattr(service, "chat_folder", no_chat_lookup)
    tc = owner(ctx)
    targets = tc.get("/api/v1/terminals/targets").json()
    assert targets["supported"] is True
    assert targets["default_folder"] == {"path": str(ctx.work), "source": "workspace"}
    term = create(tc)
    assert (term["session_id"], term["cwd"], term["folder_source"]) == (None, str(ctx.work), "workspace")
    assert [t["id"] for t in tc.get("/api/v1/terminals").json()["terminals"]] == [term["id"]]
    monkeypatch.setattr(service, "chat_folder", lambda sid: (str(ctx.tmp), "worktree"))
    assert tc.get("/api/v1/terminals?session_id=s1").json()["terminals"] == []
    wait_live(tc, term["id"])
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data": "pwd; echo NOCHAT_$((6*7))\n"}))
        out = read_until(ws, b"NOCHAT_42")
    assert str(ctx.work).encode() in strip(out)


@pytest.mark.parametrize("cwd,message", [
    ("relative", "The folder must be a full path, for example /workspace/my-project."),
    ("/definitely/not/here", "The folder /definitely/not/here doesn't exist on this worker."),
])
def test_bad_folder_is_refused_without_fallback(ctx, cwd, message):
    tc = owner(ctx)
    response = tc.post("/api/v1/terminals", json={"cwd": cwd})
    assert response.status_code == 400
    assert response.json() == {"detail": message, "code": "bad_folder"}
    assert ctx.reg.count == 0


def test_folder_that_is_a_file_is_refused(ctx):
    path = ctx.tmp / "notes.txt"
    path.write_text("x")
    response = owner(ctx).post("/api/v1/terminals", json={"cwd": str(path)})
    assert response.status_code == 400 and response.json()["detail"] == f"{path} is a file, not a folder."


def test_bad_shell_and_other_target_are_refused(ctx):
    tc = owner(ctx)
    response = tc.post("/api/v1/terminals", json={"shell": "fish-nope"})
    assert response.status_code == 400 and response.json()["code"] == "bad_shell"
    response = tc.post("/api/v1/terminals", json={"target": {"kind": "worker", "worker_id": "dev_other"}})
    assert response.status_code == 400 and response.json() == {
        "detail": "Terminals can open on this worker only for now.", "code": "target_unavailable"}
    response = tc.post("/api/v1/terminals", json={"target": {"kind": "host-ssh"}})
    assert response.json()["code"] == "target_unavailable" and ctx.reg.count == 0


def test_cap_of_eight_names_the_worker(ctx):
    tc = owner(ctx)
    for _ in range(8):
        create(tc, shell="sh")
    response = tc.post("/api/v1/terminals", json={"shell": "sh"})
    assert response.status_code == 409
    assert response.json() == {"detail": "8 terminals are open on TestBox. Close one to open another.",
                               "code": "limit_reached"}
    first = tc.get("/api/v1/terminals").json()["terminals"][0]["id"]
    assert tc.delete(f"/api/v1/terminals/{first}").status_code == 200
    create(tc, shell="sh")


def test_unsupported_worker(ctx, monkeypatch):
    monkeypatch.setattr(service, "pty_supported", lambda: False)
    tc = owner(ctx)
    body = tc.get("/api/v1/terminals/targets").json()
    assert body["supported"] is False and body["shells"] == [] and body["default_shell"] is None
    assert body["unsupported_reason"] == "Terminals are not supported on this worker yet."
    assert body["targets"][0]["available"] is False
    response = tc.post("/api/v1/terminals", json={})
    assert response.status_code == 501 and response.json() == {
        "detail": "Terminals are not supported on this worker yet.", "code": "not_supported"}


def test_paste_lands_at_the_prompt_and_never_executes(ctx):
    tc = owner(ctx)
    term = create(tc, cwd=str(ctx.tmp))
    wait_live(tc, term["id"])
    marker = ctx.tmp / "paste-ran"
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        response = tc.post(f"/api/v1/terminals/{term['id']}/paste",
                           json={"text": f"touch {marker}\ntouch {marker}-2\r\n"})
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "bracketed"
        out = read_until(ws, b"-2")
        time.sleep(1.0)
        assert not marker.exists() and not Path(f"{marker}-2").exists()
        assert b"paste-ran-2" in strip(out)
    too_big = tc.post(f"/api/v1/terminals/{term['id']}/paste", json={"text": "x" * (service.PASTE_MAX_CHARS + 1)})
    assert too_big.status_code == 413 and too_big.json()["code"] == "paste_too_large"
    assert tc.post("/api/v1/terminals/term_nope/paste", json={"text": "x"}).status_code == 404


def test_sh_refuses_a_multiline_paste_even_after_output_turned_bracketed_paste_on(ctx):
    # sh can't hold a bracketed paste; ESC[?2004h in earlier output (here a
    # printf) must not make the server send one, or every line but the last runs.
    tc = owner(ctx)
    term = create(tc, cwd=str(ctx.tmp), shell="sh")
    wait_live(tc, term["id"])
    marker = ctx.tmp / "sh-paste-ran"
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data": "printf '\\033[?2004h'; echo STALE_$((40+2))\r"}))
        read_until(ws, b"STALE_42")
        time.sleep(0.5)
        response = tc.post(f"/api/v1/terminals/{term['id']}/paste",
                           json={"text": f"touch {marker}-1\ntouch {marker}-2\ntouch {marker}-3\n"})
        assert response.status_code == 422, response.text
        assert response.json() == {
            "detail": "This shell can't hold a multi-line command without running it. Paste one line, or use bash.",
            "code": "paste_multiline_unsupported"}
        single = tc.post(f"/api/v1/terminals/{term['id']}/paste", json={"text": f"touch {marker}-1\n"})
        assert single.status_code == 200 and single.json()["mode"] == "plain"
        time.sleep(1.0)
    assert not list(ctx.tmp.glob("sh-paste-ran*"))


def test_restart_an_exited_terminal(ctx):
    tc = owner(ctx)
    term = create(tc, shell="sh")
    wait_live(tc, term["id"])
    assert tc.post(f"/api/v1/terminals/{term['id']}/restart").json()["code"] == "not_exited"
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data": "exit 3\n"}))
        while True:
            message = ws.receive()
            if message.get("text"):
                payload = json.loads(message["text"])
                if payload["type"] == "state" and payload["terminal"]["state"] == "exited":
                    assert payload["terminal"]["exit_code"] == 3
                    break
        ws.send_text(json.dumps({"type": "input", "data": "echo\n"}))
        assert next_json(ws, "error")["code"] == "terminal_exited"
        paste = tc.post(f"/api/v1/terminals/{term['id']}/paste", json={"text": "x"})
        assert paste.status_code == 409 and paste.json()["code"] == "terminal_exited"
        restarted = tc.post(f"/api/v1/terminals/{term['id']}/restart")
        assert restarted.status_code == 200 and restarted.json()["terminal"]["id"] == term["id"]
        assert restarted.json()["terminal"]["state"] in ("starting", "live")
        ws.send_text(json.dumps({"type": "input", "data": "echo BACK_$((40+2))\n"}))
        read_until(ws, b"BACK_42")


def test_close_kills_the_process_group_and_ends_attachments(ctx):
    tc = owner(ctx)
    term = create(tc, cwd=str(ctx.tmp))
    wait_live(tc, term["id"])
    shell_pid = ctx.reg.get(term["id"]).pid
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data": "sleep 300 &\nsleep 301\n"}))
        assert until(lambda: len(service._session_members(shell_pid)) >= 3)
        members = service._session_members(shell_pid)
        response = tc.delete(f"/api/v1/terminals/{term['id']}")
        assert response.json() == {"closed": True, "id": term["id"]}
        assert close_code(ws) == 4410
    assert until(lambda: not service._session_members(shell_pid))
    assert all(not os.path.exists(f"/proc/{pid}") or _zombie(pid) for pid in members)
    assert tc.get(f"/api/v1/terminals/{term['id']}").status_code == 404
    assert tc.delete(f"/api/v1/terminals/{term['id']}").status_code == 404


def _zombie(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        return stat[stat.rfind(b")") + 2:].split()[0] == b"Z"
    except OSError:
        return True


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def test_attach_replay_input_and_reload(ctx):
    tc = owner(ctx)
    term = create(tc)
    wait_live(tc, term["id"])
    with wsc(tc, ws_url(term["id"])) as ws:
        first, replay = attach(ws)
        assert first["terminal"]["id"] == term["id"] and first["terminal"]["attached"] == 1
        assert first["limits"]["max_message_bytes"] == api.MAX_CLIENT_MESSAGE_BYTES
        ws.send_text(json.dumps({"type": "input", "data": "echo HELLO_$((6*7))\n"}))
        read_until(ws, b"HELLO_42")
        ws.send_text(json.dumps({"type": "ping"}))
        assert next_json(ws, "pong") == {"type": "pong"}
    # A browser reload: a new socket replays what came before.
    with wsc(tc, ws_url(term["id"])) as ws:
        _, replay = attach(ws)
        assert b"HELLO_42" in replay


def test_resize_reaches_the_pty(ctx):
    tc = owner(ctx)
    term = create(tc, shell="sh")
    wait_live(tc, term["id"])
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "resize", "cols": 132, "rows": 43}))
        ws.send_text(json.dumps({"type": "input", "data": "stty size\n"}))
        read_until(ws, b"43 132")
        ws.send_text(json.dumps({"type": "resize", "cols": 99999, "rows": 1}))
        ws.send_text(json.dumps({"type": "input", "data": "stty size\n"}))
        read_until(ws, b"2 500")
        ws.send_text(json.dumps({"type": "resize", "cols": "80", "rows": 24}))
        assert close_code(ws) == 4400


def test_two_attachments_see_the_same_terminal(ctx):
    tc = owner(ctx)
    term = create(tc)
    wait_live(tc, term["id"])
    with wsc(tc, ws_url(term["id"])) as one, wsc(tc, ws_url(term["id"])) as two:
        attach(one)
        attach(two)
        assert tc.get(f"/api/v1/terminals/{term['id']}").json()["terminal"]["attached"] == 2
        one.send_text(json.dumps({"type": "input", "data": "echo FROM_ONE_$((1+1))\n"}))
        read_until(one, b"FROM_ONE_2")
        read_until(two, b"FROM_ONE_2")
        two.send_text(json.dumps({"type": "input", "data": "echo FROM_TWO_$((1+2))\n"}))
        read_until(one, b"FROM_TWO_3")


@pytest.mark.parametrize("send,code", [
    (lambda ws: ws.send_bytes(b"ls\n"), 4400),
    (lambda ws: ws.send_text("x" * (api.MAX_CLIENT_MESSAGE_BYTES + 1)), 4400),
    (lambda ws: ws.send_text("[1,2]"), 4400),
    (lambda ws: ws.send_text(json.dumps({"type": "input", "data": 5})), 4400),
    (lambda ws: ws.send_text(json.dumps({"type": "exec", "data": "ls"})), 4400),
])
def test_protocol_violations_close_the_socket(ctx, send, code):
    tc = owner(ctx)
    term = create(tc, shell="sh")
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        send(ws)
        assert close_code(ws) == code
    assert ctx.reg.get(term["id"]).state != "exited"  # the terminal itself lives on


def test_input_rate_is_bounded(ctx, monkeypatch):
    tc = owner(ctx)
    term = create(tc, shell="sh")
    monkeypatch.setattr(api, "MESSAGE_BURST", 3)
    monkeypatch.setattr(api, "MESSAGES_PER_SECOND", 0.001)
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        for _ in range(4):
            ws.send_text(json.dumps({"type": "ping"}))
        assert close_code(ws) == 4429
    monkeypatch.setattr(api, "MESSAGE_BURST", 200)
    monkeypatch.setattr(api, "INPUT_BURST_BYTES", 100)
    monkeypatch.setattr(api, "INPUT_BYTES_PER_SECOND", 0.001)
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data": "#" * 101}))
        assert close_code(ws) == 4429


def test_unknown_terminal_socket(ctx):
    with wsc(owner(ctx), ws_url("term_missing")) as ws:
        assert close_code(ws) == 4004


# ---------------------------------------------------------------------------
# Security: who may use terminals
# ---------------------------------------------------------------------------

def _refused(response, detail=None):
    assert response.status_code in (401, 403), response.status_code
    if detail:
        assert response.json()["detail"] == detail


ROUTES = [
    ("get", "/api/v1/terminals", None),
    ("get", "/api/v1/terminals/targets", None),
    ("post", "/api/v1/terminals/from-files", {"path": ""}),
    ("get", "/api/v1/terminals/monitor?session_id=s1", None),
    ("get", "/api/v1/terminals/monitor/task?session_id=s1", None),
    ("post", "/api/v1/terminals", {}),
    ("get", "/api/v1/terminals/{id}", None),
    ("delete", "/api/v1/terminals/{id}", None),
    ("post", "/api/v1/terminals/{id}/restart", None),
    ("post", "/api/v1/terminals/{id}/paste", {"text": "touch /tmp/should-not"}),
]


def _every_route_refused(c, tc, detail=None):
    tid = create(owner(c), shell="sh")["id"]
    before = c.reg.count
    for method, path, body in ROUTES:
        kwargs = {"json": body} if body is not None else {}
        _refused(getattr(tc, method)(path.format(id=tid), **kwargs), detail)
    assert c.reg.count == before  # nothing was opened or closed
    with wsc(tc, ws_url(tid)) as ws:
        assert close_code(ws) == 4001
    with wsc(tc, "/api/v1/ws/terminals/monitor/task?session_id=s1") as ws:
        assert close_code(ws) == 4001


def test_anonymous_lan_request_is_refused(ctx):
    _every_route_refused(ctx, client(ctx, host="192.168.1.20:8000", peer="192.168.1.50"))


def test_passwordless_loopback_is_refused_although_require_admin_admits_it(ctx):
    tc = client(ctx)
    assert tc.get("/probe-require-admin").status_code == 200  # the generic owner bypass
    _every_route_refused(ctx, tc, api.OWNER_ONLY)


def test_trusted_lan_is_refused(ctx, monkeypatch):
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    tc = client(ctx, host="192.168.1.20:8000", peer="192.168.1.50")
    assert tc.get("/probe-require-admin").status_code == 200
    _every_route_refused(ctx, tc, api.OWNER_ONLY)


def test_owner_session_on_the_remote_listener_is_refused(ctx):
    _every_route_refused(ctx, client(ctx, ingress="remote", cookie=ctx.owner))


def test_api_key_is_refused(ctx):
    from app.db import api_users

    key = "aiu_" + os.urandom(16).hex()
    api_users.create_api_user("user-1", "Integration", auth.hash_api_key(key))
    tc = client(ctx, bearer=key)
    _every_route_refused(ctx, tc)
    with wsc(client(ctx), ws_url(create(owner(ctx), shell="sh")["id"], key)) as ws:
        assert close_code(ws) == 4001


def test_member_paired_device_is_refused(ctx):
    _every_route_refused(ctx, client(ctx, bearer=MEMBER_PAIRED))
    with wsc(client(ctx), ws_url(create(owner(ctx), shell="sh")["id"], MEMBER_PAIRED)) as ws:
        assert close_code(ws) == 4001


@pytest.mark.parametrize("label", ["peer-worker", "share-link"])
def test_peer_worker_and_share_tokens_are_refused(ctx, label):
    stranger = f"{label}-" + os.urandom(16).hex()
    _every_route_refused(ctx, client(ctx, bearer=stranger))
    tid = create(owner(ctx), shell="sh")["id"]
    with wsc(client(ctx), ws_url(tid, stranger)) as ws:
        assert close_code(ws) == 4001
    # An explicit wrong credential never falls back to the ambient owner cookie.
    with wsc(owner(ctx), ws_url(tid, stranger)) as ws:
        assert close_code(ws) == 4001


def test_a_valid_peer_worker_token_is_refused(ctx, monkeypatch):
    """A paired worker's device token that the network layer ACCEPTS (the
    #1917 relay's authority) is still not the owner."""
    from app.network import tokens

    peer = "dvt_" + os.urandom(16).hex()
    monkeypatch.setattr(tokens, "verify_token", lambda token: "dev_peer" if token == peer else None)
    _every_route_refused(ctx, client(ctx, bearer=peer))
    _every_route_refused(ctx, client(ctx, headers={"X-Shuffle-Device-Token": peer}))
    tid = create(owner(ctx), shell="sh")["id"]
    with wsc(client(ctx), ws_url(tid, peer)) as ws:
        assert close_code(ws) == 4001


def test_preview_cookie_is_refused_even_alongside_the_owner(ctx):
    preview = preview_access.mint("agent-session")
    alone = client(ctx)
    alone.cookies.set(preview_access.COOKIE_NAME, preview)
    _every_route_refused(ctx, alone)
    both = owner(ctx)
    both.cookies.set(preview_access.COOKIE_NAME, preview)
    _every_route_refused(ctx, both, api.PREVIEW_REFUSED)
    tid = create(owner(ctx), shell="sh")["id"]
    with wsc(both, ws_url(tid, ctx.owner)) as ws:
        assert close_code(ws) == 4001


def test_cross_origin_socket_with_owner_cookie_is_refused(ctx):
    tid = create(owner(ctx), shell="sh")["id"]
    evil = client(ctx, origin="https://attacker.example", cookie=ctx.owner)
    with wsc(evil, ws_url(tid)) as ws:
        assert close_code(ws) == 4001
    no_origin = client(ctx, origin=None, cookie=ctx.owner)
    with wsc(no_origin, ws_url(tid)) as ws:
        assert close_code(ws) == 4001
    _refused(evil.post("/api/v1/terminals", json={}))


def test_owner_paths_that_are_admitted(ctx):
    """Owner session (cookie or explicit token), owner-admin paired device
    and the desktop app's own token-carrying loopback request."""
    tid = create(owner(ctx), shell="sh")["id"]
    assert client(ctx, bearer=ctx.owner).get("/api/v1/terminals").status_code == 200
    paired = client(ctx, bearer=OWNER_PAIRED)
    assert paired.get("/api/v1/terminals").status_code == 200
    made = create(paired, shell="sh")
    assert ctx.reg.get(made["id"]).created_by == "paired:pc-owner"
    with wsc(client(ctx), ws_url(tid, OWNER_PAIRED)) as ws:
        attach(ws)
    with wsc(client(ctx, origin=None), ws_url(tid, ctx.owner)) as ws:
        attach(ws)
    desktop_listener.configure(DESKTOP_TOKEN)
    desk = client(ctx, origin=None, headers={desktop_listener.HEADER: DESKTOP_TOKEN})
    made = create(desk, shell="sh")
    assert ctx.reg.get(made["id"]).created_by == "desktop"
    with wsc(desk, ws_url(tid)) as ws:
        attach(ws)
    # Loopback without the desktop's token is not the desktop.
    _refused(client(ctx).get("/api/v1/terminals"))
    with wsc(client(ctx), ws_url(tid)) as ws:
        assert close_code(ws) == 4001


def test_shell_environment_has_no_app_secrets(ctx, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "placeholder-value")
    monkeypatch.setenv("TERMTEST_DB_PASSWORD", "placeholder-value")
    tc = owner(ctx)
    term = create(tc, shell="sh")
    with wsc(tc, ws_url(term["id"])) as ws:
        attach(ws)
        ws.send_text(json.dumps({"type": "input", "data":
            'test -z "${SECRET_KEY+x}${TERMTEST_DB_PASSWORD+x}" && echo CLEAN_$((40+2))\n'}))
        read_until(ws, b"CLEAN_42")


TERMINAL_MODULES = {"app.core.terminals", "app.api.terminals"}
TERMINAL_WIRING = {"app/api/terminals.py", "app/main.py", "app/core/runner_capabilities.py",
                   "app/core/terminals.py"}


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[0] == "fluid":  # fluid/fluid/x.py is the ``fluid`` package
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(tree, rel: str):
    """Every module a file imports, with relative imports resolved and
    ``from pkg import name`` counted as ``pkg.name`` too."""
    import ast

    this = _module_name(rel)
    package = this if rel.endswith("__init__.py") else this.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[:len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join([*anchor, base] if base else anchor)
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}"


def _terminal_strings(tree):
    """String literals naming the terminal modules or routes (dynamic
    imports, ``importlib``, HTTP calls to our own API)."""
    import ast

    needles = ("app.core.terminals", "app.api.terminals", "/terminals", "ws/terminals")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(n in node.value for n in needles):
                yield node.value


def test_no_model_facing_path_reaches_terminals():
    """Only the owner routes, the app wiring and the capability probe reach
    the terminal service; no tool, hook, automation, MCP server or agent
    module imports it (however the import is spelled) or calls its routes."""
    import ast

    root = Path(__file__).resolve().parents[2]
    found = {}
    for base in ("app", "fluid"):
        for path in (root / base).rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=rel)
            hits = {m for m in _imported_modules(tree, rel) if m in TERMINAL_MODULES}
            hits |= set(_terminal_strings(tree))
            if hits:
                found[rel] = hits
    assert set(found) <= TERMINAL_WIRING, {k: v for k, v in found.items() if k not in TERMINAL_WIRING}
    assert "app.api.terminals" in found["app/main.py"]


def test_scanner_catches_every_import_spelling():
    import ast

    for source, rel in (
        ("from app.api import terminals", "app/x.py"),
        ("from ..core import terminals", "app/api/x.py"),
        ("from . import terminals", "app/core/x.py"),
        ("from app.core.terminals import (\n    registry,\n)", "app/x.py"),
        ("import app.core.terminals as t", "fluid/fluid/tools/x.py"),
    ):
        mods = set(_imported_modules(ast.parse(source), rel))
        assert mods & TERMINAL_MODULES, source
    dynamic = ast.parse("importlib.import_module('app.core.' + 'x'); f('/api/v1/terminals')")
    assert list(_terminal_strings(dynamic))
    assert not set(_imported_modules(ast.parse("from app.core import terminal_tools"), "app/x.py")) \
        & TERMINAL_MODULES


def test_every_terminal_route_in_the_app_requires_the_owner():
    """Whatever reaches the terminal service over HTTP in the real app goes
    through ``require_terminal_owner``; the one socket authenticates itself
    (and keeps re-checking, see the revocation tests)."""
    from app.main import app as real_app

    def walk(routes):
        for route in routes:
            if hasattr(route, "effective_candidates"):  # FastAPI's included routers
                yield from walk(route.effective_candidates())
                yield from route.effective_low_priority_routes()
            else:
                yield route

    def deps(dependant):
        for sub in dependant.dependencies:
            yield sub.call
            yield from deps(sub)

    http, sockets = [], []
    for route in walk(real_app.routes):
        original = getattr(route, "original_route", route)
        endpoint = getattr(original, "endpoint", None)
        path = getattr(route, "path", "") or getattr(original, "path", "") or ""
        if "terminals" not in path and getattr(endpoint, "__module__", "") != "app.api.terminals":
            continue
        if getattr(original, "methods", None):
            assert api.require_terminal_owner in set(deps(original.dependant)), path
            http.append(path)
        else:
            assert endpoint in (api.terminal_socket, api.monitor_socket), path
            sockets.append(path)
    assert len(http) == 10, http
    expected_sockets = {"/api/v1/ws/terminals/{terminal_id}",
                        "/api/v1/ws/terminals/monitor/{task_id}"}
    assert not sockets or set(sockets) == expected_sockets, sockets
    assert {r.endpoint for r in api.ws_router.routes} == {api.terminal_socket, api.monitor_socket}


def _close_within(ws, seconds=6.0):
    """The close code if the server closes the socket in time, else None.
    PING_SECONDS is patched small so ``receive`` never blocks for long."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return message["code"]
    return None


@pytest.fixture
def fast_recheck(monkeypatch):
    monkeypatch.setattr(api, "AUTH_RECHECK_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(api, "PING_SECONDS", 0.2)


def test_open_socket_is_closed_when_the_owner_session_is_revoked(ctx, fast_recheck):
    """Authority is held for the life of the socket, not just the handshake:
    signing out (or the session expiring) ends every terminal socket that
    rode on that session, by cookie or by explicit token."""
    tid = create(owner(ctx), shell="sh")["id"]
    by_cookie = owner(ctx)
    with wsc(by_cookie, ws_url(tid)) as cookie_ws, \
            wsc(client(ctx, origin=None), ws_url(tid, ctx.owner)) as token_ws:
        attach(cookie_ws)
        attach(token_ws)
        assert _close_within(cookie_ws, 1.0) is None  # still owner: stays open
        owner_auth.revoke_local_session(ctx.owner)
        assert _close_within(cookie_ws) == 4001
        assert _close_within(token_ws) == 4001


def test_open_socket_is_closed_when_the_paired_device_loses_owner_admin(ctx, fast_recheck, monkeypatch):
    tid = create(owner(ctx), shell="sh")["id"]
    with wsc(client(ctx), ws_url(tid, OWNER_PAIRED)) as ws:
        attach(ws)
        assert _close_within(ws, 1.0) is None
        monkeypatch.setattr(auth, "get_paired_client_from_token", lambda token, touch=True: None)
        assert _close_within(ws) == 4001


def test_open_socket_closes_when_authority_cannot_be_rechecked(ctx, fast_recheck, monkeypatch):
    """A recheck that cannot reach a verdict (hub down) fails closed."""
    from fastapi import HTTPException

    tid = create(owner(ctx), shell="sh")["id"]
    with wsc(client(ctx), ws_url(tid, OWNER_PAIRED)) as ws:
        attach(ws)

        def unavailable(token, touch=True):
            raise HTTPException(503, "hub unavailable")

        monkeypatch.setattr(auth, "get_paired_client_from_token", unavailable)
        assert _close_within(ws) == 1013


def test_websocket_tokens_are_redacted_from_uvicorn_log_lines():
    """uvicorn logs the path WITH its query string when a socket is refused
    before accept (and on an HTTP response to a WebSocket request). The
    terminal socket's explicit ``?token=`` must never reach a log."""
    import logging

    from app.main import _UvicornWebSocketFilter

    secret = "shr_" + "q" * 40
    flt = _UvicornWebSocketFilter()
    for fmt, args in (
        ('%s - "WebSocket %s" 403', (("10.0.0.9", 5555), f"/api/v1/ws/terminals/t1?token={secret}")),
        ('%s - "WebSocket %s" %d', (("10.0.0.9", 5555), f"/api/v1/ws/terminals/t1?x=1&token={secret}", 401)),
        ("%s", (f"/api/v1/ws/chat?access_token={secret}&ticket={secret}",)),
    ):
        record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, fmt, args, None)
        assert flt.filter(record) is True
        rendered = record.getMessage()
        assert secret not in rendered
        assert "/api/v1/ws/" in rendered and "redacted" in rendered
