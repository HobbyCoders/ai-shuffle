"""Pause-only port holders and lifecycle for disposable dev instances.

Awake traffic goes directly to the normal backend and Vite. A holder exists
only while paused: passive reconnects get 503, real work gets a same-URL 307
after all upstreams are ready. It never forwards headers, bodies or frames.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("devloop")
IDLE_SECONDS = 300
RECEIPT_MAX_AGE = 5
# Observed with an untouched seeded tab, including the one-minute full refresh.
# Unknown method/path pairs are work, regardless of browser or Fetch Metadata.
POLL_GETS = frozenset({
    "/api/preview", "/api/v1/auth/status", "/api/v1/owner/status",
    "/api/v1/computer-use/status", "/api/v1/preferences/all",
    "/api/v1/projects", "/api/v1/cluster/projects", "/api/v1/agents",
    "/api/v1/tags", "/api/v1/models", "/api/v1/sessions/cursor",
    "/api/v1/notifications",
})


def passive(method: str, path: str, headers=None) -> bool:
    route = urlsplit(path).path
    headers = headers or {}
    if headers.get("Upgrade", "").lower() == "websocket":
        return (route in ("/api/v1/ws/chat", "/api/v1/ws/global")
                or bool(re.fullmatch(r"/api/v1/ws/sessions/[^/]+", route))
                or headers.get("Sec-WebSocket-Protocol") in ("vite-hmr", "vite-ping"))
    if method == "GET":
        return (route in POLL_GETS or bool(re.fullmatch(r"/api/v1/projects/[^/]+/git/status", route))
                or (route == "/" and headers.get("Accept") == "text/x-vite-ping"))
    return method == "PUT" and route.startswith("/api/v1/preferences/")


def journey_active(directory: Path) -> bool:
    from scripts.devloop.guardian import alive

    for receipt in directory.glob("journey-*.lease"):
        try:
            pid, identity = receipt.read_text().split()
            if alive(int(pid), identity):
                return True
        except (OSError, ValueError):
            return True
    return False


def database_busy(database) -> bool:
    """The launcher's own database, including draining turns after run release."""
    try:
        return database.query("""SELECT
            EXISTS (SELECT 1 FROM runs WHERE state = 'active' AND lease_expires_at >= NOW())
            OR EXISTS (SELECT 1 FROM turns WHERE state IN ('running', 'interrupting', 'draining'))
            OR EXISTS (SELECT 1 FROM background_tasks
                       WHERE status NOT IN ('completed', 'error', 'killed'))
            OR EXISTS (SELECT 1 FROM git_ops WHERE status IN ('queued', 'running'))""") != "f"
    except Exception:
        log.warning("could not prove the private instance idle; keeping it awake")
        return True


class IdleLease:
    def __init__(self, instance, stopped, *, seconds=IDLE_SECONDS):
        self.instance, self.stopped, self.seconds = instance, stopped, seconds
        self.condition = threading.Condition()
        self.state = "starting"
        self.last_activity = time.monotonic()
        self.requested = False
        self.receipt = instance.directory / "activity.json"
        self.wake_file = instance.directory / "wake.request"
        self.fake_mtime = None

    def activity(self):
        with self.condition:
            self.last_activity = time.monotonic()
            self.requested = True
            self.condition.notify_all()

    def transition(self, state):
        with self.condition:
            self.state = state
            self.instance.write(state=state)
            self.condition.notify_all()

    def idle(self):
        if journey_active(self.instance.directory):
            return False
        if self.wake_file.exists():
            self.wake_file.unlink()
            self.activity()
            return False
        try:
            record = json.loads(self.receipt.read_text())
            app = self.instance.data["app"]
            now = time.monotonic()
            if not all(isinstance(record[name], (int, float)) and math.isfinite(record[name])
                       for name in ("updated", "activity")):
                return False
            if (record["pid"] != app["pid"] or record["identity"] != app["pid_identity"]
                    or not 0 <= now - record["updated"] <= RECEIPT_MAX_AGE
                    or record["uncertain"] or record["active"] != 0):
                return False
            self.last_activity = max(self.last_activity, record["activity"])
            # Real fake-provider requests are work too, including delayed replies.
            mtime = self.instance.fake.log_path.stat().st_mtime_ns if self.instance.fake.log_path.exists() else 0
            if self.fake_mtime is not None and mtime != self.fake_mtime:
                self.last_activity = now
            self.fake_mtime = mtime
            return now - self.last_activity >= self.seconds
        except (OSError, ValueError, KeyError, TypeError):
            return False  # Missing, unreadable or unfamiliar evidence is busy.


class SourceActivity(threading.Thread):
    def __init__(self, lease, *, poll=False):
        super().__init__(name="devloop-source-activity", daemon=True)
        self.lease, self.poll = lease, poll

    def run(self):
        from scripts.devloop.__main__ import FRONTEND
        from watchfiles import watch

        for _ in watch(FRONTEND / "src", stop_event=self.lease.stopped,
                       debounce=500, raise_interrupt=False, force_polling=self.poll or None):
            self.lease.activity()


