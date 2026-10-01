"""Process-level boot smoke test: the real server, started the way production starts it.

TestClient suites import ``app.main`` in-process with the database shimmed, so
they cannot notice that the server does not start: an import-order crash in
``python -m app.main``, a lifespan phase that raises, a startup step that waits
on the network, a schema migration that fails on real PostgreSQL, a shutdown
that hangs or a traceback on the console. This module catches those.

What it does, in order:

1. Initializes a private PostgreSQL 16 cluster with the container supervisor's
   own ``Database`` class (``docker/container_runtime.py``) on a free port in
   a temp data dir, and publishes ``managed-postgres.json`` exactly as the
   supervisor does before it starts the app.
2. Starts ``python -m app.main`` (the supervisor's command; ``app.serve`` binds
   the local and protected listeners) with an isolated HOME/DATA_DIR/
   WORKSPACE_DIR, an empty env file, no provider keys, no Chromium, and every
   HTTP(S) proxy variable pointed at a local recorder that refuses all
   outbound traffic, so the run stays offline and outbound attempts are seen.
3. Waits for ``/health/ready``, then exercises the unauthenticated surface,
   the fresh-install owner bootstrap (create the owner password from the
   local listener, then sign in for a Bearer token), and owner routes
   including a session created with the default profile and the chat
   WebSocket's initial frames. No query is sent: nothing needs a model.
4. Sends SIGTERM to the process group (what the supervisor does) and requires
   a graceful exit within 15 s (uvicorn's shutdown lines; exit by the re-raised
   SIGTERM), with no traceback, unexpected ERROR record or
   Python warning in the console or the on-disk app log.

Needs PostgreSQL 16 server binaries (``initdb``/``postgres``/``psql``/
``pg_controldata``): found on PATH, in ``SMOKE_PG_BIN``, or at
``/usr/lib/postgresql/16/bin`` (Ubuntu and the GitHub runner). Without them the
module skips, unless ``BOOT_SMOKE_REQUIRED=1`` (CI), where it fails instead.

Run: ``python -m pytest tests/smoke -m boot_smoke -o addopts="" -p no:cacheprovider``
(serially; never under xdist, the server owns real ports and a real cluster).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = [pytest.mark.boot_smoke, pytest.mark.timeout(240)]

pytest.importorskip("fcntl", reason="the container supervisor's database lifecycle is POSIX-only")

REPO = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 60
EXIT_TIMEOUT_S = 15
OWNER_PASSWORD = "smoke-boot-owner-password"

# Console lines and log records that are expected on this offline boot. Each
# entry: (regex, reason). Anything else that looks like a traceback, an ERROR/
# CRITICAL record or a Python warning fails the test.
ALLOWED_PROBLEMS = [
    (
        r"UserWarning: Duplicate Operation ID retired_billing_",
        "FastAPI warns while building /openapi.json: app/api/openai_billing.py's "
        "retired_billing is one api_route for GET and POST, so both operations share "
        "an id. Harmless for serving; allowed until that route is split or hidden.",
    ),
    (
        r"^\s*warnings\.warn\(message, stacklevel=1\)$",
        "The source line Python prints under the allowed FastAPI UserWarning above.",
    ),
    (
        r"app\.services\.fal_catalog - ERROR - Failed to refresh fal\.ai catalog",
        "GET /api/v1/catalog/models refreshes a stale catalog from api.fal.ai; the "
        "test's proxy refuses it (offline by design) and the route still answers.",
    ),
]

# Hosts the app may try to reach AFTER readiness, each caused by a request this
# test makes. Nothing may be contacted before readiness.
ALLOWED_OUTBOUND = {
    "api.fal.ai": "GET /api/v1/catalog/models refreshes the stale fal.ai catalog",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pg16_bindir() -> str | None:
    candidates = [os.environ.get("SMOKE_PG_BIN")]
    on_path = shutil.which("initdb")
    if on_path:
        candidates.append(str(Path(on_path).resolve().parent))
    candidates.append("/usr/lib/postgresql/16/bin")
    for candidate in candidates:
        if not candidate:
            continue
        bindir = Path(candidate)
        if not all((bindir / name).is_file() for name in ("initdb", "postgres", "psql", "pg_controldata")):
            continue
        try:
            version = subprocess.run([str(bindir / "postgres"), "--version"],
                                     capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if re.search(r"\b16\.", version):
            return str(bindir)
    return None


def _load_container_runtime():
    # docker/ is not a package (and "docker" is also the Docker SDK's name), so
    # load the supervisor by path, as tests/core/test_container_runtime.py does.
    spec = importlib.util.spec_from_file_location(
        "shuffle_container_runtime_boot_smoke", REPO / "docker" / "container_runtime.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutboundRecorder:
    """An HTTP(S) proxy that refuses every request and records its target host."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._lock = threading.Lock()
        self._attempts: list[tuple[float, str]] = []
        threading.Thread(target=self._serve, name="boot-smoke-proxy", daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    line = conn.recv(8192).split(b"\r\n", 1)[0].decode("latin-1")
                except OSError:
                    line = ""
                parts = line.split()
                target = parts[1] if len(parts) >= 2 else (line or "<no request line>")
                host = urlsplit(target).hostname if "://" in target else target.rsplit(":", 1)[0]
                with self._lock:
                    self._attempts.append((time.monotonic(), host or target))
                try:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def attempts(self) -> list[tuple[float, str]]:
        with self._lock:
            return list(self._attempts)

    def close(self) -> None:
        self._sock.close()


def _ws_connect(url: str):
    """websockets' sync client, never through an ambient proxy (15+ honours one)."""
    import inspect

    from websockets.sync.client import connect
    kwargs = {"open_timeout": 10}
    if "proxy" in inspect.signature(connect).parameters:
        kwargs["proxy"] = None
    return connect(url, **kwargs)


def _problem_lines(text: str) -> list[str]:
    """Traceback / ERROR / CRITICAL / Python-warning lines not on the allow-list."""
    patterns = (
        r"Traceback \(most recent call last\)",
        r" - (ERROR|CRITICAL) - ",
        r"^(ERROR|CRITICAL):",
        r"\b\w+Warning: ",
        r"^\s*warnings\.warn\(",
        r"Task exception was never retrieved",
        r"was never awaited",
    )
    found = []
    for line in text.splitlines():
        if any(re.search(p, line) for p in patterns) and not any(
            re.search(allowed, line) for allowed, _reason in ALLOWED_PROBLEMS
        ):
            found.append(line)
    return found


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class BootedServer(SimpleNamespace):
    """What the tests share: base URL, process, paths, recorder, owner token."""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    bindir = _pg16_bindir()
    if bindir is None:
        message = ("PostgreSQL 16 server binaries not found (PATH, SMOKE_PG_BIN or "
                   "/usr/lib/postgresql/16/bin); the boot smoke test needs a real cluster")
        if os.environ.get("BOOT_SMOKE_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)

    root = tmp_path_factory.mktemp("boot-smoke")
    data, workspace, home = root / "data", root / "workspace", root / "home"
    workspace.mkdir()
    home.mkdir()
    env_file = root / "empty.env"
    env_file.write_text("")

    runtime = _load_container_runtime()
    runtime.STOP.clear()
    db = runtime.Database(SimpleNamespace(effective_data_dir=data), port=_free_port())
    recorder = OutboundRecorder()
    process = None
    console_path = root / "console.log"
    console = open(console_path, "wb")
    started = time.monotonic()
    try:
        with pytest.MonkeyPatch.context() as mp:
            # The supervisor finds initdb/postgres/psql on PATH (the image puts
            # /usr/lib/postgresql/16/bin there).
            mp.setenv("PATH", bindir + os.pathsep + os.environ.get("PATH", ""))
            db.open()
        db.publish_descriptor()
        db_ready = time.monotonic()

        port, protected_port = _free_port(), _free_port()
        # An allow-list, not os.environ minus a few keys: the test process
        # carries TESTING=1, DATA_DIR and CORS_ORIGINS from tests/conftest.py,
        # and the developer's shell may carry provider keys or a real config.
        inherited = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "LD_LIBRARY_PATH", "SYSTEMROOT")
        env = {k: v for k, v in os.environ.items() if k in inherited}
        env.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "DATA_DIR": str(data),
            "WORKSPACE_DIR": str(workspace),
            "LOCAL_MODE": "true",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "SHUFFLE_DESKTOP_PROTECTED_PORT": str(protected_port),
            "AI_SHUFFLE_ENV_FILE": str(env_file),
            "START_CHROME": "0",
            # Serve /docs and /openapi.json: building the schema is itself a
            # check (nothing else generates it outside a live server).
            "API_DOCS_ENABLED": "true",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HTTP_PROXY": recorder.url, "http_proxy": recorder.url,
            "HTTPS_PROXY": recorder.url, "https_proxy": recorder.url,
            "ALL_PROXY": recorder.url, "all_proxy": recorder.url,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        })
        process = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=REPO, env=env,
                                   stdout=console, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + READY_TIMEOUT_S
        ready = None
        with httpx.Client(trust_env=False, timeout=5) as probe:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    response = probe.get(base + "/health/ready")
                    if response.status_code == 200 and response.json().get("status") == "ready":
                        ready = response.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
        if ready is None:
            console.flush()
            tail = console_path.read_text(errors="replace")[-6000:]
            pytest.fail(f"server not ready within {READY_TIMEOUT_S}s "
                        f"(exit code {process.poll()}); console tail:\n{tail}")
        ready_at = time.monotonic()
        client = httpx.Client(base_url=base, trust_env=False, timeout=30)
        booted = BootedServer(
            base=base, port=port, client=client, process=process, ready=ready,
            data=data, workspace=workspace, console_path=console_path, recorder=recorder,
            ready_at=ready_at, timings={"postgres_s": round(db_ready - started, 1),
                                        "app_ready_s": round(ready_at - db_ready, 1)},
            token=None, session_id=None,
        )
        yield booted
        client.close()
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    # Only reached when a test failed before the SIGTERM test ran.
                    for sig, wait in ((signal.SIGTERM, EXIT_TIMEOUT_S), (signal.SIGKILL, 5)):
                        try:
                            os.killpg(process.pid, sig)
                        except ProcessLookupError:
                            break
                        try:
                            process.wait(timeout=wait)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                # As the supervisor's stop_group: the leader exiting is not
                # evidence that the rest of its process group did.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            console.close()
            recorder.close()
            db.close()


