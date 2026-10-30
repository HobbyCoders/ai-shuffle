"""Owned Linux preview: one parent/TTL lease owns both the app and PostgreSQL.

No inherited DATA_DIR, credential file or database URL is used. Run under a
Monitor task, never daemonize it. Normal exit, Ctrl-C, SIGTERM, SIGHUP, parent
loss and TTL expiry all run the same teardown. SIGKILL cannot execute Python
finalizers (the dev loop adds a detached guardian for that case).
The dev loop (``python -m scripts.devloop up``) reuses this lease with a
restart hook and its own settings (``DEV_ENV_KEYS``).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from app.core.temp_scratch import process_identity, scratch_directory

if TYPE_CHECKING:
    from docker.container_runtime import Database

logger = logging.getLogger(__name__)

#: The only settings a launcher may add to the controlled preview environment
#: (the dev loop's; see docs/internal/dev-loop.md).
DEV_ENV_KEYS = frozenset({
    "SHUFFLE_DEV_ACCESS_FILE", "SHUFFLE_DEV_FAKE_AI_URL", "SHUFFLE_DEV_FAKE_VOICE_URL",
    "SHUFFLE_DEV_SEED", "SHUFFLE_DEV_SEED_FILE", "SHUFFLE_DEV_UI", "SHUFFLE_DEV_VOICE_ENGINE",
})


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
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The app's whole environment: nothing inherited but PATH/LANG/LD_LIBRARY_PATH.

    ``extra`` may only add the dev loop's settings (:data:`DEV_ENV_KEYS`); it can never replace an
    isolation setting (data dir, home, ports) or smuggle in a credential.
    """
    for key in extra or {}:
        if key not in DEV_ENV_KEYS:
            raise ValueError(f"Preview environment extras must be one of "
                             f"{', '.join(sorted(DEV_ENV_KEYS))}: {key}")
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
    env.update({key: str(value) for key, value in (extra or {}).items()})
    return env


def disposable_preview_data() -> bool:
    """Whether this process runs on a launcher-marked, throwaway data directory.

    Only :func:`preview_environment` writes the marker, outside DATA_DIR and
    naming it exactly. Raw preview mode against caller-managed data, or a real
    install with a stray variable, never qualifies.
    """
    if os.environ.get("AI_SHUFFLE_PREVIEW") != "1":
        return False
    from app.core.config import settings

    data = settings.effective_data_dir.resolve()
    marker = data.parent / ".branch-preview"
    return marker.is_file() and marker.read_text() == str(data)


def _saved_access() -> dict | None:
    """The dev loop's access record, if its grant still verifies (a restart).

    Only on the launcher's own disposable data: anywhere else a stray
    ``SHUFFLE_DEV_ACCESS_FILE`` must not stop a fresh link being minted.
    """
    path = os.environ.get("SHUFFLE_DEV_ACCESS_FILE")
    if not path or not disposable_preview_data():
        return None
    from app.core import preview_access

    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(saved, dict) and preview_access.verify(saved.get("token")) is not None:
        return saved
    return None


def _write_private_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    # Windows has no O_NOFOLLOW; O_CREAT | O_EXCL already refuses any
    # existing entry at tmp (a planted link included) on every platform.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(tmp, path)


