"""Event-loop stall watchdog.

A daemon OS thread pings the asyncio loop once a second. If its callback is
unanswered for LOOP_STALL_THRESHOLD_S (default 10 s), it captures every
thread's stack, marking the event-loop thread. A continuing stall gets one
more dump at 45 s; recovery is logged with the stall duration.

Unlike resource_monitor's coroutine-based lag metric, this captures the
blocking frame while the loop is stuck. The raw dump goes to file_logging's
durable fatal.log before the ERROR record goes through normal app logging,
so a loop holding a logging handler lock cannot prevent the durable dump.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_S = 10.0
DEFAULT_PING_INTERVAL_S = 1.0
DEFAULT_SECOND_THRESHOLD_S = 45.0


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
    return value if math.isfinite(value) and value > 0 else DEFAULT_THRESHOLD_S


class LoopStallWatchdog:
    """Ping the loop from a daemon thread; dump all thread stacks on a stall."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold_s: Optional[float] = None,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        second_threshold_s: float = DEFAULT_SECOND_THRESHOLD_S,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._loop = loop
        self.threshold_s = threshold_s if threshold_s is not None else _threshold_from_env()
        self.ping_interval_s = ping_interval_s
        self.second_threshold_s = second_threshold_s
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
        self._second_dumped = False
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
            second_dumped = self._second_dumped

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
                self._second_dumped = False
                self.stall_count += 1
            self._dump(age, first=True)
            return

        if not second_dumped and age >= self.second_threshold_s:
            with self._lock:
                self._second_dumped = True
            self._dump(age, first=False)

    # ------------------------------------------------------------------
    # stack dump
    # ------------------------------------------------------------------

    def format_thread_stacks(self) -> str:
        """All current Python stacks, with the event-loop thread identified."""
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        stacks = []
        for ident, frame in sys._current_frames().items():
            label = " (event-loop thread)" if ident == self._loop_thread_id else ""
            stacks.append(f"Thread {names.get(ident, 'unknown')} ({ident}){label}:\n")
            stacks.extend(traceback.format_stack(frame))
        return "".join(stacks)

    def _dump(self, age: float, *, first: bool) -> None:
        with self._lock:
            self.dump_count += 1
        message = (
            f"Event loop {'stalled' if first else 'still stalled'} for {age:.1f}s "
            f"(threshold {self.threshold_s:.1f}s)"
        )
        # Write before taking any logging handler locks: the loop may hold one.
        from app.core.file_logging import dump_diagnostic_traceback

        dump_diagnostic_traceback(message)
        self._log.error(
            "%s — all thread stacks, including loop thread stack "
            "(innermost last):\n%s", message, self.format_thread_stacks(),
        )


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
