"""One command for a private, live-reloading dev copy of this checkout.

    python -m scripts.devloop up [--host 0.0.0.0 --browser-host IP] [--ttl-seconds N]
                                 [--no-vite] [--seed default|none] [--status PATH] [--ci]
                                 [--claude-cli fake|none]
    python -m scripts.devloop status [--status PATH] [--wait SECONDS]
    python -m scripts.devloop down [--status PATH]
    python -m scripts.devloop up --pair [--ci] [...]       two paired instances (scripts/devloop/pair.py)
    python -m scripts.devloop pair offline|online a|b       take one paired instance's app down / back
    python -m scripts.devloop status|down --pair

``up`` starts the fake AI, an isolated app with its own throwaway PostgreSQL
(``app.preview_run``'s lease), and Vite dev with HMR for this checkout's
frontend; seeds data; prints one access link; writes a status JSON. It
restarts only the app process when a Python file under app/ or fluid/
changes. Run it under a Monitor task, never daemonize it: Ctrl-C, SIGTERM,
SIGHUP, ``down``, parent loss and the TTL all tear everything down, and a
detached guardian (``scripts/devloop/guardian.py``) finishes the teardown if
``up`` itself is SIGKILLed. ``--ci`` is the non-interactive form CI uses.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
log = logging.getLogger("devloop")
#: Graceful stop budget for the app (restarts and teardown). Open SSE/WebSocket
#: streams keep uvicorn waiting, so after this the app is SIGKILLed and any
#: in-flight work in it is cut off. PostgreSQL is a separate process and
#: survives. The one start-up job that must not be cut, the first start's seed,
#: is protected twice: :class:`RestartGate` holds file-change restarts until
#: that start is ready, and a seed cut anyway is reported, never re-run.
APP_STOP_SECONDS = 4
#: How long the gate holds restarts for a first start that neither answers nor
#: exits (a hang): after this an edit restarts it anyway.
FIRST_START_GRACE_SECONDS = 180
#: ``--ci`` prints exactly one of these, then a JSON object, on one stdout line.
READY_LINE = "DEVLOOP_READY"
FAILED_LINE = "DEVLOOP_FAILED"
#: Signals that stop ``up`` with a full teardown. SIGHUP: the terminal or the
#: Monitor's pty went away. TaskStop SIGTERMs the group, then SIGKILLs every
#: descendant about a second later; the guardian finishes what that cuts off.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
#: ``up --pair`` member only: take the app offline (it stays stopped,
#: PostgreSQL and the data stay up) and bring it back (``pair offline|online``).
OFFLINE_SIGNAL, ONLINE_SIGNAL = signal.SIGUSR1, signal.SIGUSR2


class StatusError(Exception):
    """A status path that is not safely private to this user."""


def status_directory() -> Path:
    """Private to this user: ``$XDG_RUNTIME_DIR`` when set, else a per-uid temp dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "ai-shuffle-devloop"
    return Path(tempfile.gettempdir()) / f"ai-shuffle-devloop-{os.getuid()}"


def default_status_path(root: Path = ROOT) -> Path:
    """Per checkout, in :func:`status_directory` (never the repo)."""
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return status_directory() / f"{digest}.json"


def default_pair_status_path(root: Path = ROOT) -> Path:
    """``up --pair``'s status (it lists both instances), beside the single one."""
    path = default_status_path(root)
    return path.with_name(path.stem + "-pair.json")


def ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` 0700, or accept it only if it is already private to us.

    A shared temp dir lets another user pre-create the directory (or a symlink
    in its place) and then read or swap the status file; refuse that.
    """
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    info = os.lstat(directory)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise StatusError(
            f"Refusing the status directory {directory}: it must be a real directory owned "
            "by you with no group or other access (chmod 700), or pass another --status")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def read_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_status(path: Path) -> dict | None:
    """The launcher status at ``path``: None if absent or unreadable JSON.

    Raises :class:`StatusError` for a symlink, a non-file or a file another
    user owns, so ``down`` never signals a pid someone else wrote down.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: a symlink
        raise StatusError(f"Refusing the status file {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StatusError(
                f"Refusing the status file {path}: it is not a regular file owned by you")
        try:
            data = json.loads(stream.read().decode("utf-8"))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def write_private_json(path: Path, data: dict) -> None:
    """Atomically replace ``path`` with a 0600 file, never through a planted link."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a stale tmp from a killed launcher
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.replace(tmp, path)


def status_alive(status: dict) -> bool:
    """The recorded launcher is still the same live process (not a reused pid).

    An exited launcher its parent has not reaped yet (a zombie, as when the
    CI driver that started it runs ``down``) still has its start time; it is
    gone all the same.
    """
    from scripts.devloop.guardian import alive

    pid, identity = status.get("pid"), status.get("pid_identity")
    return isinstance(pid, int) and isinstance(identity, str) and alive(pid, identity)


def answers_http(host: str, port: int, path: str = "/", timeout: float = 1.0) -> bool:
    """Any HTTP response (403 included) means the listener finished starting."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path)
        connection.getresponse().read()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _connect_host(host: str) -> str:
    return "127.0.0.1" if ipaddress.ip_address(host).is_unspecified else host


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


class RestartGate:
    """Holds file-change restarts until a start has got ready (and so seeded).

    A start seeds the data inside its lifespan, and the app answers HTTP only
    after that, so "ready" means the seed is finished (done or failed). A
    restart before then would SIGKILL a half-done seed. The gate opens for good
    (:attr:`is_open`) only when a start gets ready. Until then each start holds
    the changes seen while it runs and folds them into one restart when it
    stops being cuttable: it exited by itself (a crash must stay fixable) or it
    hung past :data:`FIRST_START_GRACE_SECONDS`. That pass lasts only until the
    next start: the start the fix triggers is the one that seeds, so it is held
    closed again (:meth:`started`).
    """

    def __init__(self, restart: threading.Event) -> None:
        self.restart = restart
        self.lock = threading.Lock()
        self.is_open = False
        #: The current start crashed or hung: nothing to cut until the next one.
        self.passing = False
        self.pending = False

    def request(self) -> bool:
        """A source change: restart now, or hold it. True if it was held."""
        with self.lock:
            if self.is_open or self.passing:
                self.restart.set()
                return False
            self.pending = True
            return True

    def started(self) -> None:
        """A new app start: hold changes again until it is ready (or not cuttable)."""
        with self.lock:
            self.passing = False

    def release(self) -> None:
        """The current start exited by itself or hung: let changes restart it."""
        with self.lock:
            if self.is_open or self.passing:
                return
            self.passing = True
            self._flush("the app start ended or hung; applying the change seen meanwhile")

    def open(self) -> None:
        """A start got ready: its seed is finished, restarts pass from now on."""
        with self.lock:
            if self.is_open:
                return
            self.is_open = True
            self._flush("first start settled; applying the change seen meanwhile")

    def _flush(self, message: str) -> None:
        if self.pending:
            self.pending = False
            log.info(message)
            self.restart.set()


class Watcher(threading.Thread):
    """Requests a restart when a Python file under app/ or fluid/ changes."""

    def __init__(self, gate: RestartGate, *, poll: bool = False) -> None:
        super().__init__(name="devloop-watch", daemon=True)
        self.gate, self.poll = gate, poll
        self.stop_event = threading.Event()

    def run(self) -> None:
        from watchfiles import PythonFilter, watch

        class SourceFilter(PythonFilter):
            def __call__(self, change: Any, path: str) -> bool:
                parts = Path(path).parts
                return super().__call__(change, path) and "tests" not in parts

        for changes in watch(ROOT / "app", ROOT / "fluid", watch_filter=SourceFilter(),
                             stop_event=self.stop_event, debounce=1600, step=300,
                             raise_interrupt=False, force_polling=self.poll or None):
            names = sorted({os.path.relpath(path, ROOT) for _, path in changes})
            shown = ", ".join(names[:5])
            if len(names) > 5:
                shown += f" (+{len(names) - 5} more)"
            if self.gate.request():
                log.info("change: %s -> restarting once the first start (and seed) is done",
                         shown)
            else:
                log.info("change: %s -> restarting the app", shown)

    def stop(self) -> None:
        self.stop_event.set()


class Instance:
    """The status file and the readiness reporting for one ``up``."""

    def __init__(self, args: argparse.Namespace, directory: Path, env: dict, fake: Any,
                 vite_port: int | None, gate: RestartGate | None = None) -> None:
        from app.core.temp_scratch import process_identity

        self.args, self.directory, self.env, self.fake = args, directory, env, fake
        self.vite_port = vite_port
        self.gate = gate or RestartGate(threading.Event())
        #: Set by a stop signal: the status says "stopping" and is never rewritten.
        self.retired = False
        #: ``up``'s managed PostgreSQL, recorded for the guardian once it runs.
        self.database: Any = None
        #: ``up``'s stop event; ``--ci`` sets it when the instance cannot be used.
        self.stopped: threading.Event | None = None
        self.failed: str | None = None
        #: The link and seed are announced on the first start that gets ready,
        #: which is not the first start when that one crashed.
        self.announced = False
        self.vite: subprocess.Popen | None = None
        self.port = int(env["SHUFFLE_PREVIEW_PORT"])
        self.connect = _connect_host(str(args.host))
        self.browser = _url_host(str(args.browser_host or args.host))
        self.started = time.time()
        self.app: subprocess.Popen | None = None
        self.app_started = 0.0
        self.starts = 0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.done = threading.Event()
        self.data: dict[str, Any] = {
            "format": 1, "pid": os.getpid(), "pid_identity": process_identity(os.getpid()),
            "pair_role": getattr(args, "pair_role", None),
            "checkout": str(ROOT), "branch": _branch(), "state": "starting",
            "mode": "ci" if getattr(args, "ci", False) else "dev",
            "started_at": _iso(self.started), "expires_at": _iso(self.started + args.ttl_seconds),
            "access_link": None,
            "urls": {"ui": f"http://{self.browser}:{vite_port or self.port}/",
                     "vite": f"http://{self.browser}:{vite_port}/" if vite_port else None,
                     "preview_listener": f"http://{self.browser}:{self.port}/"},
            "app": {"pid": None, "pid_identity": None, "starts": 0, "last_ready_seconds": None,
                    "local_port": int(env["PORT"]),
                    "protected_port": int(env["SHUFFLE_PROTECTED_PORT"])},
            "vite": None, "postgres": None,
            "fake_ai": {"url": fake.url, "request_log": str(fake.log_path),
                        "scenarios_dir": str(fake.scenarios.directory),
                        "scenarios": sorted(fake.scenarios.load())},
            "scratch_dir": str(directory), "data_dir": env["DATA_DIR"],
            "workspace_dir": env["WORKSPACE_DIR"], "seed": None,
        }

    # ── status file ──
    def write(self, **changes: Any) -> None:
        with self.lock:
            if self.retired:
                return
            self.data.update(changes)
            write_private_json(self.args.status, self.data)

    def retire(self) -> None:
        """A stop began: say so at once, before the slow teardown.

        A stop from Monitor/TaskStop SIGKILLs this process about a second after
        its SIGTERM, long before the app and PostgreSQL are down, so "ready"
        must not survive until the end of the teardown. Nothing rewrites the
        file after this; the guardian removes it if the teardown is cut off.
        """
        with self.lock:
            if self.retired:
                return
            self.retired = True
            self.data["state"] = "stopping"
            try:
                write_private_json(self.args.status, self.data)
            except (OSError, StatusError):
                self.args.status.unlink(missing_ok=True)

    # ── supervise() hook ──
    def on_app_start(self, process: subprocess.Popen) -> None:
        from app.core.temp_scratch import process_identity

        self.starts += 1
        self.gate.started()
        self.app, self.app_started = process, time.monotonic()
        log.info("app %s (pid %s)", "starting" if self.starts == 1 else "restarting", process.pid)
        with self.lock:
            self.data["app"].update(pid=process.pid, pid_identity=process_identity(process.pid),
                                    starts=self.starts)
            postgres = getattr(self.database, "process", None)
            if postgres is not None and self.data["postgres"] is None:
                socket = getattr(self.database, "socket", None)
                self.data["postgres"] = {"pid": postgres.pid,
                                         "pid_identity": process_identity(postgres.pid),
                                         "socket_dir": str(socket) if socket else None}
        self.write(state="starting" if self.starts == 1 else "restarting")
        self.wake.set()

    # ── readiness ──
    def on_hold(self) -> None:
        """supervise(): the app was taken offline (``pair offline``) and stays down."""
        log.info("app offline (held until `pair online`)")
        self.write(state="offline")

    def on_app_exit(self, process: subprocess.Popen) -> None:
        """supervise(): the app exited on its own (not a restart or teardown)."""
        self.write(state="app-exited")
        # Nothing left to interrupt: the fix must restart it. Only this start's
        # pass: the start the fix triggers seeds and is held closed again.
        self.gate.release()

    def report(self) -> None:
        """Thread: report every app (re)start once it answers HTTP."""
        while not self.done.is_set():
            self.wake.wait(0.5)
            if not self.wake.is_set():
                continue
            self.wake.clear()
            process, started = self.app, self.app_started
            while not self.done.is_set() and process is self.app and process.poll() is None:
                if answers_http(self.connect, self.port, "/api/preview"):
                    self._ready(process, round(time.monotonic() - started, 1))
                    break
                if time.monotonic() - started > FIRST_START_GRACE_SECONDS:
                    self.gate.release()
                time.sleep(0.25)

    def _ready(self, process: subprocess.Popen, seconds: float) -> None:
        with self.lock:
            self.data["app"]["last_ready_seconds"] = seconds
        if not self.announced:
            self.announced = True
            self._announce(seconds)
        else:
            log.info("app ready again in %.1fs (pid %s)", seconds, process.pid)
            self.write(state="ready")
        self.gate.open()

    def _announce(self, seconds: float) -> None:
        from app.core import preview_access

        access = read_status(self.directory / "access.json") or {}
        seed = read_status(self.directory / "seed.json")
        link = access.get("url")
        if link and self.vite_port:
            link = preview_access.preview_url(access["session_id"], self.browser.strip("[]"),
                                              self.vite_port, token=access["token"])
        if self.vite_port:
            deadline = time.monotonic() + 60
            while not self.done.is_set() and time.monotonic() < deadline:
                if answers_http(_connect_host(str(self.args.host)), self.vite_port):
                    break
                time.sleep(0.25)
            else:
                log.warning("Vite did not answer on port %s yet", self.vite_port)
        self.write(state="ready", access_link=link, seed=seed)
        lines = [
            f"app ready in {seconds:.1f}s",
            f"open once in the agent browser: {link}",
            f"status file: {self.args.status}",
            f"fake AI: {self.fake.url} (request log {self.fake.log_path})",
        ]
        if seed is not None:
            lines.append("seed: " + ("ok" if seed.get("ok") else f"FAILED: {seed.get('error')}"))
        if seed is not None and not seed.get("ok"):
            log.error("SEED FAILED: %s", seed.get("error"))
        for line in lines:
            log.info(line)
        if getattr(self.args, "ci", False):
            self._ci_verdict(link, seed, seconds)

    def _ci_verdict(self, link: str | None, seed: dict | None, seconds: float) -> None:
        """``--ci``: one machine-readable line; an unusable instance stops at once."""
        problem = None
        if not link:
            problem = "no access link was minted"
        elif self.args.seed != "none" and not (seed or {}).get("ok"):
            problem = f"seed failed: {(seed or {}).get('error', 'no seed result')}"
        record = {"status": str(self.args.status), "ui": self.data["urls"]["ui"],
                  "pid": os.getpid(), "seconds": seconds}
        if problem is None:
            print(READY_LINE + " " + json.dumps(record), flush=True)
            return
        self.failed = problem
        print(FAILED_LINE + " " + json.dumps({**record, "error": problem}), flush=True)
        if self.stopped is not None:
            self.stopped.set()


def _branch() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "branch", "--show-current"],
                              capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _vite_command(host: str, port: int) -> list[str]:
    return [str(FRONTEND / "node_modules" / ".bin" / "vite"), "dev", "--host", host,
            "--port", str(port), "--strictPort"]


def vite_environment(env: dict, api: str) -> dict:
    """Vite needs no credentials either: the app's controlled PATH/LANG/HOME."""
    keep = {key: env[key] for key in ("PATH", "LANG", "LD_LIBRARY_PATH", "HOME") if key in env}
    return {**keep, "AI_SHUFFLE_DEV_API": api, "BROWSER": "none", "NO_COLOR": "1",
            "FORCE_COLOR": "0"}


def _ensure_frontend() -> None:
    if (FRONTEND / "node_modules" / ".bin" / "vite").exists():
        return
    log.info("frontend/node_modules is missing: running npm ci (one time)")
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    browser = args.browser_host or args.host
    if browser.is_unspecified:
        parser.error("--browser-host must name a reachable address for a wildcard --host")
    for name in ("port", "vite_port", "protected_port"):
        value = getattr(args, name)
        if value is not None and (not 1024 <= value <= 65535 or value in (8000, 8001)):
            parser.error(f"--{name.replace('_', '-')} must be a free port other than 8000/8001")
    if args.port is not None and args.port == args.vite_port:
        parser.error("--port and --vite-port must differ")
    if not 0 < args.ttl_seconds <= 14400:
        parser.error("--ttl-seconds must be positive and at most four hours")
    if args.ci:
        if not args.host.is_loopback or args.browser_host is not None:
            parser.error("--ci binds loopback only (no --host/--browser-host)")
        if args.vite_port is not None:
            parser.error("--ci serves the stamped build; --vite-port does not apply")
        args.no_vite = True
    if args.no_vite:
        problem = stamped_build_problem()
        if problem:
            parser.error(f"--no-vite{'/--ci' if args.ci else ''} serves the stamped build, "
                         f"but {problem}: (cd frontend && SHUFFLE_SKIP_MOBILE_UI=1 npm run build) "
                         "&& python -m app.preview stamp")


def stamped_build_problem(root: Path = ROOT) -> str | None:
    """Why the app would refuse this checkout's frontend build, or None.

    The app compares the stamp with the sources and the build at start and
    exits when they differ; checking first turns that into one clear error.
    """
    from app.preview import MANIFEST, _build_digest, _build_dir, _source_identity

    directory = _build_dir(root)
    try:
        stamp = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "frontend/build has no stamp"
    try:
        expected = {**_source_identity(root), "build_sha256": _build_digest(directory)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"the build cannot be checked ({exc})"
    if stamp != expected:
        return "the frontend sources or build changed since the stamp"
    return None


def up(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if sys.platform != "linux":
        parser.error("The dev loop's owned PostgreSQL lease needs Linux")
    _validate(parser, args)
    try:
        ensure_private_dir(args.status.parent)
        existing = load_status(args.status)
    except StatusError as exc:
        parser.error(str(exc))
    if existing and status_alive(existing):
        parser.error(f"A dev instance for this checkout is already running (pid "
                     f"{existing['pid']}): python -m scripts.devloop down")
    from app.core.temp_scratch import scratch_directory
    from app.preview_run import free_port, preview_environment, supervise
    from docker import container_runtime as runtime
    from docker.container_runtime import stop_group
    from scripts.devloop.fake_ai import FakeAI
    from scripts.devloop.guardian import spawn_guardian

    if not args.no_vite:
        _ensure_frontend()
    stopped, restart = threading.Event(), threading.Event()
    gate = RestartGate(restart)
    holder: dict[str, Instance] = {}

    def stop(signum: int, frame: Any) -> None:
        log.info("stopping (signal %s)", signum)
        stopped.set()
        runtime.STOP.set()
        if "instance" in holder:
            # Off the signal frame: retire() takes the status lock, which this
            # (main) thread may hold right now.
            threading.Thread(target=holder["instance"].retire, name="devloop-retire",
                             daemon=True).start()

    old = {sig: signal.signal(sig, stop) for sig in STOP_SIGNALS}
    fake = vite = watcher = instance = reporter = None
    try:
        with scratch_directory("ai-shuffle-preview-") as raw:
            directory = Path(raw)
            # First, before anything it would have to clean up exists: a
            # detached process that finishes the teardown if this one is
            # SIGKILLed (TaskStop does that a second after its SIGTERM).
            guardian = spawn_guardian(args.status, directory)
            fake = FakeAI(log_path=directory / "fake-ai-requests.jsonl",
                          latency_ms=args.latency_ms, tps=args.tps)
            port = args.port or free_port()
            protected = args.protected_port or free_port(exclude=(port,))
            vite_port = None if args.no_vite else (
                args.vite_port or free_port(exclude=(port, protected)))
            extra = {"SHUFFLE_DEV_FAKE_AI_URL": fake.url,
                     "SHUFFLE_DEV_ACCESS_FILE": str(directory / "access.json"),
                     "SHUFFLE_DEV_SEED_FILE": str(directory / "seed.json")}
            if args.seed != "none":
                extra["SHUFFLE_DEV_SEED"] = args.seed
            if vite_port:
                extra["SHUFFLE_DEV_UI"] = "vite"
            if args.pair_role:
                extra.update(pair_member_environment(directory, protected))
            env = preview_environment(
                directory, port, protected, host=str(args.host),
                browser_host=str(args.browser_host or args.host), ttl_seconds=args.ttl_seconds,
                extra=extra,
            )
            claude_cli = None
            if args.claude_cli == "fake":
                from scripts.devloop import fake_claude

                # Only the instance's PATH: the shim lives in its scratch dir.
                bin_dir = fake_claude.install(directory)
                env["PATH"] = fake_claude.instance_path(env["PATH"], bin_dir)
                fake_claude.cover_home(Path(env["HOME"]), bin_dir)
                claude_cli = {"mode": "fake", "path": str(bin_dir / "claude"),
                              "log": str(fake_claude.log_path(directory))}
                log.info("fake Claude Code CLI on the app's PATH only: %s (log %s)",
                         claude_cli["path"], claude_cli["log"])
            database = runtime.Database(
                SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])),
                port=free_port(exclude=(port, protected, int(env["PORT"]), vite_port or 0)),
            )
            instance = Instance(args, directory, env, fake, vite_port, gate)
            instance.database, instance.stopped = database, stopped
            instance.data["guardian"] = guardian
            instance.data["claude_cli"] = claude_cli or {"mode": "none"}
            holder["instance"] = instance
            log.info("status file: %s", args.status)
            instance.write()
            if stopped.is_set():  # a signal before the instance existed
                instance.retire()
            if vite_port:
                from app.core.temp_scratch import process_identity

                api = f"http://{_url_host(instance.connect)}:{port}"
                vite = subprocess.Popen(_vite_command(str(args.host), vite_port), cwd=FRONTEND,
                                        env=vite_environment(env, api), start_new_session=True)
                instance.data["vite"] = {"pid": vite.pid,
                                         "pid_identity": process_identity(vite.pid),
                                         "port": vite_port, "proxies_to": api}
                log.info("vite dev (pid %s) on port %s, proxying /api to %s", vite.pid,
                         vite_port, api)
            if not args.ci:  # a CI run tests one fixed revision: no restarts
                watcher = Watcher(gate, poll=args.poll)
                watcher.start()
            reporter = threading.Thread(target=instance.report, name="devloop-report",
                                        daemon=True)
            reporter.start()
            hold = None
            if args.pair_role:
                hold = threading.Event()
                old.update(install_offline_switch(hold, restart))
            code = supervise([sys.executable, "-m", "app.main"], env, database,
                             ttl=args.ttl_seconds, owner_pid=os.getppid(), stopped=stopped,
                             restart=None if args.ci and not args.pair_role else restart,
                             on_start=instance.on_app_start, on_exit=instance.on_app_exit,
                             stop_timeout=APP_STOP_SECONDS, hold=hold,
                             on_hold=instance.on_hold)
            log.info("lease ended (app exit code %s); tearing down", code)
            if args.ci and not instance.announced and not stopped.is_set():
                instance.failed = f"the app exited (code {code}) before it was ready"
                print(FAILED_LINE + " " + json.dumps({"status": str(args.status),
                                                      "error": instance.failed}), flush=True)
            if args.ci:
                return 1 if instance.failed else 0
            return code
    finally:
        if instance is not None:
            instance.done.set()
            instance.retire()
        if watcher is not None:
            watcher.stop()
        if vite is not None:
            stop_group(vite, 10)
        if fake is not None:
            fake.close()
        if instance is not None:
            remove_own_status(args.status, os.getpid())
        for sig, handler in old.items():
            signal.signal(sig, handler)
        log.info("stopped; app, PostgreSQL, Vite and the fake AI are down")


def pair_member_environment(directory: Path, protected: int) -> dict[str, str]:
    """The app settings of one ``up --pair`` member (its launcher set the pair env).

    Its peer dials it at its own protected loopback listener (normal peer
    ingress, never the owner's local port), so that is what it advertises.
    The pair key and the other member's address make it refuse any other
    peer (``app.network.dev_pair``); the owner file lets the launcher pair
    the two through the app's own owner API.
    """
    key = os.environ.get("SHUFFLE_DEV_PAIR_KEY", "")
    peers = os.environ.get("SHUFFLE_DEV_PAIR_PEERS", "")
    if len(key) < 32 or not peers:
        raise SystemExit("--pair-role is for `up --pair`'s own members (no pair key or peer)")
    return {"SHUFFLE_DEV_PAIR_KEY": key, "SHUFFLE_DEV_PAIR_PEERS": peers,
            "CLUSTER_ADVERTISE_URL": f"http://127.0.0.1:{protected}",
            "SHUFFLE_DEV_OWNER_FILE": str(directory / "owner.json")}


def install_offline_switch(hold: threading.Event, restart: threading.Event) -> dict:
    """``pair offline`` (SIGUSR1) stops the app and holds it; ``pair online`` (SIGUSR2) frees it."""

    def offline(signum: int, frame: Any) -> None:
        hold.set()
        restart.set()

    def online(signum: int, frame: Any) -> None:
        hold.clear()

    return {OFFLINE_SIGNAL: signal.signal(OFFLINE_SIGNAL, offline),
            ONLINE_SIGNAL: signal.signal(ONLINE_SIGNAL, online)}


def down(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    status = load_status(args.status)
    if status is None:
        print(f"No dev instance recorded at {args.status}")
        return 0
    if not status_alive(status):
        print(f"The recorded launcher (pid {status.get('pid')}) is gone; removing its stale "
              "status file")
        args.status.unlink(missing_ok=True)
        return 0
    try:
        os.kill(status["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not status_alive(status):
            print(f"Stopped the dev instance (pid {status['pid']})")
            return 0
        time.sleep(0.25)
    print(f"The dev instance (pid {status['pid']}) did not stop within 60s", file=sys.stderr)
    return 1


def remove_own_status(path: Path, pid: int, identity: str | None = None) -> bool:
    """Delete the status file only if it still names launcher ``pid`` (and identity).

    A newer ``up`` for the same checkout may already have replaced it; that
    one's file must survive an older launcher's (or guardian's) cleanup.
    """
    try:
        status = load_status(path)
    except StatusError:
        return False
    if status is None or status.get("pid") != pid:
        return False
    if identity is not None and status.get("pid_identity") != identity:
        return False
    path.unlink(missing_ok=True)
    return True


def show(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Print the running instance's status. 0 when it is running (``--wait``: ready).

    A file whose launcher is gone (a reused pid included) is stale: it is
    reported and removed, never shown as a running instance.
    """
    deadline = time.monotonic() + (args.wait or 0)
    while True:
        status = load_status(args.status)
        if status is not None and not status_alive(status):
            remove_own_status(args.status, status.get("pid"), status.get("pid_identity"))
            print(f"Stale dev instance status at {args.status}: launcher pid "
                  f"{status.get('pid')} is gone; removed it")
            return 1
        ready = status is not None and status.get("state") == "ready"
        if status is not None and (ready or not args.wait):
            print(json.dumps(status, indent=2))
            return 0
        if status is not None and status.get("state") in ("app-exited", "stopping"):
            print(json.dumps(status, indent=2))
            print(f"The dev instance is {status['state']}, not ready", file=sys.stderr)
            return 1
        if time.monotonic() >= deadline:
            if status is None:
                print(f"No running dev instance recorded at {args.status}")
            else:
                print(json.dumps(status, indent=2))
                print(f"The dev instance did not get ready within {args.wait}s",
                      file=sys.stderr)
            return 1
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.devloop", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up", help="Start a dev instance (run under Monitor)")
    start.add_argument("--host", type=ipaddress.ip_address,
                       default=ipaddress.ip_address("127.0.0.1"),
                       help="Bind address for the UI listeners; 0.0.0.0 for a remote browser")
    start.add_argument("--browser-host", type=ipaddress.ip_address,
                       help="Address the browser reaches this machine by (required with 0.0.0.0)")
    start.add_argument("--ttl-seconds", type=int, default=3600)
    start.add_argument("--port", type=int, help="Preview listener port (default: free)")
    start.add_argument("--vite-port", type=int, help="Vite dev port (default: free)")
    start.add_argument("--no-vite", action="store_true",
                       help="Serve the stamped frontend build instead of Vite dev")
    start.add_argument("--seed", default="default", help="Seed name, or none")
    start.add_argument("--latency-ms", type=float, default=0.0,
                       help="Fake AI delay before each reply")
    start.add_argument("--tps", type=float, default=0.0,
                       help="Fake AI streamed tokens per second (0 = fast)")
    start.add_argument("--claude-cli", choices=("fake", "none"), default="fake",
                       help="fake (default): a scripted claude CLI on the app's PATH, so Claude "
                            "Code chats run (scripts/devloop/fake_claude); none: leave PATH alone")
    start.add_argument("--poll", action="store_true",
                       help="Poll for source changes (for filesystems without inotify)")
    start.add_argument("--pair", action="store_true",
                       help="Two paired instances (A and B) in one network, each with its own "
                            "database, data and fake AI (scripts/devloop/pair.py)")
    start.add_argument("--pair-role", choices=("a", "b"), help=argparse.SUPPRESS)
    start.add_argument("--protected-port", type=int, help=argparse.SUPPRESS)
    start.add_argument("--ci", action="store_true", default=os.environ.get("DEVLOOP_CI") == "1",
                       help="Non-interactive run (also DEVLOOP_CI=1): loopback only, the "
                            "stamped build instead of Vite, no restarts, one "
                            f"{READY_LINE}/{FAILED_LINE} JSON line, exit 1 if unusable")
    status_command = commands.add_parser("status", help="Print the running instance's status")
    status_command.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                                help="Wait up to SECONDS for the instance to be ready; "
                                     "exit 0 only once it is")
    stop_command = commands.add_parser("down", help="Stop the running instance")
    switch = commands.add_parser("pair", help="Take one paired instance's app offline or back")
    switch.add_argument("action", choices=("offline", "online"))
    switch.add_argument("role", choices=("a", "b"))
    switch.add_argument("--wait", type=float, default=90, metavar="SECONDS")
    for command in (start, stop_command, status_command, switch):
        command.add_argument("--status", type=Path, default=None,
                             help="Status JSON path in a private (0700) directory (default: "
                                  "per checkout, under $XDG_RUNTIME_DIR or the temp dir)")
    for command in (stop_command, status_command):
        command.add_argument("--pair", action="store_true",
                             help="The checkout's paired instances (up --pair)")
    args = parser.parse_args(argv)
    if args.status is None:
        paired = args.command == "pair" or getattr(args, "pair", False)
        args.status = default_pair_status_path() if paired else default_status_path()
    logging.basicConfig(level=logging.INFO, format="[devloop] %(message)s", stream=sys.stdout,
                        force=True)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)  # we log each restart ourselves
    from scripts.devloop import pair

    if args.command == "up" and args.pair:
        handler = pair.up_pair
    elif args.command in ("down", "status") and pair.is_pair_status(args.status):
        handler = {"down": pair.down_pair, "status": pair.show_pair}[args.command]
    else:
        handler = {"up": up, "down": down, "status": show, "pair": pair.switch}[args.command]
    try:
        return handler(parser, args)
    except StatusError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
