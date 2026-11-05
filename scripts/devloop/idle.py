"""Resident ingress and an idle lease for private devloop instances (stdlib only).

Ports belong to this launcher, not to the disposable children. Idle browser
fetches/reconnects wait here while paused; navigation, input, mutations and
explicit wake requests start the children before forwarding the original request.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import select
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
# Only actual browser input sends this. There is deliberately no timer/heartbeat.
ACTIVITY_SCRIPT = b"""<script>(() => {
let last = 0;
const activity = () => {
  if (Date.now() - last < 1000) return;
  last = Date.now();
  fetch('/__devloop/activity', {method:'POST', keepalive:true}).catch(() => {});
};
for (const name of ['pointerdown','keydown','wheel','touchstart'])
  window.addEventListener(name, activity, {capture:true, passive:true});
})();</script>"""
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def foreground(method: str, path: str, headers) -> bool:
    """Browser GET polling and WS reconnects are not evidence of human work.

    Browser actions are separately signalled by the injected input listener.
    Unknown clients/routes fail safe: count them as work. Vite's reconnect ping
    has no fetch-dest on some Chromium versions, so recognise its exact Accept.
    """
    if headers.get("Accept") == "text/x-vite-ping":
        return False
    if headers.get("Upgrade", "").lower() == "websocket":
        route = urlsplit(path).path
        passive = route in ("/api/v1/ws/chat", "/api/v1/ws/global") or route.startswith("/api/v1/ws/sessions/")
        return not passive and "vite-hmr" not in headers.get("Sec-WebSocket-Protocol", "")
    if method not in ("GET", "HEAD", "OPTIONS"):
        return True
    return not (path.startswith("/api/") and headers.get("Sec-Fetch-Dest") == "empty")


class IdleLease:
    def __init__(self, instance, stopped: threading.Event, *, seconds: float = IDLE_SECONDS):
        self.instance, self.stopped, self.seconds = instance, stopped, seconds
        self.condition = threading.Condition()
        self.state = "starting"
        self.last_activity = time.monotonic()
        self.active = 0
        self.requested = False
        self.connections: set[socket.socket] = set()

    def activity(self):
        with self.condition:
            self.last_activity = time.monotonic()
            self.requested = True
            self.condition.notify_all()

    def enter(self, meaningful: bool, connection=None) -> bool:
        with self.condition:
            if meaningful:
                self.activity()
            while self.state != "ready" and not self.stopped.is_set():
                if connection is not None and select.select([connection], [], [], 0)[0]:
                    # A browser may abort a polling fetch while idle. Do not
                    # retain a thread for every abandoned retry until the TTL.
                    if not connection.recv(1, socket.MSG_PEEK):
                        return False
                self.condition.wait(0.2)
            if self.stopped.is_set():
                return False
            # Even a passive finite request must finish before stopping the app.
            self.active += 1
            return True

    def leave(self):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def transition(self, state):
        with self.condition:
            self.state = state
            self.instance.write(state=state)
            self.condition.notify_all()

    def idle(self) -> bool:
        return (self.active == 0 and not journey_active(self.instance.directory)
                and time.monotonic() - self.last_activity >= self.seconds)

    def disconnect_streams(self):
        with self.condition:
            for connection in tuple(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def journey_active(directory: Path) -> bool:
    """A journey pins the lease with its PID/start time, even during long waits.

    A crashed runner cannot pin it forever. Lease files live only in this
    launcher's private scratch directory, not the checkout or shared database.
    """
    from scripts.devloop.guardian import alive

    for receipt in directory.glob("journey-*.lease"):
        try:
            pid, identity = receipt.read_text().split()
            if alive(int(pid), identity):
                return True
        except (OSError, ValueError):
            return True  # An incomplete receipt is uncertain, not idle.
    return False


class SourceActivity(threading.Thread):
    """Vite reloads frontend source edits; the resident launcher observes them too."""

    def __init__(self, lease, *, poll=False):
        super().__init__(name="devloop-source-activity", daemon=True)
        self.lease, self.poll = lease, poll

    def run(self):
        from scripts.devloop.__main__ import FRONTEND
        from watchfiles import watch

        for _ in watch(FRONTEND / "src", stop_event=self.lease.stopped,
                       debounce=500, raise_interrupt=False, force_polling=self.poll or None):
            self.lease.activity()


def database_busy(database) -> bool:
    """Read the producing path's durable run/task truth, never a UI spinner.

    This is the launcher's OWN database handle, not a configured live database.
    Failure or an unfamiliar schema keeps the instance awake. A stale running
    task is intentionally conservative. No app endpoint or environment knob.
    """
    try:
        result = database.query("""SELECT
            EXISTS (SELECT 1 FROM runs WHERE state = 'active' AND lease_expires_at >= NOW())
            OR EXISTS (SELECT 1 FROM background_tasks
                       WHERE status NOT IN ('completed', 'error', 'killed'))
            OR EXISTS (SELECT 1 FROM git_ops WHERE status IN ('queued', 'running'))""")
        return result != "f"
    except Exception:
        log.warning("could not prove the private instance idle; keeping it awake")
        return True


def websocket_frames(buffer: bytes):
    """Split RFC6455 frames without changing their bytes (including masks)."""
    frames = []
    while len(buffer) >= 2:
        size, offset = buffer[1] & 127, 2
        if size in (126, 127):
            width = 2 if size == 126 else 8
            if len(buffer) < offset + width:
                break
            size = int.from_bytes(buffer[offset:offset + width], "big")
            offset += width
        masked = bool(buffer[1] & 128)
        payload_at = offset + (4 if masked else 0)
        end = payload_at + size
        if len(buffer) < end:
            break
        payload = buffer[payload_at:end]
        if masked:
            mask = buffer[offset:offset + 4]
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        frames.append((buffer[:end], buffer[0] & 15, payload))
        buffer = buffer[end:]
    return frames, buffer


def websocket_work(opcode, payload):
    """Only known keepalives/subscriptions are passive; unknown messages pin work."""
    if opcode in (8, 9, 10):
        return False
    try:
        return json.loads(payload).get("type") not in (
            "pong", "ping", "watch_session", "unwatch_session", "load_session")
    except (ValueError, AttributeError, UnicodeError):
        return True


class Ingress:
    def __init__(self, lease: IdleLease, host: str, port: int, upstream: int):
        self.lease, self.upstream = lease, upstream
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass  # Access tokens must not appear in logs.

            def handle_request(self):
                if self.path in ("/__devloop/activity", "/__devloop/wake") and self.command == "POST":
                    owner.lease.activity()
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                meaningful = foreground(self.command, self.path, self.headers)
                if not owner.lease.enter(meaningful, self.connection):
                    self.send_error(503, "The dev instance lease ended")
                    return
                counted = True
                conn = None
                try:
                    if self.headers.get("Upgrade", "").lower() == "websocket":
                        # An idle chat/HMR connection is detachable; a voice or
                        # unknown WebSocket pins the lease for its whole lifetime.
                        if not meaningful:
                            owner.lease.leave()
                            counted = False
                        self.tunnel()
                        return
                    conn = http.client.HTTPConnection("127.0.0.1", owner.upstream, timeout=120)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
                    headers["Accept-Encoding"] = "identity"  # HTML receives the input listener.
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    conn.request(self.command, self.path, body=body or None, headers=headers)
                    response = conn.getresponse()
                    stream = "text/event-stream" in response.getheader("Content-Type", "")
                    html = "text/html" in response.getheader("Content-Type", "") and self.command != "HEAD"
                    payload = response.read() if html else None
                    if payload is not None:
                        payload = payload.replace(b"</head>", ACTIVITY_SCRIPT + b"</head>", 1)
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in HOP_HEADERS | ({"content-length", "etag"} if html else set()):
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    if payload is not None:
                        self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.close_connection = True
                    if stream:
                        owner.lease.leave()
                        counted = False
                    upstream_socket = conn.sock
                    if upstream_socket:
                        with owner.lease.condition:
                            owner.lease.connections.add(upstream_socket)
                    try:
                        if payload is not None:
                            self.wfile.write(payload)
                        else:
                            while data := response.read1(65536):
                                self.wfile.write(data)
                                self.wfile.flush()
                    finally:
                        with owner.lease.condition:
                            owner.lease.connections.discard(upstream_socket)
                except (OSError, http.client.HTTPException):
                    self.close_connection = True
                finally:
                    if conn:
                        conn.close()
                    if counted:
                        owner.lease.leave()

            def tunnel(self):
                lease = owner.lease
                passive = not foreground(self.command, self.path, self.headers)
                upstream = None
                subscriptions = {}
                pending = b""
                initial = True
                last_ping = time.monotonic()

                def connect():
                    nonlocal upstream, initial
                    upstream = socket.create_connection(("127.0.0.1", owner.upstream), timeout=30)
                    request = f"{self.command} {self.path} HTTP/1.1\r\n"
                    # No compression: passive-frame classification must inspect
                    # the actual JSON, not compressed wire bytes.
                    request += "".join(f"{key}: {value}\r\n" for key, value in self.headers.items()
                                       if key.lower() != "sec-websocket-extensions")
                    upstream.sendall((request + "\r\n").encode("latin1"))
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        byte = upstream.recv(1)
                        if not byte or len(header) > 65536:
                            raise OSError("WebSocket upgrade did not complete")
                        header += byte
                    if initial:
                        self.connection.sendall(header)
                        initial = False
                    if b" 101 " not in header.split(b"\r\n", 1)[0]:
                        raise OSError("WebSocket upgrade refused")
                    with lease.condition:
                        lease.connections.add(upstream)
                    for frame in subscriptions.values():
                        upstream.sendall(frame)

                def detach():
                    nonlocal upstream
                    if upstream is not None:
                        with lease.condition:
                            lease.connections.discard(upstream)
                        upstream.close()
                        upstream = None

                try:
                    connect()
                    while not lease.stopped.is_set():
                        # Keep the browser-side HMR/chat connection, not the
                        # backend process. Pongs never wake; real query frames
                        # wait for readiness and are forwarded exactly once.
                        if passive and lease.state in ("pausing", "paused", "waking"):
                            detach()
                        if upstream is None and lease.state == "ready":
                            connect()
                        sockets = [self.connection] + ([upstream] if upstream else [])
                        ready, _, _ = select.select(sockets, [], [], 0.5)
                        if upstream is None and time.monotonic() - last_ping >= 15:
                            # Browser's chat health timeout otherwise reconnects
                            # forever. This contains no app state and no work.
                            payload = b'{"type":"ping"}'
                            if self.path.startswith("/api/"):
                                self.connection.sendall(bytes([129, len(payload)]) + payload)
                            last_ping = time.monotonic()
                        for source in ready:
                            data = source.recv(65536)
                            if source is upstream:
                                if not data:
                                    if passive and lease.state != "ready":
                                        detach()
                                        continue
                                    return
                                if lease.state == "ready" or not passive:
                                    self.connection.sendall(data)
                                continue
                            if not data:
                                return
                            if not passive:
                                upstream.sendall(data)
                                continue
                            pending += data
                            frames, pending = websocket_frames(pending)
                            if pending:
                                lease.activity()  # fragmented/unknown work: stay safe
                            for raw, opcode, payload in frames:
                                if opcode == 8:
                                    return
                                work = websocket_work(opcode, payload)
                                if work:
                                    if not lease.enter(True):
                                        return
                                    try:
                                        if upstream is None:
                                            connect()
                                        upstream.sendall(raw)
                                    finally:
                                        lease.leave()
                                elif upstream is not None and lease.state == "ready":
                                    upstream.sendall(raw)
                                if opcode == 1 and not work:
                                    message = json.loads(payload)
                                    if message.get("type") in ("watch_session", "unwatch_session", "load_session"):
                                        key = (("load_session", None) if message["type"] == "load_session"
                                               else ("watch", message.get("session_id")))
                                        subscriptions[key] = raw
                finally:
                    detach()
                    self.close_connection = True

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="devloop-ingress")
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def supervise(instance, env, database, lease, restart, *, start_vite, stop_vite):
    """One serial lifecycle owner: never open a second cluster during wake.

    Pausing uses only clean shutdown. An app that cannot shut down does not get
    called paused and never has its database stopped underneath it. Teardown's
    existing guardian remains the final backstop for a killed launcher.
    """
    from app.core.temp_scratch import process_identity
    from docker.container_runtime import stop_group

    stopped = lease.stopped
    owner = os.getppid()
    owner_start = process_identity(owner)
    deadline = time.monotonic() + instance.args.ttl_seconds
    process = None
    db_open = False

    def over():
        return stopped.is_set() or time.monotonic() >= deadline or process_identity(owner) != owner_start

    def start():
        nonlocal process, db_open
        database.open()
        db_open = True
        database.publish_descriptor()
        instance.record_postgres()
        if over():
            return
        start_vite()
        process = subprocess.Popen([os.sys.executable, "-m", "app.main"], env=env, start_new_session=True)
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
                if lease.requested or restart.is_set():
                    lease.requested = False
                    restart.clear()
                    lease.transition("waking")
                    start()
            elif restart.is_set():
                lease.activity()
                lease.transition("restarting")
                lease.disconnect_streams()
                stop_group(process, 4)
                restart.clear()
                process = subprocess.Popen([os.sys.executable, "-m", "app.main"], env=env, start_new_session=True)
                instance.on_app_start(process)
            elif process is not None and process.poll() is not None and lease.state != "app-exited":
                lease.transition("app-exited")
                instance.on_app_exit(process)
            elif lease.state == "ready":
                # No request can slip between idle proof and the pause commit.
                with lease.condition:
                    if lease.idle():
                        if database_busy(database):
                            # Active/uncertain work starts a new idle period;
                            # don't spawn psql five times a second during a turn.
                            lease.last_activity = time.monotonic()
                        else:
                            lease.requested = False
                            lease.transition("pausing")
                if lease.state == "pausing":
                    lease.disconnect_streams()
                    process.send_signal(signal.SIGTERM)
                    # Do not SIGKILL an idle-pause. The lease/guardian still
                    # handles TTL, stop, parent loss and exceptional teardown.
                    while process.poll() is None and not over():
                        stopped.wait(0.2)
                    if over():
                        break
                    stop_vite(clean=True)
                    # Demand fast clean PostgreSQL shutdown BEFORE close()'s
                    # generic teardown backstop can kill any live process.
                    database.process.send_signal(signal.SIGINT)
                    database.process.wait(timeout=30)
                    database.close()
                    database.process = None
                    db_open = False
                    instance.write(app={**instance.data["app"], "pid": None, "pid_identity": None},
                                   postgres=None, vite=None)
                    lease.transition("paused")
                    log.info("idle: app, Vite and PostgreSQL paused; request or `devloop wake` resumes them")
            stopped.wait(0.2)
        return 0
    finally:
        instance.retire()
        stopped.set()
        with lease.condition:
            lease.condition.notify_all()
        lease.disconnect_streams()
        stop_vite()
        try:
            stop_group(process, 4)
        finally:
            # open() can fail after spawning the postmaster, so close even then.
            if db_open or database.process is not None:
                database.close()
