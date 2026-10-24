"""Owned Linux preview: one parent/TTL lease owns both the app and PostgreSQL.

No inherited DATA_DIR, credential file or database URL is used. Run under a
Monitor task, never daemonize it. Normal exit, Ctrl-C, SIGTERM, parent loss and
TTL expiry all run the same teardown. SIGKILL cannot execute Python finalizers.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence
from uuid import uuid4

from app.core.temp_scratch import process_identity, scratch_directory

if TYPE_CHECKING:
    from docker.container_runtime import Database


def free_port(*, exclude: Iterable[int] = ()) -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in (8000, 8001, *exclude):
            return port


def preview_environment(
    directory: str | Path, port: int, protected_port: int, *, host: str = "127.0.0.1",
    browser_host: str | None = None, ttl_seconds: int = 3600,
) -> dict[str, str]:
    directory = Path(directory)
    home = directory / "home"
    home.mkdir()
    workspace = directory / "workspace"
    workspace.mkdir()
    data = directory / "data"
    # Keep the lease marker outside DATA_DIR: PostgreSQL refuses nonempty,
    # unrecognized data directories on first boot.
    (directory / ".branch-preview").write_text(str(data.resolve()))
    empty = directory / "empty.env"
    empty.write_text("")
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "LANG", "LD_LIBRARY_PATH")}
    env.update(HOME=str(home), DATA_DIR=str(directory / "data"),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), WORKSPACE_DIR=str(workspace),
               LOCAL_MODE="true", AI_SHUFFLE_PREVIEW="1", AI_SHUFFLE_ENV_FILE=str(empty),
               HOST="127.0.0.1", PORT=str(free_port(exclude=(port, protected_port))),
               START_CHROME="0", SHUFFLE_DESKTOP_ISOLATED="1",
               SHUFFLE_PROTECTED_PORT=str(protected_port), SHUFFLE_PREVIEW_PORT=str(port),
               SHUFFLE_PREVIEW_HOST=host, SHUFFLE_PREVIEW_ACCESS_HOST=browser_host or host,
               SHUFFLE_PREVIEW_ACCESS_TTL=str(ttl_seconds),
               PYTHONPATH=str(root) + os.pathsep + str(root / "fluid"))
    return env


def initialize_preview_access() -> None:
    """Publish a fresh link only in the launcher's disposable, token-only preview.

    Called off the event loop after schema setup. The real hidden owner session
    keeps normal preview grant verification (including session existence) intact.
    No key or session from the installed worker is copied or trusted here.
    """
    browser_host = os.environ.get("SHUFFLE_PREVIEW_ACCESS_HOST")
    if os.environ.get("AI_SHUFFLE_PREVIEW") != "1" or not browser_host:
        return
    from app.core import preview_access
    from app.db.profiles import create_profile, get_all_profiles
    from app.db.sessions import create_session

    port = preview_access.listener_port()
    if port is None:
        raise RuntimeError("Branch preview requires its token-only listener")

    from app.core import owner_auth
    from app.core.auth import auth_service
    from app.core.config import settings
    from app.db import database

    # Only the owned launcher marks its fresh data directory. Raw preview mode
    # against caller-managed data must not silently configure an installation.
    data = settings.effective_data_dir.resolve()
    marker = data.parent / ".branch-preview"
    if marker.is_file() and marker.read_text() == str(data):
        owner_auth.configure_installation()
        database.set_system_setting(
            f"workspace_path:{settings.effective_instance_id}", str(data.parent / "workspace"),
        )
        auth_service.set_onboarding_complete()

    profiles = get_all_profiles()
    profile_id = profiles[0]["id"] if profiles else "branch-preview-access"
    if not profiles:
        create_profile(profile_id, "Branch preview access", None, {})
    session_id = "branch-preview-" + uuid4().hex
    create_session(session_id, profile_id, title="Branch preview access", hidden=True)
    ttl = int(os.environ["SHUFFLE_PREVIEW_ACCESS_TTL"])
    token = preview_access.mint(session_id, ttl_seconds=ttl)
    url = preview_access.preview_url(session_id, browser_host, port, token=token)
    print(f"Branch preview access (open once in the agent browser): {url}", flush=True)


def supervise(
    command: Sequence[str], env: Mapping[str, str], database: Database, *, ttl: float,
    owner_pid: int, stopped: threading.Event | None = None,
) -> int:
    """Testable lifecycle boundary. The caller exclusively owns the database."""
    from docker.container_runtime import stop_group

    owner_start = process_identity(owner_pid)
    if owner_pid <= 1 or owner_start is None or not 0 < ttl <= 14400:
        raise ValueError("Preview requires a live owner and TTL of at most four hours")
    stopped = stopped or threading.Event()
    process = None
    deadline = time.monotonic() + ttl
    try:
        database.open()
        database.publish_descriptor()
        process = subprocess.Popen(command, env=env, start_new_session=True)
        while process.poll() is None:
            if (stopped.wait(min(0.5, max(0, deadline - time.monotonic())))
                    or time.monotonic() >= deadline
                    or process_identity(owner_pid) != owner_start):
                break
        return process.poll() if process.poll() is not None else 0
    finally:
        try:
            stop_group(process, 15)
        finally:
            database.close()


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "linux":
        raise RuntimeError("Owned PostgreSQL previews require Linux")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="Token-only preview listener port")
    parser.add_argument("--host", type=ipaddress.ip_address, default="127.0.0.1",
                        help="Preview bind address; use 0.0.0.0 for a remote agent browser")
    parser.add_argument("--browser-host", type=ipaddress.ip_address,
                        help="Reachable address for the access link; required for wildcard binds")
    parser.add_argument("--protected-port", type=int)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    browser_host = args.browser_host or args.host
    if browser_host.is_unspecified:
        parser.error("--browser-host must name a reachable address for a wildcard --host")
    protected = args.protected_port or free_port(exclude=(args.port,))
    if (not 1024 <= args.port <= 65535 or not 1024 <= protected <= 65535
            or args.port in (8000, 8001) or protected in (8000, 8001, args.port)
            or not 0 < args.ttl_seconds <= 14400):
        parser.error("Use distinct isolated ports and a positive TTL of at most four hours")
    from docker import container_runtime as runtime

    stopped = threading.Event()
    def stop(signum: int, frame: FrameType | None) -> None:
        stopped.set()
        runtime.STOP.set()
    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with scratch_directory("ai-shuffle-preview-") as directory:
            env = preview_environment(
                directory, args.port, protected, host=str(args.host),
                browser_host=str(browser_host), ttl_seconds=args.ttl_seconds,
            )
            database = runtime.Database(
                SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])),
                port=free_port(exclude=(args.port, protected, int(env["PORT"]))),
            )
            return supervise([sys.executable, "-m", "app.main"], env, database,
                             ttl=args.ttl_seconds, owner_pid=os.getppid(), stopped=stopped)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
