"""Ask PostgreSQL from an isolated child: no answer keeps the checkout counted.

libpq can freeze an interpreter on a replaced descriptor. Pipes in the parent have
that same weakness. Instead, the child opens private files; the parent maps the
reply before launch and closes every file before starting the lifecycle owner.
That owner only spawns, polls, signals and reaps. Even a delayed spawn cannot leave
a reader or closer with authority over a descriptor reused after read() returns.
"""
from __future__ import annotations

import json
import math
import mmap
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import psycopg2

_CHILD = Path(__file__).with_name("backend_probe_child.py")
# Only what libpq reads and the OS needs: this process's environment holds API keys.
_ENVIRONMENT = ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LD_LIBRARY_PATH", "HOME",
                "APPDATA")
_REAP_SECONDS = 2.0
_MAX_ANSWER_BYTES = 1024 * 1024


class NoAnswer(Exception):
    """The probe has nothing to say. The message is a type name, never a value."""


def _environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k.upper() in _ENVIRONMENT or k.upper().startswith("PG")}


def _interpreter() -> str:
    # A Windows venv's python.exe only starts the real one, which a kill would miss.
    base = getattr(sys, "_base_executable", None) if os.name == "nt" else None
    return base if base and os.path.isfile(base) else sys.executable


class BackendProbe:
    """One lifecycle owner and at most one child per pool. read() is off the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen] = None
        self._owner: Optional[threading.Thread] = None
        self._closed = threading.Event()

    def read(self, pids: list[int], args: tuple, kwargs: dict[str, Any], seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        with tempfile.TemporaryDirectory(prefix="db-hold-check-") as directory:
            # mkstemp uses mode 0600, inside a mode-0700 directory. On Windows these
            # inherit the user's temp-directory ACL. No credential goes in argv.
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as request:
                request.write(json.dumps({
                    "path": [str(Path(psycopg2.__file__).parents[1])], "pids": pids,
                    "args": list(args),
                    "kwargs": {**kwargs, "application_name": "ai-shuffle-hold-check"},
                }).encode())
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as reply:
                reply.truncate(_MAX_ANSWER_BYTES)
                answer = mmap.mmap(reply.fileno(), _MAX_ANSWER_BYTES, access=mmap.ACCESS_READ)
            # No open file is passed to Popen; the mapping needs no read syscall.
            with answer:
                done = threading.Event()
                errors: list[str] = []

                def run() -> None:
                    child = None
                    try:
                        child = self._start(seconds, request.name, reply.name)
                        self._child = child
                        while child.poll() is None:
                            if self._closed.is_set() or time.monotonic() >= deadline:
                                errors.append("TimeoutError")
                                break
                            time.sleep(0.01)
                    except Exception as exc:  # noqa: BLE001 - report only a type
                        errors.append(type(exc).__name__)
                    finally:
                        if child is not None:
                            # Only this owner ever reaps or signals, including close().
                            if child.poll() is None:
                                child.kill()
                            child.wait()
                            self._child = None
                        done.set()

                with self._lock:
                    if self._closed.is_set():
                        raise NoAnswer("closed")
                    if self._child is not None or (self._owner is not None and self._owner.is_alive()):
                        raise NoAnswer("previous child still running")
                    self._owner = threading.Thread(target=run, name="db-hold-check-child", daemon=True)
                    self._owner.start()
                if not done.wait(max(0.0, deadline - time.monotonic()) + _REAP_SECONDS):
                    raise TimeoutError("backend probe deadline")
                if "TimeoutError" in errors:
                    raise TimeoutError("backend probe deadline")
                if errors:
                    raise NoAnswer(errors[0])
                return _parse(answer[:].rstrip(b"\0"), pids)

    def close(self) -> None:
        """Request shutdown without competing with the lifecycle owner's signal/reap."""
        with self._lock:
            self._closed.set()

    def _start(self, seconds: float, request: str, reply: str) -> subprocess.Popen:
        command = [_interpreter(), "-I", "-S", str(_CHILD), str(int(seconds) + 2), request, reply]
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        # POSIX: force Popen's posix_spawn path, not fork/exec's blocking error-pipe
        # read. Python's descriptors are non-inheritable (PEP 446). Windows uses
        # CreateProcess directly, with native handles rather than CRT descriptor I/O.
        if os.name != "nt" and not subprocess._USE_POSIX_SPAWN:
            raise NoAnswer("posix_spawn unavailable")
        return subprocess.Popen(command, close_fds=False, env=_environment(), **options)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_row(row: Any, pids: list[int]) -> bool:
    return (isinstance(row, list) and len(row) == 6 and row[0] in pids
            and type(row[0]) is int and isinstance(row[4], str) and bool(row[4])
            and all(value is None or isinstance(value, str) for value in row[1:5])
            and (row[5] is None or _is_number(row[5])))


def _parse(output: Optional[bytes], pids: list[int]) -> dict:
    """The whole reply or nothing: an empty answer means those backends exited, so only a
    complete, well-formed one may say so."""
    if not output or len(output) > _MAX_ANSWER_BYTES:
        raise NoAnswer("no output")
    try:
        answer = json.loads(output)
    except (ValueError, RecursionError):
        raise NoAnswer("malformed") from None
    if not isinstance(answer, dict):
        raise NoAnswer("malformed")
    if "rows" not in answer:
        raise NoAnswer(str(answer.get("error", "unknown"))[:64])
    rows = answer["rows"]
    if not isinstance(rows, list) or not all(_is_row(row, pids) for row in rows):
        raise NoAnswer("malformed")
    return {
        row[0]: {"state": row[1], "wait_type": row[2], "wait": row[3], "tag": row[4],
                 "state_age": row[5] or 0.0}
        for row in rows
    }