def _auth(server) -> dict:
    assert server.token, "owner bootstrap did not produce a token (see test_owner_bootstrap)"
    return {"Authorization": f"Bearer {server.token}"}


# ---------------------------------------------------------------------------
# Tests (run in file order against one server; the last one shuts it down)
# ---------------------------------------------------------------------------

def test_ready_without_outbound_network(server):
    assert server.ready["checks"]["database"] == "ok"
    before_ready = [host for at, host in server.recorder.attempts() if at <= server.ready_at]
    assert before_ready == [], f"startup tried to reach the network: {before_ready}"


def test_unauthenticated_surface(server):
    c = server.client
    health = c.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "healthy"
    # A fresh install: nothing configured, no owner password yet.
    assert body["setup_required"] is True
    assert c.get("/api/v1/health").json()["status"] == "healthy"

    version = c.get("/api/v1/version")
    assert version.status_code == 200
    assert version.json()["api_version"] == body["version"] == server.ready["version"]

    for path in ("/does-not-exist", "/api/v1/does-not-exist"):
        missing = c.get(path)
        assert missing.status_code == 404, path
        assert missing.headers["content-type"].startswith("application/json"), path
        assert missing.json() == {"detail": "Not Found"}, path

    # The SPA is served only when the frontend build is present (the image
    # copies it to app/static; a source checkout usually has none).
    root = c.get("/")
    if (REPO / "app" / "static" / "index.html").is_file():
        assert root.status_code == 200
        assert root.headers["content-type"].startswith("text/html")
    else:
        assert root.status_code == 404

    docs = c.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    schema = c.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/health" in paths and "/api/v1/sessions" in paths and "/api/v1/owner/login" in paths

    documentation = c.get("/api/v1/documentation")
    assert documentation.status_code == 200
    assert any(cat["id"] == "getting-started" for cat in documentation.json()["categories"])


