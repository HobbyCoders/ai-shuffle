"""Event-loop stall watchdog.

A plain OS thread (not a coroutine — it has to keep working while the loop is
frozen) pings the asyncio loop once a second with ``loop.call_soon_threadsafe``
and measures how long the loop takes to answer. When an answer is overdue by
more than ``LOOP_STALL_THRESHOLD_S`` (default 3 s) the watchdog logs the loop
thread's current Python stack at ERROR — that is the frame that is blocking
the loop, which is exactly what an after-the-fact log needs to say. While the
stall persists it re-dumps every ``redump_interval_s`` (30 s); when the loop
finally answers it logs the stall duration.

Why a thread and not ``resource_monitor``'s loop-lag metric: that metric is a
coroutine, so it can only report a stall after the loop is back, and only as a
number. This watchdog captures *where* the loop was stuck, while it is stuck.

The stack is taken with ``sys._current_frames()`` and formatted with
``traceback.format_stack`` so it goes through the normal app logger (file
handler + error_log). ``faulthandler`` would bypass the logger's handler
locks — safer if the loop is frozen *inside* a logging handler — so the dump
is also written with ``faulthandler.dump_traceback`` to the file handler's raw
stream when one is installed (best effort, all threads).
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_S = 3.0
DEFAULT_PING_INTERVAL_S = 1.0
DEFAULT_REDUMP_INTERVAL_S = 30.0


def _threshold_from_env() -> float:
    raw = os.environ.get("LOOP_STALL_THRESHOLD_S", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "LOOP_STALL_THRESHOLD_S=%r is not a number; using %.1fs", raw, DEFAULT_THRESHOLD_S
        )
        return DEFAULT_THRESHOLD_S
    return value if value > 0 else DEFAULT_THRESHOLD_S


class LoopStallWatchdog:
    """Ping the loop from a daemon thread; dump the loop thread's stack on a stall."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold_s: Optional[float] = None,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        redump_interval_s: float = DEFAULT_REDUMP_INTERVAL_S,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._loop = loop
        self.threshold_s = threshold_s if threshold_s is not None else _threshold_from_env()
        self.ping_interval_s = ping_interval_s
        self.redump_interval_s = redump_interval_s
        self._log = log or logger

        self._loop_thread_id: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Monotonic time of the oldest unanswered ping, or None when the loop
        # has answered everything sent so far.
        self._pending_since: Optional[float] = None
        self._stalled = False
        self._stall_started_at: float = 0.0
        self._last_dump_at: float = 0.0
        # Observability for tests / the resource monitor.
        self.stall_count = 0
        self.dump_count = 0
        self.last_stall_seconds: Optional[float] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the watchdog thread. Must be called from the loop's thread."""
        if self._thread is not None:
            return
        self._loop_thread_id = threading.get_ident()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="loop-stall-watchdog", daemon=True
        )
        self._thread.start()
        self._log.info(
            "Event-loop stall watchdog started (threshold %.1fs, ping every %.1fs)",
            self.threshold_s, self.ping_interval_s,
        )

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None

    @property
    def stalled(self) -> bool:
        with self._lock:
            return self._stalled

    # ------------------------------------------------------------------
    # ping / pong
    # ------------------------------------------------------------------

    def _pong(self) -> None:
        """Runs on the loop thread: the loop got around to our callback."""
        with self._lock:
            self._pending_since = None

    def _send_ping(self, now: float) -> None:
        with self._lock:
            if self._pending_since is not None:
                return  # one outstanding ping is enough; age it instead
            self._pending_since = now
        try:
            self._loop.call_soon_threadsafe(self._pong)
        except RuntimeError:
            # Loop closed — nothing to watch any more.
            with self._lock:
                self._pending_since = None
            self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.ping_interval_s):
            now = time.monotonic()
            self._check(now)
            self._send_ping(now)

    def _check(self, now: float) -> None:
        with self._lock:
            pending_since = self._pending_since
            stalled = self._stalled
            stall_started_at = self._stall_started_at
            last_dump_at = self._last_dump_at

        if pending_since is None:
            if stalled:
                duration = now - stall_started_at
                with self._lock:
                    self._stalled = False
                    self.last_stall_seconds = duration
                self._log.error(
                    "Event loop stall ended after %.1fs (threshold %.1fs)",
                    duration, self.threshold_s,
                )
            return

        age = now - pending_since
        if not stalled:
            if age < self.threshold_s:
                return
            with self._lock:
                self._stalled = True
                self._stall_started_at = pending_since
                self._last_dump_at = now
                self.stall_count += 1
            self._dump(age, first=True)
            return

        if now - last_dump_at >= self.redump_interval_s:
            with self._lock:
                self._last_dump_at = now
            self._dump(age, first=False)

    # ------------------------------------------------------------------
    # stack dump
    # ------------------------------------------------------------------

    def format_loop_stack(self) -> str:
        """The loop thread's current Python stack, innermost frame last."""
        frames = sys._current_frames()
        frame = frames.get(self._loop_thread_id) if self._loop_thread_id is not None else None
        if frame is None:
            return "<loop thread frame unavailable>"
        return "".join(traceback.format_stack(frame))

    def _dump(self, age: float, *, first: bool) -> None:
        with self._lock:
            self.dump_count += 1
        stack = self.format_loop_stack()
        self._log.error(
            "Event loop %s for %.1fs (threshold %.1fs) — loop thread stack "
            "(innermost last):\n%s",
            "stalled" if first else "still stalled", age, self.threshold_s, stack,
        )
        self._faulthandler_dump()

    def _faulthandler_dump(self) -> None:
        """Best-effort raw dump of every thread to the app log file's stream.

        Bypasses the logging machinery — useful if the loop thread froze while
        holding a logging handler lock, in which case the ERROR above is itself
        blocked until the stall ends.
        """
        try:
            from app.core import file_logging

            handler = getattr(file_logging, "_file_handler", None)
            stream = getattr(handler, "stream", None)
            if stream is None:
                return
            stream.write("\n--- loop-stall-watchdog: faulthandler dump of all threads ---\n")
            stream.flush()
            faulthandler.dump_traceback(file=stream, all_threads=True)
            stream.flush()
        except Exception:  # noqa: BLE001 — never let the dump path raise
            pass


# ----------------------------------------------------------------------
# process-wide singleton (started from app.main's lifespan)
# ----------------------------------------------------------------------

_watchdog: Optional[LoopStallWatchdog] = None


def start_loop_watchdog(
    loop: Optional[asyncio.AbstractEventLoop] = None, **kwargs: object
) -> LoopStallWatchdog:
    """Start the process-wide watchdog (idempotent). Call from the loop thread."""
    global _watchdog
    if _watchdog is not None:
        return _watchdog
    target_loop = loop or asyncio.get_running_loop()
    _watchdog = LoopStallWatchdog(target_loop, **kwargs)  # type: ignore[arg-type]
    _watchdog.start()
    return _watchdog


def stop_loop_watchdog() -> None:
    global _watchdog
    if _watchdog is None:
        return
    _watchdog.stop()
    _watchdog = None


def get_loop_watchdog() -> Optional[LoopStallWatchdog]:
    return _watchdog