class PausedPort:
    """Hold accepted requests, never proxy; closing the listener leaves them alive."""
    def __init__(self, lease, host, port):
        owner = self
        self.lease = lease

        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
            daemon_threads = True
            block_on_close = False

            def server_bind(self):
                if self.address_family == socket.AF_INET6:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 10  # Bound incomplete heads; never read or retain a body.

            def log_message(self, *_):
                pass  # Access links, query strings and bodies never enter logs.

            def respond(self, status, *, redirect=False):
                self.send_response(status)
                if redirect:
                    # A relative redirect keeps the exact target, never Host.
                    self.send_header("Location", self.path)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.close_connection = True

            def handle_request(self):
                from app.core.owner_auth import is_private_address

                if not is_private_address(ipaddress.ip_address(self.client_address[0])):
                    self.respond(403)
                    return
                if not self.path.startswith("/") or self.path.startswith("//"):
                    self.respond(400)
                    return
                if passive(self.command, self.path, self.headers):
                    self.respond(503)
                    return
                owner.lease.activity()
                with owner.lease.condition:
                    while owner.lease.state != "ready" and not owner.lease.stopped.is_set():
                        owner.lease.condition.wait(0.5)
                    self.respond(503 if owner.lease.stopped.is_set() else 307,
                                 redirect=not owner.lease.stopped.is_set())

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request

        self.server = Server((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True, name="devloop-paused-port")
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def supervise(instance, env, database, lease, restart, *, start_vite, stop_vite):
    from app.core.temp_scratch import process_identity
    from docker.container_runtime import stop_group

    stopped = lease.stopped
    owner, owner_start = os.getppid(), process_identity(os.getppid())
    deadline = time.monotonic() + instance.args.ttl_seconds
    process = None
    process_start = None
    holders = []
    db_open = False

    def over():
        return stopped.is_set() or time.monotonic() >= deadline or process_identity(owner) != owner_start

    def stop_app(*, clean=False):
        nonlocal process
        if process is None:
            return
        if process.poll() is None and process_identity(process.pid) == process_start:
            if clean:
                process.send_signal(signal.SIGTERM)
                while process.poll() is None and not over():
                    stopped.wait(0.2)
            else:
                stop_group(process, 4)
        if process.poll() is not None:
            process = None  # Reaped; never signal this possibly reused PGID again.

    def start():
        nonlocal process, process_start, db_open
        for holder in holders:
            holder.close()
        holders.clear()
        database.open()
        db_open = True
        database.publish_descriptor()
        instance.record_postgres()
        if over():
            return
        start_vite()
        process = subprocess.Popen([os.sys.executable, "-m", "scripts.devloop.child", str(lease.receipt)],
                                   env=env, start_new_session=True)
        process_start = process_identity(process.pid)
        instance.on_app_start(process)
        if lease.state == "waking":
            instance.write(state="waking")

    try:
        start()
        while not over():
            if lease.state in ("starting", "waking", "restarting") and instance.data["state"] == "ready":
                lease.transition("ready")
                lease.last_activity = time.monotonic()
            if lease.state == "paused":
                if lease.requested or restart.is_set() or lease.wake_file.exists():
                    lease.wake_file.unlink(missing_ok=True)
                    lease.requested = False
                    restart.clear()
                    lease.transition("waking")
                    start()
            elif restart.is_set():
                lease.activity()
                lease.transition("restarting")
                stop_app()
                restart.clear()
                process = subprocess.Popen([os.sys.executable, "-m", "scripts.devloop.child", str(lease.receipt)],
                                           env=env, start_new_session=True)
                process_start = process_identity(process.pid)
                instance.on_app_start(process)
            elif process is not None and process.poll() is not None and lease.state != "app-exited":
                lease.transition("app-exited")
                instance.on_app_exit(process)
                process = None
            elif lease.state == "ready" and lease.idle():
                if database_busy(database):
                    lease.last_activity = time.monotonic()
                else:
                    lease.requested = False
                    lease.transition("pausing")
                    stop_app(clean=True)
                    if over():
                        break
                    stop_vite(clean=True)
                    database.process.send_signal(signal.SIGINT)  # PostgreSQL fast clean shutdown.
                    database.process.wait(timeout=30)
                    database.close()
                    database.process = None
                    db_open = False
                    instance.write(app={**instance.data["app"], "pid": None, "pid_identity": None},
                                   postgres=None, vite=None)
                    for port in (instance.port, instance.vite_port):
                        if port:
                            holders.append(PausedPort(lease, str(instance.args.host), port))
                    lease.transition("paused")
                    log.info("idle: app, Vite and PostgreSQL paused; reload or `devloop wake` resumes them")
            stopped.wait(0.2)
        return 0
    finally:
        instance.retire()
        stopped.set()
        with lease.condition:
            lease.condition.notify_all()
        for holder in holders:
            holder.close()
        stop_vite()
        try:
            stop_app()
        finally:
            if db_open or database.process is not None:
                database.close()
