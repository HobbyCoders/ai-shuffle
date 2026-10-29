"""One command for a private, live-reloading dev copy of this checkout.

    python -m scripts.devloop up [--host 0.0.0.0 --browser-host IP] [--ttl-seconds N]
                                 [--no-vite] [--seed default|none] [--status PATH]
    python -m scripts.devloop status [--status PATH]
    python -m scripts.devloop down [--status PATH]

``up`` starts the fake AI, an isolated app with its own throwaway PostgreSQL
(``app.preview_run``'s lease), and Vite dev with HMR for this checkout's
frontend; seeds data; prints one access link; writes a status JSON. It
restarts only the app process when a Python file under app/ or fluid/
changes. Run it under a Monitor task, never daemonize it: Ctrl-C, SIGTERM,
``down``, parent loss and the TTL all tear everything down.
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
#: streams keep uvicorn waiting; PostgreSQL is a separate process and the data
#: is throwaway, so a forced stop loses nothing the next start needs.
APP_STOP_SECONDS = 4


def default_status_path(root: Path = ROOT) -> Path:
    """Per checkout, under the system temp dir (never the repo)."""
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "ai-shuffle-devloop" / f"{digest}.json"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def read_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status_alive(status: dict) -> bool:
    """The recorded launcher is still the same live process (not a reused pid)."""
    from app.core.temp_scratch import process_identity

    pid, identity = status.get("pid"), status.get("pid_identity")
    if not isinstance(pid, int) or pid <= 1 or identity is None:
        return False
    return process_identity(pid) == identity


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


class Watcher(threading.Thread):
    """Sets ``restart`` when a Python file under app/ or fluid/ changes."""

    def __init__(self, restart: threading.Event, *, poll: bool = False) -> None:
        super().__init__(name="devloop-watch", daemon=True)
        self.restart, self.poll = restart, poll
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
            shown = ", ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
            log.info("change: %s -> restarting the app", shown)
            self.restart.set()

    def stop(self) -> None:
        self.stop_event.set()


class Instance:
    """The status file and the readiness reporting for one ``up``."""

    def __init__(self, args: argparse.Namespace, directory: Path, env: dict, fake: Any,
                 vite_port: int | None) -> None:
        from app.core.temp_scratch import process_identity

        self.args, self.directory, self.env, self.fake = args, directory, env, fake
        self.vite_port = vite_port
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
            "checkout": str(ROOT), "branch": _branch(), "state": "starting",
            "started_at": _iso(self.started), "expires_at": _iso(self.started + args.ttl_seconds),
            "access_link": None,
            "urls": {"ui": f"http://{self.browser}:{vite_port or self.port}/",
                     "vite": f"http://{self.browser}:{vite_port}/" if vite_port else None,
                     "preview_listener": f"http://{self.browser}:{self.port}/"},
            "app": {"pid": None, "starts": 0, "last_ready_seconds": None,
                    "local_port": int(env["PORT"]),
                    "protected_port": int(env["SHUFFLE_PROTECTED_PORT"])},
            "vite": None,
            "fake_ai": {"url": fake.url, "request_log": str(fake.log_path),
                        "scenarios_dir": str(fake.scenarios.directory),
                        "scenarios": sorted(fake.scenarios.load())},
            "scratch_dir": str(directory), "data_dir": env["DATA_DIR"],
            "workspace_dir": env["WORKSPACE_DIR"], "seed": None,
        }

    # ── status file ──
    def write(self, **changes: Any) -> None:
        with self.lock:
            self.data.update(changes)
            path = self.args.status
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2)
            os.replace(tmp, path)

    # ── supervise() hook ──
    def on_app_start(self, process: subprocess.Popen) -> None:
        self.starts += 1
        self.app, self.app_started = process, time.monotonic()
        log.info("app %s (pid %s)", "starting" if self.starts == 1 else "restarting", process.pid)
        with self.lock:
            self.data["app"].update(pid=process.pid, starts=self.starts)
        self.write(state="starting" if self.starts == 1 else "restarting")
        self.wake.set()

    # ── readiness ──
    def on_app_exit(self, process: subprocess.Popen) -> None:
        """supervise(): the app exited on its own (not a restart or teardown)."""
        self.write(state="app-exited")

    def report(self) -> None:
        """Thread: announce every app (re)start once it answers HTTP."""
        while not self.done.is_set():
            self.wake.wait(0.5)
            if not self.wake.is_set():
                continue
            self.wake.clear()
            process, started = self.app, self.app_started
            while not self.done.is_set() and process is self.app and process.poll() is None:
                if answers_http(self.connect, self.port, "/api/preview"):
                    seconds = round(time.monotonic() - started, 1)
                    with self.lock:
                        self.data["app"]["last_ready_seconds"] = seconds
                    if self.starts == 1:
                        self._announce(seconds)
                    else:
                        log.info("app ready again in %.1fs (pid %s)", seconds, process.pid)
                        self.write(state="ready")
                    break
                time.sleep(0.25)

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
    for name in ("port", "vite_port"):
        value = getattr(args, name)
        if value is not None and (not 1024 <= value <= 65535 or value in (8000, 8001)):
            parser.error(f"--{name.replace('_', '-')} must be a free port other than 8000/8001")
    if args.port is not None and args.port == args.vite_port:
        parser.error("--port and --vite-port must differ")
    if not 0 < args.ttl_seconds <= 14400:
        parser.error("--ttl-seconds must be positive and at most four hours")
    if args.no_vite and not (ROOT / "frontend" / "build" / ".shuffle-preview.json").is_file():
        parser.error("--no-vite serves the stamped build: (cd frontend && npm run build) "
                     "&& python -m app.preview stamp")


def up(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if sys.platform != "linux":
        parser.error("The dev loop's owned PostgreSQL lease needs Linux")
    _validate(parser, args)
    existing = read_status(args.status)
    if existing and status_alive(existing):
        parser.error(f"A dev instance for this checkout is already running (pid "
                     f"{existing['pid']}): python -m scripts.devloop down")
    from app.core.temp_scratch import scratch_directory
    from app.preview_run import free_port, preview_environment, supervise
    from docker import container_runtime as runtime
    from docker.container_runtime import stop_group
    from scripts.devloop.fake_ai import FakeAI

    if not args.no_vite:
        _ensure_frontend()
    stopped, restart = threading.Event(), threading.Event()

    def stop(signum: int, frame: Any) -> None:
        log.info("stopping (signal %s)", signum)
        stopped.set()
        runtime.STOP.set()

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    fake = vite = watcher = instance = reporter = None
    try:
        with scratch_directory("ai-shuffle-preview-") as raw:
            directory = Path(raw)
            fake = FakeAI(log_path=directory / "fake-ai-requests.jsonl",
                          latency_ms=args.latency_ms, tps=args.tps)
            port = args.port or free_port()
            protected = free_port(exclude=(port,))
            vite_port = None if args.no_vite else (
                args.vite_port or free_port(exclude=(port, protected)))
            extra = {"SHUFFLE_DEV_FAKE_AI_URL": fake.url,
                     "SHUFFLE_DEV_ACCESS_FILE": str(directory / "access.json"),
                     "SHUFFLE_DEV_SEED_FILE": str(directory / "seed.json")}
            if args.seed != "none":
                extra["SHUFFLE_DEV_SEED"] = args.seed
            if vite_port:
                extra["SHUFFLE_DEV_UI"] = "vite"
            env = preview_environment(
                directory, port, protected, host=str(args.host),
                browser_host=str(args.browser_host or args.host), ttl_seconds=args.ttl_seconds,
                extra=extra,
            )
            database = runtime.Database(
                SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])),
                port=free_port(exclude=(port, protected, int(env["PORT"]), vite_port or 0)),
            )
            instance = Instance(args, directory, env, fake, vite_port)
            log.info("status file: %s", args.status)
            instance.write()
            if vite_port:
                api = f"http://{_url_host(instance.connect)}:{port}"
                vite = subprocess.Popen(_vite_command(str(args.host), vite_port), cwd=FRONTEND,
                                        env=vite_environment(env, api), start_new_session=True)
                instance.data["vite"] = {"pid": vite.pid, "port": vite_port, "proxies_to": api}
                log.info("vite dev (pid %s) on port %s, proxying /api to %s", vite.pid,
                         vite_port, api)
            watcher = Watcher(restart, poll=args.poll)
            watcher.start()
            reporter = threading.Thread(target=instance.report, name="devloop-report",
                                        daemon=True)
            reporter.start()
            code = supervise([sys.executable, "-m", "app.main"], env, database,
                             ttl=args.ttl_seconds, owner_pid=os.getppid(), stopped=stopped,
                             restart=restart, on_start=instance.on_app_start,
                             on_exit=instance.on_app_exit,
                             stop_timeout=APP_STOP_SECONDS)
            log.info("lease ended (app exit code %s); tearing down", code)
            return code
    finally:
        if instance is not None:
            instance.done.set()
        if watcher is not None:
            watcher.stop()
        if vite is not None:
            stop_group(vite, 10)
        if fake is not None:
            fake.close()
        if instance is not None:
            args.status.unlink(missing_ok=True)
        for sig, handler in old.items():
            signal.signal(sig, handler)
        log.info("stopped; app, PostgreSQL, Vite and the fake AI are down")


def down(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    status = read_status(args.status)
    if status is None:
        print(f"No dev instance recorded at {args.status}")
        return 0
    if not status_alive(status):
        print(f"The recorded launcher (pid {status.get('pid')}) is gone; removing its stale "
              "status file")
        args.status.unlink(missing_ok=True)
        return 0
    os.kill(status["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not status_alive(status):
            print(f"Stopped the dev instance (pid {status['pid']})")
            return 0
        time.sleep(0.25)
    print(f"The dev instance (pid {status['pid']}) did not stop within 60s", file=sys.stderr)
    return 1


def show(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    status = read_status(args.status)
    if status is None or not status_alive(status):
        print(f"No running dev instance recorded at {args.status}")
        return 1
    print(json.dumps(status, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.devloop", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up", help="Start a dev instance (run under Monitor)")
    start.add_argument("--host", type=ipaddress.ip_address, default=ipaddress.ip_address("127.0.0.1"),
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
    start.add_argument("--poll", action="store_true",
                       help="Poll for source changes (for filesystems without inotify)")
    for command in (start, commands.add_parser("down", help="Stop the running instance"),
                    commands.add_parser("status", help="Print the running instance's status")):
        command.add_argument("--status", type=Path, default=default_status_path(),
                             help="Status JSON path (default: per checkout, in the temp dir)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[devloop] %(message)s", stream=sys.stdout,
                        force=True)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)  # we log each restart ourselves
    handler = {"up": up, "down": down, "status": show}[args.command]
    return handler(parser, args)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