def initialize_preview_access() -> None:
    """Publish a preview access link in a token-only preview (AI_SHUFFLE_PREVIEW=1).

    Each start mints a fresh grant on a hidden session and prints its link,
    except a dev-loop restart on the launcher's disposable data whose saved
    grant (``SHUFFLE_DEV_ACCESS_FILE``) still verifies: that link stays valid,
    so nothing new is minted. Owner setup runs only on that disposable data.
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
    if disposable_preview_data():
        data = settings.effective_data_dir.resolve()
        owner_auth.configure_installation()
        database.set_system_setting(
            f"workspace_path:{settings.effective_instance_id}", str(data.parent / "workspace"),
        )
        auth_service.set_onboarding_complete()
        if os.environ.get("SHUFFLE_DEV_SEED"):
            # The seeded agent owns the access session, so a dev instance has
            # no stand-in "Branch preview access" agent in its picker.
            from scripts.devloop.seed import ensure_dev_agent
            ensure_dev_agent()

    # A dev-loop restart keeps the link it already printed: the grant's
    # session and the signing key both survive in the kept data directory.
    saved = _saved_access()
    if saved is not None:
        print("Branch preview access is still valid after the restart", flush=True)
        return

    profiles = get_all_profiles()
    profile_id = profiles[0]["id"] if profiles else "branch-preview-access"
    if not profiles:
        create_profile(profile_id, "Branch preview access", None, {})
    session_id = "branch-preview-" + uuid4().hex
    create_session(session_id, profile_id, title="Branch preview access", hidden=True)
    ttl = int(os.environ["SHUFFLE_PREVIEW_ACCESS_TTL"])
    token = preview_access.mint(session_id, ttl_seconds=ttl)
    url = preview_access.preview_url(session_id, browser_host, port, token=token)
    access_file = os.environ.get("SHUFFLE_DEV_ACCESS_FILE")
    if os.environ.get("SHUFFLE_DEV_UI") == "vite":
        # The listener serves no UI here; the launcher prints the Vite link.
        print("Branch preview access minted; the dev loop prints the link to open", flush=True)
    else:
        print(f"Branch preview access (open once in the agent browser): {url}", flush=True)
    if access_file:
        _write_private_json(Path(access_file), {
            "session_id": session_id, "token": token, "url": url,
            "expires_at": int(time.time()) + ttl,
        })


async def seed_dev_instance() -> None:
    """Seed a dev-loop instance (``SHUFFLE_DEV_SEED``) through the app's own code.

    Refuses anything but the launcher's disposable data directory, exactly
    like the owner setup above: the seed creates an owner-visible provider,
    projects and chats and must never touch a real install.
    """
    name = os.environ.get("SHUFFLE_DEV_SEED")
    if not name:
        return
    if not disposable_preview_data():
        raise RuntimeError(
            "SHUFFLE_DEV_SEED is only honoured on a launcher-owned disposable preview; "
            "refusing to seed this data directory"
        )
    from scripts.devloop.seed import seed

    await seed(name)


def supervise(
    command: Sequence[str], env: Mapping[str, str], database: Database, *, ttl: float,
    owner_pid: int, stopped: threading.Event | None = None,
    restart: threading.Event | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_exit: Callable[[subprocess.Popen], None] | None = None,
    stop_timeout: float = 15,
) -> int:
    """Testable lifecycle boundary. The caller exclusively owns the database.

    With ``restart`` (the dev loop), setting the event replaces only the app
    process; PostgreSQL and the data directory stay. Changes signalled while
    the old process is stopping fold into the same restart (the new process
    loads the latest code anyway). An app that exits on its own (a syntax
    error mid-edit) waits for the next ``restart`` instead of ending the lease.
    ``on_start`` sees every app process started; ``on_exit`` sees one that
    exited on its own while restarts are on. ``stop_timeout`` bounds each
    graceful stop before SIGKILL (open streams hold uvicorn's graceful
    shutdown); a disposable dev instance can afford a short one.
    """
    from docker.container_runtime import stop_group

    owner_start = process_identity(owner_pid)
    if owner_pid <= 1 or owner_start is None or not 0 < ttl <= 14400:
        raise ValueError("Preview requires a live owner and TTL of at most four hours")
    stopped = stopped or threading.Event()
    process = None
    deadline = time.monotonic() + ttl
    step = 0.5 if restart is None else 0.2

    def lease_over() -> bool:
        return (stopped.wait(min(step, max(0, deadline - time.monotonic())))
                or time.monotonic() >= deadline
                or process_identity(owner_pid) != owner_start)

    try:
        database.open()
        database.publish_descriptor()
        while True:
            process = subprocess.Popen(command, env=env, start_new_session=True)
            if on_start is not None:
                on_start(process)
            over = False
            while process.poll() is None and not (restart is not None and restart.is_set()):
                if lease_over():
                    over = True
                    break
            if over or restart is None:
                break
            if not restart.is_set():
                logger.warning("Preview app exited with code %s; waiting for a source change "
                               "to restart it", process.poll())
                if on_exit is not None:
                    on_exit(process)
                while not restart.is_set():
                    if lease_over():
                        over = True
                        break
                if over:
                    break
            stop_group(process, stop_timeout)
            restart.clear()
        return process.poll() if process.poll() is not None else 0
    finally:
        try:
            stop_group(process, stop_timeout)
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
    # SIGHUP too: the terminal or a Monitor's pty going away is a stop.
    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
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
