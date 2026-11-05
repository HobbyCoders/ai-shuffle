"""Idle decisions and the real resident HTTP ingress; no app/database fixtures."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.devloop import idle, guardian
from scripts.devloop import __main__ as devloop


def instance(tmp_path):
    states = []
    obj = SimpleNamespace(directory=tmp_path, data={"state": "starting", "app": {}},
                          args=SimpleNamespace(ttl_seconds=0.6))

    def write(**changes):
        obj.data.update(changes)
        states.append(obj.data["state"])

    obj.write = write
    obj.retire = lambda: write(state="stopping")
    obj.states = states
    obj.record_postgres = Mock()
    obj.on_app_start = lambda process: write(state="ready")
    obj.on_app_exit = Mock()
    return obj


def test_polling_reconnects_and_vite_pings_do_not_count_as_work():
    browser = {"Sec-Fetch-Dest": "empty"}
    assert not idle.foreground("GET", "/api/v1/network/status", browser)
    assert not idle.foreground("GET", "/api/v1/sessions/x/events", browser)
    assert not idle.foreground("GET", "/", {"Accept": "text/x-vite-ping"})
    for route in ("/api/v1/ws/chat", "/api/v1/ws/global", "/api/v1/ws/sessions/x"):
        assert not idle.foreground("GET", route, {"Upgrade": "websocket"})
    assert not idle.foreground("GET", "/?token=hmr", {
        "Upgrade": "websocket", "Sec-WebSocket-Protocol": "vite-hmr"})
    assert idle.foreground("GET", "/", {"Sec-Fetch-Dest": "document"})
    assert idle.foreground("GET", "/api/v1/projects", {})  # Unknown clients stay safe.
    assert idle.foreground("POST", "/api/v1/sessions", browser)
    for path in ("/api/v1/voice/sessions/x/audio", "/api/v1/ws/terminals/x"):
        assert idle.foreground("GET", path, {"Upgrade": "websocket"})
    assert b"setInterval" not in idle.ACTIVITY_SCRIPT
    assert b"pointerdown" in idle.ACTIVITY_SCRIPT


def test_http_browser_polling_without_fetch_metadata_is_passive():
    # Observed on Chromium against the private plain-HTTP, non-loopback preview:
    # Fetch Metadata is absent, unlike localhost or an HTTPS page.
    browser = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    for path in ("/api/v1/auth/owner-status", "/api/v1/git/status", "/api/v1/computer-use/status"):
        assert not idle.foreground("GET", path, browser)
    browser["Accept"] = "*/*"
    assert not idle.foreground("GET", "/api/v1/sessions", browser)
    assert idle.foreground("POST", "/api/v1/sessions", browser)
    assert not idle.foreground("PUT", "/api/v1/preferences/browser_panel", browser)
    assert not idle.foreground("PUT", "/api/v1/preferences/chat_task_checks?only_if_missing=true", browser)
    assert idle.foreground("PUT", "/api/v1/sessions/x", browser)
    assert idle.foreground("PUT", "/api/v1/preferences/browser_panel", {})
    assert idle.foreground("POST", "/__devloop/activity", browser)
    browser["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    assert idle.foreground("GET", "/api/v1/sessions", browser)
    assert idle.foreground("GET", "/api/v1/sessions", {"Accept": "application/json"})
    assert idle.foreground("GET", "/api/v1/sessions", {"User-Agent": "Mozilla/5.0"})


def test_busy_and_uncertain_database_reads_prevent_pause():
    db = SimpleNamespace(query=Mock(return_value="f"))
    assert not idle.database_busy(db)
    sql = db.query.call_args.args[0]
    assert "lease_expires_at >= NOW()" in sql
    assert "background_tasks" in sql and "git_ops" in sql
    for answer in ("t", "", "unknown"):
        db.query.return_value = answer
        assert idle.database_busy(db)
    db.query.side_effect = RuntimeError("offline")
    assert idle.database_busy(db)


def test_idle_requires_no_inflight_work_and_a_deadline(tmp_path):
    lease = idle.IdleLease(instance(tmp_path), threading.Event(), seconds=0.01)
    lease.last_activity -= 1
    assert lease.idle()
    lease.active = 1
    assert not lease.idle()
    lease.active = 0
    lease.activity()
    assert not lease.idle()


def test_journeys_hold_the_whole_run_with_reuse_safe_identity(tmp_path):
    receipt = tmp_path / "journey-test.lease"
    receipt.write_text(f"{os.getpid()} {guardian.identity(os.getpid())}")
    assert idle.journey_active(tmp_path)
    receipt.write_text(f"{os.getpid()} different-start")
    assert not idle.journey_active(tmp_path)
    receipt.write_text("partial")
    assert idle.journey_active(tmp_path)  # Unsure: do not stop work.


@pytest.mark.parametrize("meaningful", [True, False])
def test_paused_request_is_held_then_served_once(tmp_path, meaningful):
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"saved")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lease = idle.IdleLease(instance(tmp_path), threading.Event())
    lease.transition("paused")
    ingress = idle.Ingress(lease, "127.0.0.1", 0, server.server_port)
    result = []

    def request():
        connection = http.client.HTTPConnection("127.0.0.1", ingress.server.server_port, timeout=3)
        connection.request("GET", "/api/v1/saved?original=1", headers={} if meaningful else {"Sec-Fetch-Dest": "empty"})
        result.append(connection.getresponse().read())
        connection.close()

    client = threading.Thread(target=request)
    client.start()
    try:
        time.sleep(0.1)
        assert not received and client.is_alive()
        assert lease.requested is meaningful
        lease.transition("waking")
        time.sleep(0.05)
        assert not received
        lease.transition("ready")
        client.join(3)
        assert result == [b"saved"]
        assert received == ["/api/v1/saved?original=1"]
        assert lease.active == 0
    finally:
        lease.stopped.set()
        with lease.condition:
            lease.condition.notify_all()
        ingress.close()
        server.shutdown()
        server.server_close()
        client.join(3)




def test_idle_websocket_keeps_browser_and_buffers_a_real_query_through_wake(tmp_path):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    async def exercise():
        received = []

        async def echo(socket):
            try:
                async for message in socket:
                    received.append(message)
                    await socket.send(message)
            except Exception:
                pass  # The upstream is deliberately stopped by the pause.

        lease = idle.IdleLease(instance(tmp_path), threading.Event())
        lease.transition("ready")
        async with serve(echo, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ingress = idle.Ingress(lease, "127.0.0.1", 0, port)
            try:
                async with connect(f"ws://127.0.0.1:{ingress.server.server_port}/api/v1/ws/chat",
                                   ping_interval=None) as client:
                    watch = '{"type":"watch_session","session_id":"seeded"}'
                    await client.send(watch)
                    assert await asyncio.wait_for(client.recv(), 3) == watch
                    lease.transition("pausing")
                    lease.disconnect_streams()
                    await asyncio.sleep(0.1)
                    lease.transition("paused")
                    lease.requested = False
                    await client.send('{"type":"pong"}')
                    await asyncio.sleep(0.1)
                    assert not lease.requested
                    query = '{"type":"query","prompt":"original","session_id":"seeded"}'
                    await client.send(query)
                    for _ in range(100):
                        if lease.requested:
                            break
                        await asyncio.sleep(0.01)
                    assert lease.requested
                    assert query not in received
                    lease.transition("waking")
                    await asyncio.sleep(0.05)
                    assert query not in received
                    lease.transition("ready")
                    assert await asyncio.wait_for(client.recv(), 3) == watch  # replayed subscription
                    assert await asyncio.wait_for(client.recv(), 3) == query
                    assert received.count(query) == 1
                    assert received.count(watch) == 2
            finally:
                lease.stopped.set()
                lease.disconnect_streams()
                ingress.close()

    asyncio.run(exercise())


def test_websocket_parser_handles_partial_and_extended_masked_frames():
    payload = b'{"type":"query","prompt":"' + b'x' * 200 + b'"}'
    mask = b'abcd'
    frame = bytes([129, 128 | 126]) + len(payload).to_bytes(2, "big") + mask
    frame += bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    assert idle.websocket_frames(frame[:5]) == ([], frame[:5])
    frames, rest = idle.websocket_frames(frame)
    assert not rest and frames == [(frame, 1, payload)]
    assert idle.websocket_work(1, payload)
    assert not idle.websocket_work(1, b'{"type":"pong"}')
    assert idle.websocket_work(2, b'unknown')


def test_ttl_ends_while_paused_and_shutdown_is_clean(tmp_path, monkeypatch):
    obj = instance(tmp_path)
    stopped = threading.Event()
    lease = idle.IdleLease(obj, stopped, seconds=0)
    process = SimpleNamespace(returncode=None)
    process.poll = lambda: process.returncode
    process.send_signal = Mock(side_effect=lambda _: setattr(process, "returncode", 0))
    postgres = Mock()
    db = SimpleNamespace(open=Mock(), publish_descriptor=Mock(), close=Mock(),
                         process=postgres, query=Mock(return_value="f"))
    monkeypatch.setattr(idle.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr("docker.container_runtime.stop_group", Mock())
    idle.supervise(obj, {}, db, lease, threading.Event(), start_vite=Mock(), stop_vite=Mock())
    assert "pausing" in obj.states and "paused" in obj.states
    assert stopped.is_set()
    process.send_signal.assert_called_once_with(idle.signal.SIGTERM)
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


def test_postgres_identity_is_replaced_on_every_start(tmp_path, monkeypatch):
    import ipaddress

    args = SimpleNamespace(host=ipaddress.ip_address("127.0.0.1"), browser_host=None,
                           ttl_seconds=60, status=tmp_path / "private" / "status.json")
    fake = SimpleNamespace(url="http://127.0.0.1:9", log_path=tmp_path / "log",
                           scenarios=SimpleNamespace(directory=tmp_path, load=dict))
    env = {"SHUFFLE_PREVIEW_PORT": "19990", "PORT": "19991", "SHUFFLE_PROTECTED_PORT": "19992",
           "DATA_DIR": str(tmp_path / "data"), "WORKSPACE_DIR": str(tmp_path / "workspace")}
    obj = devloop.Instance(args, tmp_path, env, fake, None)
    monkeypatch.setattr("app.core.temp_scratch.process_identity", lambda pid: f"identity-{pid}")
    obj.database = SimpleNamespace(process=SimpleNamespace(pid=100), socket=tmp_path / "first")
    obj.record_postgres()
    obj.database.process.pid = 200
    obj.database.socket = tmp_path / "second"
    obj.record_postgres()
    status = json.loads(args.status.read_text())
    assert status["postgres"] == {"pid": 200, "pid_identity": "identity-200",
                                  "socket_dir": str(tmp_path / "second")}


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
