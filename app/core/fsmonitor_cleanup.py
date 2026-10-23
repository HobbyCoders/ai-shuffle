"""One-time repair of the repo-local fsmonitor setting formerly enabled by the app."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fluid.tools.child_env import scrub_child_env

from app.core.config import settings
from app.core.worktree_report import _project_repos
from app.db import database

logger = logging.getLogger(__name__)
MARKER = "fsmonitor_cleanup_v1"
COMMAND_TIMEOUT = 5
TOTAL_TIMEOUT = 60


def _git(path: str, *args: str, deadline: float, cancelled: threading.Event):
    remaining = deadline - time.monotonic()
    if cancelled.is_set() or remaining <= 0:
        raise TimeoutError("fsmonitor cleanup budget exhausted")
    # No PIPE: on Windows a timed-out git.exe redirector can leave a child
    # holding the pipe open, making subprocess.run's final communicate unbounded.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", path, *args],
            check=False, stdout=output, stderr=subprocess.STDOUT, env=scrub_child_env(),
            timeout=min(COMMAND_TIMEOUT, remaining),
        )
        output.seek(0)
        return result.returncode, output.read().decode("utf-8", errors="replace")


def _cleanup(cancelled: threading.Event) -> None:
    if sys.platform not in {"win32", "darwin"}:
        return
    fixed = stopped = failures = 0
    complete = skipped = False
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        key = f"{MARKER}:{settings.effective_instance_id}"
        state = database.get_system_setting(key)
        if state == "1":
            skipped = True
            return
        pending = set(json.loads(state or "[]"))

        def stop_pending():
            nonlocal stopped
            for worktree in sorted(pending.copy()):
                code, message = _git(worktree, "fsmonitor--daemon", "stop",
                                     deadline=deadline, cancelled=cancelled)
                if "not running" not in message.lower():
                    if code != 0:
                        raise RuntimeError("daemon stop failed")
                    stopped += 1
                pending.remove(worktree)
            database.set_system_setting(key, json.dumps(sorted(pending)))

        # A previous boot may have exited after unsetting but before all stops.
        if pending:
            stop_pending()
        seen = set()
        for source in _project_repos():
            # Registry-only worktrees still matter when the original root moved.
            candidates = [source["path"], *(w["path"] for w in source["registered"])]
            for path in candidates:
                path = os.path.normcase(os.path.abspath(path))
                if path in seen or not (Path(path) / ".git").exists():
                    continue
                try:
                    def run(*args, repo=path):
                        return _git(repo, *args, deadline=deadline, cancelled=cancelled)

                    code, value = run("config", "--local", "--get-all", "core.fsmonitor")
                    if code == 1:  # No local value, even if global config enables it.
                        seen.add(path)
                        continue
                    if code != 0:
                        raise RuntimeError("config read failed")
                    if value.splitlines() != ["true"]:
                        seen.add(path)
                        continue
                    # -z keeps spaces, newlines and non-ASCII worktree paths literal.
                    code, listing = run("worktree", "list", "--porcelain", "-z")
                    if code != 0:
                        raise RuntimeError("worktree listing failed")
                    paths = {os.path.normcase(os.path.abspath(p)) for p in
                             [path, *(field[9:] for field in listing.split("\0")
                                      if field.startswith("worktree "))]}
                    # Journal before unsetting so interruption cannot strand a
                    # daemon whose repo no longer has a discoverable true value.
                    pending.update(paths)
                    database.set_system_setting(key, json.dumps(sorted(pending)))
                    code, _ = run("config", "--local", "--unset", "core.fsmonitor")
                    if code != 0:
                        raise RuntimeError("config unset failed")
                    fixed += 1
                    stop_pending()
                    seen.update(os.path.normcase(os.path.abspath(p)) for p in paths)
                except Exception:  # noqa: BLE001 - best-effort migration; one summary below
                    failures += 1
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("fsmonitor cleanup interrupted")
        if not failures and not pending and not cancelled.is_set() and time.monotonic() < deadline:
            database.set_system_setting(key, "1")
            complete = True
    except Exception:  # noqa: BLE001 - must never fail startup, including database errors
        failures += 1
    finally:
        if not skipped:
            logger.info("Fsmonitor cleanup: repos fixed=%d, daemons stopped=%d, failures=%d, complete=%s",
                        fixed, stopped, failures, complete)


async def cleanup_once() -> None:
    """Background startup task; no Git or database work on the event loop."""
    cancelled = threading.Event()
    try:
        await asyncio.wait_for(asyncio.to_thread(_cleanup, cancelled), timeout=TOTAL_TIMEOUT)
    except TimeoutError:
        # Also bound the task if registry/database access is slow. The worker
        # checks cancellation before further Git calls or recording completion.
        pass
    finally:
        cancelled.set()
