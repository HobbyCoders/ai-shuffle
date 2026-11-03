"""Ask PostgreSQL about backends from a disposable child process.

The question exists because something in this process may have lost a connection,
often through a stray close of a descriptor number, so it cannot be answered from
this process. libpq can block here holding the GIL, on a descriptor another thread
replaced, and then no clock, loop or watchdog in this process runs again. A child
shares none of that, and the kernel can always end it. Every failure here means no
answer, which means the checkout stays counted.

Using a pipe has the same weakness on a smaller scale: its number can change hands too,
and then a read can wait on a stranger forever. So every call that uses a descriptor
number runs on a throwaway thread, and read() itself uses only the clock and the process
id. Nothing a pipe does can delay the kill, the reap or read() returning.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
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
# A stranded exchange thread cannot be ended, so this is what bounds them. Each one takes
# a stray close landing inside a window of milliseconds, and ends if the stranger's peer
# ever closes. With this many stranded, no more checks run and checkouts stay counted.
_MAX_STRANDED_EXCHANGES = 8


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
        self._exchanges = threading.BoundedSemaphore(_MAX_STRANDED_EXCHANGES)

    def read(self, pids: list[int], args: tuple, kwargs: dict[str, Any], seconds: float) -> dict:
        """What PostgreSQL shows for these backends, or an exception within the bound."""
        request = json.dumps({
            "path": [str(Path(psycopg2.__file__).parents[1])], "pids": pids, "args": list(args),
            "kwargs": {**kwargs, "application_name": "ai-shuffle-hold-check"},
        }).encode()
        self._end_previous()
        deadline = time.monotonic() + seconds
        started = threading.Event()
        # Only this call looks at it, so whatever a stranded thread adds later is ignored.
        exchange: dict[str, Any] = {}

        def run() -> None:
            try:
                exchange["child"] = self._start(seconds)
                started.set()
                exchange["output"] = exchange["child"].communicate(request)[0]
            except Exception as exc:  # noqa: BLE001 - the type is all anything ever reports
                exchange["error"] = type(exc).__name__
            finally:
                started.set()
                self._exchanges.release()

        if not self._exchanges.acquire(blocking=False):
            logger.warning("Long database checkouts cannot be checked: %d probe reads are stuck",
                           _MAX_STRANDED_EXCHANGES)
            raise NoAnswer("stranded")
        thread = threading.Thread(target=run, name="db-hold-check-pipe", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._exchanges.release()
            raise
        if not started.wait(seconds):
            raise TimeoutError("backend probe deadline")
        child = exchange.get("child")
        if child is None:
            raise NoAnswer(exchange.get("error", "unknown"))
        self._adopt(child)
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError("backend probe deadline") from None
        finally:
            self._end(child)
        thread.join(_REAP_SECONDS)  # The child is gone, so its pipe has ended.
        if "output" not in exchange:
            raise NoAnswer(exchange.get("error", "stranded"))
        return _parse(exchange["output"], pids)

    def close(self) -> None:
        """End any running child and start no more."""
        with self._lock:
            self._closed = True
            if self._child is not None:
                self._child.kill()

    def _end_previous(self) -> None:
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
                self._child = None

    def _start(self, seconds: float) -> subprocess.Popen:
        command = [_interpreter(), "-I", "-S", str(_CHILD), str(int(seconds) + 2)]
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
            env=_environment(), **options)

    def _adopt(self, child: subprocess.Popen) -> None:
        with self._lock:
            self._child = child
            if self._closed:
                child.kill()

    def _end(self, child: subprocess.Popen) -> None:
        try:
            child.kill()  # No effect once it has exited and been reaped.
            child.wait(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Backend probe process %s did not end after being killed", child.pid)
            return  # Stays in self._child, which refuses another until this one is gone.
        with self._lock:
            if self._child is child:
                self._child = None


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_row(row: Any, pids: list[int]) -> bool:
    return (isinstance(row, list) and len(row) == 6 and row[0] in pids
            and type(row[0]) is int
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
