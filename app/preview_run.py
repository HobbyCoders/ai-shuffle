"""Owned Linux preview: one parent/TTL lease owns both the app and PostgreSQL.

No inherited DATA_DIR, credential file or database URL is used. Run under a
Monitor task, never daemonize it. Normal exit, Ctrl-C, SIGTERM, parent loss and
TTL expiry all run the same teardown. SIGKILL cannot execute Python finalizers.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app.core.temp_scratch import process_identity, scratch_directory


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def preview_environment(directory, port, protected_port):
    directory = Path(directory)
    home = directory / "home"
    home.mkdir()
    workspace = directory / "workspace"
    workspace.mkdir()
    empty = directory / "empty.env"
    empty.write_text("")
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "LANG", "LD_LIBRARY_PATH")}
    env.update(HOME=str(home), DATA_DIR=str(directory / "data"),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), WORKSPACE_DIR=str(workspace),
               LOCAL_MODE="true", AI_SHUFFLE_PREVIEW="1", AI_SHUFFLE_ENV_FILE=str(empty),
               HOST="127.0.0.1", PORT=str(port), START_CHROME="0",
               SHUFFLE_DESKTOP_ISOLATED="1", SHUFFLE_PROTECTED_PORT=str(protected_port),
               SHUFFLE_PREVIEW_PORT="off", PYTHONPATH=str(root) + os.pathsep + str(root / "fluid"))
    return env


def supervise(command, env, database, *, ttl, owner_pid, stopped=None):
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


def main(argv=None):
    if sys.platform != "linux":
        raise RuntimeError("Owned PostgreSQL previews require Linux")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--protected-port", type=int)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    protected = args.protected_port or free_port()
    if (not 1024 <= args.port <= 65535 or not 1024 <= protected <= 65535
            or args.port in (8000, 8001) or protected in (8000, 8001, args.port)
            or not 0 < args.ttl_seconds <= 14400):
        parser.error("Use distinct isolated ports and a positive TTL of at most four hours")
    from docker import container_runtime as runtime

    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
        runtime.STOP.set()
    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with scratch_directory("ai-shuffle-preview-") as directory:
            env = preview_environment(directory, args.port, protected)
            database = runtime.Database(SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])), port=free_port())
            return supervise([sys.executable, "-m", "app.main"], env, database,
                             ttl=args.ttl_seconds, owner_pid=os.getppid(), stopped=stopped)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
