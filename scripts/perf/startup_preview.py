"""Owned disposable preview for startup-repeat.mjs. Never starts a live worker."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from app.core.temp_scratch import scratch_directory
from app.preview_run import free_port, preview_environment, supervise
from docker import container_runtime as runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--seed", choices=("long", "none"), default="long")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in (8000, 8001, 8002):
        parser.error("choose a private preview port, never the live worker")
    stopped, restart = threading.Event(), threading.Event()

    def stop(*_):
        stopped.set()
        runtime.STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def commands():
        for line in sys.stdin:
            if line.strip() == "restart":
                restart.set()
            elif line.strip() == "stop":
                stop()
        stop()  # parent closed the pipe; do not leave a server behind

    threading.Thread(target=commands, daemon=True).start()
    with scratch_directory("ai-shuffle-preview-") as directory:
        extra = {"SHUFFLE_DEV_SEED": args.seed} if args.seed != "none" else None
        env = preview_environment(directory, args.port, free_port(exclude=(args.port,)),
                                  ttl_seconds=2400, extra=extra)
        db = runtime.Database(
            SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])),
            port=free_port(exclude=(args.port, int(env["PORT"]), int(env["SHUFFLE_PROTECTED_PORT"]))))

        def on_start(child):
            print("@PERF_APP_SPAWN@", child.pid, flush=True)

        def on_exit(child):
            print("@PERF_APP_EXIT@", child.returncode, flush=True)

        return supervise([sys.executable, "-m", "app.main"], env, db, ttl=2400,
                         owner_pid=os.getppid(), stopped=stopped, restart=restart,
                         on_start=on_start, on_exit=on_exit)


if __name__ == "__main__":
    raise SystemExit(main())
