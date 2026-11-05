"""Focused idle decisions, observational ASGI parity and paused-only holders."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.devloop import idle, guardian, observer, child
from scripts.devloop import __main__ as devloop


def instance(tmp_path):
    states = []
    obj = SimpleNamespace(directory=tmp_path, data={"state": "starting", "app": {}},
                          args=SimpleNamespace(ttl_seconds=0.8, host="127.0.0.1"),
                          fake=SimpleNamespace(log_path=tmp_path / "fake.jsonl"), port=0, vite_port=None)

    def write(**changes):
        obj.data.update(changes)
        states.append(obj.data["state"])

    obj.write = write
    obj.retire = lambda: write(state="stopping")
    obj.states = states
    obj.record_postgres = Mock()
    obj.on_app_exit = Mock()
    return obj


def receipt(lease, **changes):
    app = lease.instance.data["app"]
    app.update(pid=os.getpid(), pid_identity=guardian.identity(os.getpid()))
    record = {"pid": app["pid"], "identity": app["pid_identity"],
              "updated": time.monotonic(), "activity": time.monotonic() - 100,
              "active": 0, "uncertain": False}
    record.update(changes)
    lease.receipt.write_text(json.dumps(record))


def test_observed_allowlist_needs_no_browser_guessing():
    for path in idle.POLL_GETS:
        assert idle.passive("GET", path + "?query=not-stored")
    assert idle.passive("GET", "/api/v1/projects/seeded/git/status")
    assert idle.passive("PUT", "/api/v1/preferences/browser_panel")
    assert idle.passive("POST", "/api/v1/model-providers/refresh-capabilities")
    for route in ("/api/v1/ws/chat", "/api/v1/ws/global", "/api/v1/ws/sessions/seeded"):
        assert idle.passive("GET", route, {"Upgrade": "websocket"})
    for protocol in ("vite-hmr", "vite-ping"):
        assert idle.passive("GET", "/", {"Upgrade": "websocket", "Sec-WebSocket-Protocol": protocol})
    assert idle.passive("GET", "/", {"Accept": "text/x-vite-ping"})
    for method, path in (("GET", "/"), ("POST", "/api/v1/sessions"),
                         ("PUT", "/api/v1/sessions/x"), ("GET", "/api/unknown")):
        assert not idle.passive(method, path, {"Sec-Fetch-Dest": "empty", "User-Agent": "Mozilla/5.0"})
    for path in ("/api/v1/voice/sessions/x/audio", "/api/v1/ws/terminals/x"):
        assert not idle.passive("GET", path, {"Upgrade": "websocket"})


def test_draining_turn_without_active_run_prevents_pause():
    # Execute the real busy-proof SQL, not a fixture which returns the desired answer.
    connection = sqlite3.connect(":memory:")
    connection.create_function("NOW", 0, lambda: 0)
    for ddl in ("runs (state TEXT, lease_expires_at INT)", "turns (state TEXT)",
                "background_tasks (status TEXT)", "git_ops (status TEXT)"):
        connection.execute("CREATE TABLE " + ddl)
    db = SimpleNamespace(query=lambda sql: "t" if connection.execute(sql).fetchone()[0] else "f")
    try:
        assert not idle.database_busy(db)
        for state in ("running", "interrupting", "draining"):
            connection.execute("DELETE FROM turns")
            connection.execute("INSERT INTO turns VALUES (?)", (state,))
            assert idle.database_busy(db)
        connection.execute("UPDATE turns SET state='completed'")
        assert not idle.database_busy(db)
    finally:
        connection.close()
    assert idle.database_busy(SimpleNamespace(query=Mock(side_effect=RuntimeError("offline"))))


@pytest.mark.parametrize("changes", [{"active": 1}, {"uncertain": True}, {"pid": -1},
                                     {"identity": "reused"}, {"updated": 0},
                                     {"activity": "unreadable"}, {"activity": float("nan")}])
def test_missing_stale_or_busy_receipts_never_pause(tmp_path, changes):
    lease = idle.IdleLease(instance(tmp_path), threading.Event(), seconds=0)
    assert not lease.idle()
    receipt(lease, **changes)
    assert not lease.idle()


def test_activity_fake_provider_and_journey_keep_awake(tmp_path):
    lease = idle.IdleLease(instance(tmp_path), threading.Event(), seconds=1)
    lease.last_activity -= 100
    receipt(lease)
    assert lease.idle()
    lease.instance.fake.log_path.write_text("request metadata only")
    assert not lease.idle()
    lease.last_activity -= 100
    receipt(lease)
    pin = tmp_path / "journey-test.lease"
    pin.write_text(f"{os.getpid()} {guardian.identity(os.getpid())}")
    assert not lease.idle()
    pin.write_text(f"{os.getpid()} different-start")
    assert lease.idle()
    pin.write_text("partial")
    assert not lease.idle()


def test_passive_requests_get_503_without_waking(tmp_path):
    lease = idle.IdleLease(instance(tmp_path), threading.Event())
    lease.transition("paused")
    holder = idle.PausedPort(lease, "127.0.0.1", 0)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", holder.server.server_port, timeout=2)
        connection.request("GET", "/api/v1/owner/status")
        response = connection.getresponse()
        assert response.status == 503 and response.read() == b""
        assert not lease.requested
        connection.close()
    finally:
        holder.close()


def test_held_post_redirects_then_resends_unchanged_to_direct_server(tmp_path):
    lease = idle.IdleLease(instance(tmp_path), threading.Event())
    lease.transition("paused")
    holder = idle.PausedPort(lease, "127.0.0.1", 0)
    port = holder.server.server_port
    result, received = [], []
    body = b"original POST body"

    def request():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("POST", "/work?original=1", body=body)
        response = connection.getresponse()
        result.append((response.status, response.getheader("Location"), response.read()))
        connection.close()

    client = threading.Thread(target=request)
    client.start()
    server = None
    try:
        for _ in range(100):
            if lease.requested:
                break
            time.sleep(0.005)
        assert lease.requested and not result
        lease.transition("waking")
        holder.close()  # Accepted connection survives; public port is released.

        class Direct(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Direct)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        lease.transition("ready")
        client.join(3)
        assert result == [(307, "/work?original=1", b"")] and not received
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("POST", result[0][1], body=body)
        assert connection.getresponse().status == 200
        connection.close()
        assert received == [("/work?original=1", body)]
    finally:
        lease.stopped.set()
        with lease.condition:
            lease.condition.notify_all()
        if server:
            server.shutdown()
            server.server_close()
        client.join(3)


def test_holder_denies_nonprivate_peer_without_wake(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.owner_auth.is_private_address", lambda peer: False)
    lease = idle.IdleLease(instance(tmp_path), threading.Event())
    holder = idle.PausedPort(lease, "127.0.0.1", 0)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", holder.server.server_port, timeout=2)
        connection.request("GET", "/")
        assert connection.getresponse().status == 403
        assert not lease.requested
        connection.close()
    finally:
        holder.close()


def bare_observer(app):
    obj = observer.ActivityObserver.__new__(observer.ActivityObserver)
    obj.app, obj.lock = app, threading.Lock()
    obj.active, obj.activity, obj.uncertain = 0, 0, False
    return obj


def test_observer_http_streaming_identity_and_inflight_pin():
    async def exercise():
        original_scope = {"type": "http", "path": "/unknown", "method": "POST", "headers": []}
        messages = [{"type": "http.request", "body": b"secret-not-retained", "more_body": False}]
        sent = []

        async def receive():
            return messages[0]

        async def send(message):
            sent.append(message)

        async def app(scope, receive_arg, send_arg):
            assert scope is original_scope and receive_arg is receive and send_arg is send
            assert wrapped.active == 1
            assert await receive_arg() is messages[0]
            for chunk in (b"data: first\n\n", b"data: last\n\n"):
                message = {"type": "http.response.body", "body": chunk, "more_body": True}
                await send_arg(message)
                assert sent[-1] is message  # No buffering or copies, including SSE.

        wrapped = bare_observer(app)
        await wrapped(original_scope, receive, send)
        assert wrapped.active == 0 and wrapped.activity > 0 and not wrapped.uncertain
        assert not hasattr(wrapped, "body")
    asyncio.run(exercise())




def test_passive_finite_http_pins_without_extending_idle_timer():
    async def exercise():
        async def app(scope, receive, send):
            assert wrapped.active == 1 and wrapped.activity == 0
        wrapped = bare_observer(app)
        await wrapped({"type": "http", "method": "GET", "path": "/api/v1/owner/status"}, None, None)
        assert wrapped.active == 0 and wrapped.activity == 0
    asyncio.run(exercise())


def test_observer_socket_heartbeats_only_are_passive_and_voice_pins():
    async def exercise(path, heartbeat, pin):
        message = {"type": "websocket.receive", "text": heartbeat}

        async def receive():
            return message

        async def send(_):
            pass

        async def app(scope, wrapped_receive, wrapped_send):
            assert wrapped.active == pin
            before = wrapped.activity
            assert await wrapped_receive() is message
            assert (wrapped.activity == before) == (heartbeat == '{"type":"ping"}')

        wrapped = bare_observer(app)
        await wrapped({"type": "websocket", "path": path, "headers": []}, receive, send)
        assert wrapped.active == 0
    for text in ('{"type":"ping"}', '{"type":"query","prompt":"not stored"}', 'unknown'):
        asyncio.run(exercise("/api/v1/ws/chat", text, 0))
    asyncio.run(exercise("/api/v1/voice/sessions/x/audio", '{"type":"ping"}', 1))


def test_settled_chat_subscription_is_unpinned_but_live_turn_is_busy(tmp_path):
    async def exercise(live):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            assert wrapped.active == 0 and wrapped.activity == 0
            entered.set()
            await finish.wait()  # The real feed stays open and emits keepalives.

        wrapped = bare_observer(app)
        task = asyncio.create_task(wrapped({"type": "http", "method": "GET",
            "path": "/api/v1/sessions/seeded/events", "headers": []}, None, None))
        await entered.wait()
        assert not task.done()
        lease = idle.IdleLease(instance(tmp_path), threading.Event(), seconds=0)
        receipt(lease, active=wrapped.active)
        database = sqlite3.connect(":memory:")
        database.create_function("NOW", 0, lambda: 0)
        for ddl in ("runs (state TEXT, lease_expires_at INT)", "turns (state TEXT)",
                    "background_tasks (status TEXT)", "git_ops (status TEXT)"):
            database.execute("CREATE TABLE " + ddl)
        if live:
            database.execute("INSERT INTO turns VALUES ('draining')")
        db = SimpleNamespace(query=lambda sql: "t" if database.execute(sql).fetchone()[0] else "f")
        try:
            assert lease.idle()  # An open feed alone does not forbid pausing.
            assert idle.database_busy(db) is live
            assert (lease.idle() and not idle.database_busy(db)) is not live
        finally:
            finish.set()
            await task
            database.close()
        assert wrapped.active == 0 and wrapped.activity == 0

    assert idle.passive("GET", "/api/v1/sessions/seeded/events?from_seq=45")
    assert not idle.passive("POST", "/api/v1/sessions/seeded/events")
    assert not idle.passive("GET", "/api/v1/sessions/seeded/unknown-stream")
    asyncio.run(exercise(False))
    asyncio.run(exercise(True))


def test_app_start_never_publishes_restarting_during_wake(tmp_path):
    obj = instance(tmp_path)
    obj.starts, obj.gate, obj.wake = 1, Mock(), threading.Event()
    obj.lock = threading.RLock()
    obj.data["state"] = "waking"
    obj.record_postgres = lambda: obj.write(postgres={"pid": os.getpid()})
    devloop.Instance.on_app_start(obj, SimpleNamespace(pid=os.getpid()))
    assert obj.states == ["waking", "waking"]
    assert obj.data["app"]["starts"] == 2 and obj.wake.is_set()


def test_child_reaches_app_main_as_main_without_changing_serve_settings(tmp_path, monkeypatch):
    from app import serve

    app, settings, calls = object(), object(), []

    def normal_run(wrapped, actual_settings):
        assert actual_settings is settings and wrapped.app is app
        wrapped.stopped.set()
        wrapped.writer.join(2)
        calls.append("normal serve")

    def module(name, **kwargs):
        assert name == "app.main" and kwargs == {"run_name": "__main__", "alter_sys": True}
        assert len(sys.argv) == 1
        serve.run(app, settings)

    monkeypatch.setattr(serve, "run", normal_run)
    monkeypatch.setattr(child.runpy, "run_module", module)
    monkeypatch.setattr(sys, "argv", ["child", str(tmp_path / "activity.json")])
    child.main()
    assert calls == ["normal serve"]


@pytest.mark.parametrize("failed_pause", [False, True])
def test_real_child_process_reaped_before_paused_ttl(tmp_path, monkeypatch, failed_pause):
    obj = instance(tmp_path)
    stopped = threading.Event()
    lease = idle.IdleLease(obj, stopped, seconds=0)
    real_popen = subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        children.append(process)
        return process

    def on_start(process):
        receipt(lease)
        obj.data["app"].update(pid=process.pid, pid_identity=guardian.identity(process.pid))
        record = json.loads(lease.receipt.read_text())
        record.update(pid=process.pid, identity=guardian.identity(process.pid))
        lease.receipt.write_text(json.dumps(record))
        obj.write(state="ready")

    obj.on_app_start = on_start
    postgres = Mock()
    db = SimpleNamespace(open=Mock(), publish_descriptor=Mock(), close=Mock(), process=postgres,
                         query=Mock(return_value="f"))
    monkeypatch.setattr(idle.subprocess, "Popen", spawn)
    stop_group = Mock()
    monkeypatch.setattr("docker.container_runtime.stop_group", stop_group)
    if failed_pause:
        postgres.wait.side_effect = subprocess.TimeoutExpired("postmaster", 30)
        with pytest.raises(subprocess.TimeoutExpired):
            idle.supervise(obj, {}, db, lease, threading.Event(), start_vite=Mock(), stop_vite=Mock())
        assert "pausing" in obj.states and "paused" not in obj.states
    else:
        idle.supervise(obj, {}, db, lease, threading.Event(), start_vite=Mock(), stop_vite=Mock())
        assert "pausing" in obj.states and "paused" in obj.states
    assert children[0].poll() == -idle.signal.SIGTERM
    stop_group.assert_not_called()  # Reaped app is cleared, including during TTL cleanup.
    assert stopped.is_set()
    db.close.assert_called_once()
    postgres.send_signal.assert_called_once_with(idle.signal.SIGINT)
    postgres.wait.assert_called_once_with(timeout=30)


@pytest.mark.parametrize("ci,workers,flag,expected", [
    (False, 1, False, False), (False, 1, True, True),
    (True, 1, False, True), (False, 2, False, True),
])
def test_ci_and_paired_workers_never_pause(monkeypatch, ci, workers, flag, expected):
    import argparse
    import ipaddress

    args = SimpleNamespace(host=ipaddress.ip_address("127.0.0.1"), browser_host=None,
                           port=None, vite_port=None, ttl_seconds=60,
                           fake_voice_host=ipaddress.ip_address("127.0.0.1"),
                           workers=workers, seed="default", ci=ci, no_vite=False, no_pause=flag)
    monkeypatch.setattr(devloop, "stamped_build_problem", lambda: None)
    devloop._validate(argparse.ArgumentParser(), args)
    assert args.no_pause is expected


@pytest.mark.parametrize("state", ["paused", "waking"])
def test_guardian_uses_only_the_latest_process_identities(tmp_path, monkeypatch, state):
    scratch = tmp_path / "ai-shuffle-preview-test"
    scratch.mkdir()
    status = tmp_path / "status.json"
    records = {} if state == "paused" else {
        "app": {"pid": 100, "pid_identity": "new-app"},
        "vite": {"pid": 101, "pid_identity": "new-vite"},
        "postgres": {"pid": 102, "pid_identity": "new-pg"}}
    status.write_text(json.dumps({"pid": 99, "pid_identity": "launcher", "state": state, **records}))
    live = {(100, "new-app"), (101, "new-vite"), (102, "new-pg")} if records else set()
    killed = []
    monkeypatch.setattr(guardian, "alive", lambda pid, start: (pid, start) in live)
    monkeypatch.setattr(guardian, "_kill_group", lambda pid: (killed.append(pid), live.difference_update({r for r in live if r[0] == pid})))
    guardian.cleanup(status, scratch, 99, "launcher")
    assert killed == ([100, 101, 102] if records else [])
    assert not status.exists()