def test_owner_bootstrap(server):
    c = server.client
    # A browser on this computer: loopback is passwordless owner access, and
    # the CSRF middleware makes every unsafe loopback request same-origin
    # (owner/login included), so these carry the page's Origin.
    origin = {"Origin": server.base}
    status = c.get("/api/v1/owner/status").json()
    assert status["password_set"] is False
    assert status["setup_allowed"] is True

    # Signing in before a password exists is refused, not a crash.
    early = c.post("/api/v1/owner/login", json={"password": OWNER_PASSWORD, "bearer": True}, headers=origin)
    assert early.status_code == 409, early.text

    # First-run: the local-network browser creates the owner password.
    created = c.post("/api/v1/owner/password", json={"password": OWNER_PASSWORD}, headers=origin)
    assert created.status_code == 200, created.text
    assert created.json() == {"status": "ok"}
    assert "session" in created.cookies
    c.cookies.clear()  # the rest of the run authenticates by Bearer only
    assert c.get("/api/v1/owner/status").json()["password_set"] is True

    wrong = c.post("/api/v1/owner/login", json={"password": "not-the-owner-password", "bearer": True},
                   headers=origin)
    assert wrong.status_code == 401

    login = c.post("/api/v1/owner/login", json={"password": OWNER_PASSWORD, "bearer": True}, headers=origin)
    assert login.status_code == 200, login.text
    server.token = login.json()["token"]
    assert server.token

    # An Authorization header opts out of passwordless loopback access, so a
    # bad token must be refused on HTTP and on the chat socket.
    assert c.get("/api/v1/sessions", headers={"Authorization": "Bearer shr_not-a-real-token"}).status_code == 401
    from websockets.exceptions import ConnectionClosed
    with pytest.raises(ConnectionClosed) as closed:
        with _ws_connect(server.base.replace("http", "ws") + "/api/v1/ws/chat?token=shr_bad") as ws:
            ws.recv(timeout=10)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4001


