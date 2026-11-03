"""Ask PostgreSQL about backends from a disposable child process.

The question exists because something in this process may have lost a connection,
often through a stray close of a descriptor number, so it cannot be answered from
this process. libpq can block here holding the GIL, on a descriptor another thread
replaced, and then no clock, loop or watchdog in this process runs again. A child
shares none of that, and the kernel can always end it. Every failure here means no
answer, which means the checkout stays counted.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import psycopg2

logger = logging.getLogger(__name__)

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
    """At most one child at a time, for one pool. Call read() off the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen] = None
        self._closed = False

    def read(self, pids: list[int], args: tuple, kwargs: dict[str, Any], seconds: float) -> dict:
        """What PostgreSQL shows for these backends, or an exception within the bound."""
        request = json.dumps({
            "path": [str(Path(psycopg2.__file__).parents[1])], "pids": pids, "args": list(args),
            "kwargs": {**kwargs, "application_name": "ai-shuffle-hold-check"},
        }).encode()
        child = self._start(seconds)
        try:
            output, _ = child.communicate(request, timeout=seconds)
        except subprocess.TimeoutExpired:
            raise TimeoutError("backend probe deadline") from None
        finally:
            self._end(child)
        return _parse(output)

    def close(self) -> None:
        """End any running child and start no more."""
        with self._lock:
            self._closed = True
            if self._child is not None:
                self._child.kill()

    def _start(self, seconds: float) -> subprocess.Popen:
        command = [_interpreter(), "-I", "-S", str(_CHILD), str(int(seconds) + 2)]
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        with self._lock:
            if self._closed:
                raise NoAnswer("closed")
            if self._child is not None:
                # It outlived its kill, so it is still ours to end: no second one meanwhile.
                self._child.kill()
                try:
                    self._child.wait(timeout=_REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    raise NoAnswer("previous child still running") from None
            self._child = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
                env=_environment(), **options)
            return self._child

    def _end(self, child: subprocess.Popen) -> None:
        try:
            child.kill()  # No effect once it has exited and been reaped.
            child.communicate(timeout=_REAP_SECONDS)  # Reaps it and closes its pipes.
        except (subprocess.TimeoutExpired, ValueError, OSError):
            logger.warning("Backend probe process %s did not end after being killed", child.pid)
            return  # Stays in self._child, which refuses another until this one is gone.
        with self._lock:
            if self._child is child:
                self._child = None


def _parse(output: Optional[bytes]) -> dict:
    if not output or len(output) > _MAX_ANSWER_BYTES:
        raise NoAnswer("no output")
    answer = json.loads(output)
    if "rows" not in answer:
        raise NoAnswer(str(answer.get("error", "unknown"))[:64])
    return {
        row[0]: {"state": row[1], "wait_type": row[2], "wait": row[3], "tag": row[4],
                 "state_age": row[5] or 0.0}
        for row in answer["rows"]
    }
