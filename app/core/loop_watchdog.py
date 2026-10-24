"""Event-loop stall watchdog.

A daemon OS thread pings the asyncio loop once a second. If its callback is
unanswered for LOOP_STALL_THRESHOLD_S (default 10 s), it captures every
thread's stack, marking the event-loop thread. A continuing stall gets one
more dump at 45 s; recovery includes the duration and a bounded thread/task
census. Successful loop ticks are persisted every five seconds in the instance
log directory, exclusively from the watchdog thread. An unclean restart reports
the last tick, not a claimed stall duration (the process end time is unknown).

Unlike resource_monitor's coroutine-based lag metric, this captures the
blocking frame while the loop is stuck. The raw dump goes to file_logging's
durable freeze.log before the ERROR record goes through normal app logging,
so a loop holding a logging handler lock cannot prevent the durable dump.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_S = 10.0
DEFAULT_PING_INTERVAL_S = 1.0
DEFAULT_SECOND_THRESHOLD_S = 45.0
# GIL-independent freeze dump (#436): re-armed from the loop every
# FREEZE_DUMP_REARM_S, fires after FREEZE_DUMP_TIMEOUT_S without a re-arm.
FREEZE_DUMP_TIMEOUT_S = 30.0
FREEZE_DUMP_REARM_S = 5.0
HEARTBEAT_INTERVAL_S = 5.0
RECOVERY_THREAD_LIMIT = 5


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
        self._last_tick: Optional[float] = None
        self._recovery_task_count = 0
        self._heartbeat_path: Optional[Path] = None
        self._heartbeat_written_tick: Optional[float] = None
        self._heartbeat_last_write = float("-inf")
        self._heartbeat_failed = False
        self._pending_heartbeat_warning: Optional[functools.partial] = None
        # Observability for tests / the resource monitor.
        self.stall_count = 0
        self.dump_count = 0
        self.last_stall_seconds: Optional[float] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the watchdog thread. Must be called from the loop's thread."""
        if self._thread is not None and self._thread.is_alive():
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
        """Request shutdown; retain a timed-out thread so it can be joined again."""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            # Filesystem calls such as fsync cannot safely be cancelled in Python.
            # Do not lose ownership or let start() clear the stop event underneath
            # this thread. A later stop() can finish joining once I/O returns.
            frame = sys._current_frames().get(thread.ident)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "<exited>"
            self._log.warning(
                "Event-loop stall watchdog did not stop within %.1fs; thread retained\n%s",
                join_timeout, stack,
            )
            return
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
            self._last_tick = time.time()
            if self._stalled:
                # Inspect asyncio only on its owning loop; publish the count
                # with the pong so the watchdog cannot log an older census.
                self._recovery_task_count = len(asyncio.all_tasks(self._loop))
            warning = self._pending_heartbeat_warning
            self._pending_heartbeat_warning = None
        if warning is not None:
            warning()  # Logging happens only on the loop's existing ping callback.

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
        self._boot_heartbeat()
        try:
            while not self._stop.wait(self.ping_interval_s):
                now = time.monotonic()
                self._check(now)
                self._write_heartbeat(now)
                self._send_ping(now)
        finally:
            self._write_heartbeat(time.monotonic(), stopped_at=time.time())

    def _heartbeat_warning(self, message: str, *args: object, exc_info: bool = False) -> None:
        # No logging or extra wakeup here: call_soon_threadsafe itself may log
        # synchronously in asyncio debug mode if its wakeup socket is full.
        # Keep only the latest notice; the existing loop-side pong drains it.
        warning = functools.partial(
            self._log.warning, message, *args,
            exc_info=sys.exc_info() if exc_info else None,
        )
        with self._lock:
            self._pending_heartbeat_warning = warning

    def _boot_heartbeat(self) -> None:
        """All heartbeat filesystem work, including boot inspection, is off-loop."""
        from app.core.file_logging import get_log_dir

        try:
            self._heartbeat_path = get_log_dir() / "loop-heartbeat.json"
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                previous = json.loads(self._heartbeat_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            tick = float(previous["last_tick"])
            stopped_at = previous.get("stopped_at")
            # A clean shutdown gives an end time. SIGKILL cannot: age at boot
            # is only an upper bound, and can include time the app was off.
            end = float(stopped_at) if stopped_at is not None else time.time()
            if not math.isfinite(tick) or not math.isfinite(end):
                raise ValueError("non-finite heartbeat")
            if end - tick > self.threshold_s + HEARTBEAT_INTERVAL_S:
                self._heartbeat_warning(
                    "previous run stopped ticking at %s (%s)",
                    datetime.fromtimestamp(tick, UTC).isoformat(),
                    "before shutdown" if stopped_at is not None
                    else "unclean exit; process end time unknown, age at boot includes downtime",
                )
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self._heartbeat_warning("Could not read previous loop heartbeat", exc_info=True)

    def _write_heartbeat(self, now: float, *, stopped_at: Optional[float] = None) -> None:
        path = self._heartbeat_path
        with self._lock:
            tick = self._last_tick
        if path is None or tick is None:
            return
        if stopped_at is None and (
            now - self._heartbeat_last_write < HEARTBEAT_INTERVAL_S
            or tick == self._heartbeat_written_tick
        ):
            return
        self._heartbeat_last_write = now
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"last_tick": tick, "stopped_at": stopped_at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._heartbeat_written_tick = tick
        except OSError:
            if not self._heartbeat_failed:
                self._heartbeat_failed = True
                self._heartbeat_warning("Could not persist loop heartbeat", exc_info=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A failed cleanup must not stop stall detection.

    def _recovery_context(self) -> str:
        threads = threading.enumerate()
        frames = sys._current_frames()
        threads.sort(key=lambda thread: (thread.ident != self._loop_thread_id, thread.name))
        summaries = []
        for thread in threads[:RECOVERY_THREAD_LIMIT]:
            frame = frames.get(thread.ident)
            location = (
                f"{Path(frame.f_code.co_filename).name[:80]}:"
                f"{frame.f_lineno}:{frame.f_code.co_name[:80]}"
                if frame is not None else "no Python frame"
            )
            summaries.append(f"{thread.name[:60]}={location}")
        return (
            f"threads={len(threads)} thread_frames=[{'; '.join(summaries)}] "
            f"asyncio_tasks={self._recovery_task_count}"
        )

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
                    "Event loop stall ended after %.1fs (threshold %.1fs); %s",
                    duration, self.threshold_s, self._recovery_context(),
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


class FreezeDumpTimer:
    """Keep faulthandler's GIL-free C watchdog armed from the event loop.

    The stall watchdog above is a Python thread, so it cannot run while
    another thread holds the GIL in native code; in #436 the interpreter froze
    for two minutes and nothing was dumped. Every ``rearm_s`` the loop re-arms
    ``faulthandler.dump_traceback_later(timeout_s, repeat=True)``. When the
    loop or the whole interpreter stops for ``timeout_s``, faulthandler's own
    C thread writes every Python stack to freeze.log, then again every
    ``timeout_s`` until the loop re-arms it.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout_s: float = FREEZE_DUMP_TIMEOUT_S,
        rearm_s: float = FREEZE_DUMP_REARM_S,
    ) -> None:
        self._loop = loop
        self.timeout_s = timeout_s
        self.rearm_s = rearm_s
        self._handle: Optional[asyncio.TimerHandle] = None
        self.armed = False

    def start(self) -> None:
        if self._handle is None:
            self._tick()

    def _tick(self) -> None:
        from app.core.file_logging import arm_freeze_dump

        self.armed = arm_freeze_dump(self.timeout_s)
        if not self.armed:
            self._handle = None
            return  # file logging is off or freeze.log is full
        self._handle = self._loop.call_later(self.rearm_s, self._tick)

    def stop(self) -> None:
        from app.core.file_logging import cancel_freeze_dump

        handle, self._handle = self._handle, None
        if handle is not None:
            handle.cancel()
        if self.armed:
            cancel_freeze_dump()
        self.armed = False


# ----------------------------------------------------------------------
# process-wide singleton (started from app.main's lifespan)
# ----------------------------------------------------------------------

_watchdog: Optional[LoopStallWatchdog] = None
_freeze_timer: Optional[FreezeDumpTimer] = None


def start_loop_watchdog(
    loop: Optional[asyncio.AbstractEventLoop] = None, **kwargs: object
) -> LoopStallWatchdog:
    """Start the process-wide watchdog (idempotent). Call from the loop thread.

    Also arms the GIL-independent freeze dump (``FreezeDumpTimer``)."""
    global _watchdog, _freeze_timer
    if _watchdog is not None:
        return _watchdog
    target_loop = loop or asyncio.get_running_loop()
    _watchdog = LoopStallWatchdog(target_loop, **kwargs)  # type: ignore[arg-type]
    _watchdog.start()
    _freeze_timer = FreezeDumpTimer(target_loop)
    _freeze_timer.start()
    return _watchdog


def stop_loop_watchdog() -> None:
    global _watchdog, _freeze_timer
    if _freeze_timer is not None:
        _freeze_timer.stop()
        _freeze_timer = None
    if _watchdog is None:
        return
    _watchdog.stop()
    _watchdog = None