def test_owner_sessions_and_default_profile(server):
    c, auth = server.client, _auth(server)
    sessions = c.get("/api/v1/sessions", headers=auth)
    assert sessions.status_code == 200
    assert sessions.json() == []

    profiles = c.get("/api/v1/agents", headers=auth)
    assert profiles.status_code == 200
    profile_ids = [p["id"] for p in profiles.json()]
    assert "general-purpose" in profile_ids  # built-ins are seeded on first boot

    # A project is a git repository in the workspace; the UI then creates a
    # session on a new branch without a prompt (no model involved).
    repo = server.workspace / "smoke-project"
    repo.mkdir()
    git = ["git", "-c", "user.name=Boot Smoke", "-c", "user.email=boot-smoke@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("boot smoke\n")
    subprocess.run([*git, "add", "README.md"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run([*git, "branch", "-M", "main"], cwd=repo, check=True)

    project = c.post("/api/v1/projects", headers=auth,
                     json={"id": "smoke-project", "name": "Smoke project", "path": "smoke-project"})
    assert project.status_code == 201, project.text

    created = c.post("/api/v1/sessions/with-worktree", headers=auth, json={
        "project_id": "smoke-project", "branch_name": "boot-smoke", "base_branch": "main",
        "create_new_branch": True,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "active"
    assert body["query_started"] is False
    server.session_id = body["session_id"]

    listed = c.get("/api/v1/sessions", headers=auth).json()
    assert [s["id"] for s in listed] == [server.session_id]
    # No profile was given: the session takes the install's default profile.
    assert listed[0]["profile_id"] in profile_ids

    detail = c.get(f"/api/v1/sessions/{server.session_id}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["id"] == server.session_id


def test_chat_websocket_initial_frames(server):
    assert server.session_id, "no session (see test_owner_sessions_and_default_profile)"
    url = server.base.replace("http", "ws") + f"/api/v1/ws/chat?token={server.token}&device_id=boot-smoke"
    with _ws_connect(url) as ws:
        first = json.loads(ws.recv(timeout=10))
        assert first["type"] == "user_streaming_sessions"
        assert first["session_ids"] == []
        # The second connect frame: sessions still waiting on a prompt.
        attention = json.loads(ws.recv(timeout=10))
        assert attention["type"] == "session_attention_snapshot", attention
        assert attention["sessions"] == []

        ws.send(json.dumps({"type": "load_session", "session_id": server.session_id}))
        history = json.loads(ws.recv(timeout=10))
        assert history["type"] == "history", history
        assert history["session_id"] == server.session_id
        assert history["session"]["id"] == server.session_id

        # An unknown type is answered with an error frame; the socket stays open.
        ws.send(json.dumps({"type": "not-a-message-type"}))
        frames = [json.loads(ws.recv(timeout=10))]
        while frames[-1]["type"] != "error" and len(frames) < 10:
            frames.append(json.loads(ws.recv(timeout=10)))  # e.g. a keep-alive ping
        assert frames[-1]["type"] == "error", frames
        assert "Unknown message type" in frames[-1]["message"]


def test_owner_settings_usage_catalog_api_users(server):
    c, auth = server.client, _auth(server)
    integrations = c.get("/api/v1/settings/integrations", headers=auth)
    assert integrations.status_code == 200
    assert integrations.json()["anthropic_api_key_set"] is False  # no provider keys: offline
    for path in ("/api/v1/settings/claude-auth", "/api/v1/settings/text-generation", "/api/v1/settings/voice"):
        assert c.get(path, headers=auth).status_code == 200, path

    usage = c.get("/api/v1/usage/limits", headers=auth)  # the usage panel's poll
    assert usage.status_code == 200
    assert isinstance(usage.json()["windows"], list)

    catalog = c.get("/api/v1/catalog/models", headers=auth)
    assert catalog.status_code == 200
    assert isinstance(catalog.json()["models"], list)

    api_users = c.get("/api/v1/api-users", headers=auth)
    assert api_users.status_code == 200
    assert api_users.json() == []


def test_sigterm_exits_cleanly_without_tracebacks(server):
    process = server.process
    assert process.poll() is None, "the server exited before shutdown was requested"
    sent = time.monotonic()
    os.killpg(process.pid, signal.SIGTERM)  # what the container supervisor sends
    try:
        code = process.wait(timeout=EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"no exit within {EXIT_TIMEOUT_S}s of SIGTERM")
    stop_s = time.monotonic() - sent
    # uvicorn finishes its graceful shutdown and then re-raises the signal it
    # caught, so a clean stop exits by SIGTERM (-15), not with status 0; the
    # shutdown lines below prove it was graceful. Anything else is a crash.
    assert code in (0, -signal.SIGTERM), f"exit code {code}"

    console = server.console_path.read_text(errors="replace")
    logs_dir = next((server.data / "logs").iterdir())
    app_log = (logs_dir / "app.log").read_text(errors="replace")
    fatal = (logs_dir / "fatal.log").read_text(errors="replace") if (logs_dir / "fatal.log").exists() else ""

    # Application records reach the console (docker logs), not only the file.
    assert "Starting AI Shuffle v" in console
    assert "Application shutdown complete." in console
    assert "Finished server process" in console
    assert "AI Shuffle shutting down" in app_log
    assert fatal.strip() == "", f"fatal.log is not empty:\n{fatal}"

    problems = _problem_lines(console) + _problem_lines(app_log)
    assert problems == [], "unexpected tracebacks/errors/warnings:\n" + "\n".join(problems)

    unexpected = sorted({host for _, host in server.recorder.attempts() if host not in ALLOWED_OUTBOUND})
    assert unexpected == [], f"outbound connection attempts: {unexpected}"

    server.timings["shutdown_s"] = round(stop_s, 1)
    print(f"boot smoke timings: {server.timings}; outbound attempts refused: "
          f"{sorted({host for _, host in server.recorder.attempts()})}")
