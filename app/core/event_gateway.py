"""LISTEN/NOTIFY → asyncio fan-out gateway (v2 core).

Bridges a dedicated PostgreSQL ``LISTEN session_events`` connection to
per-session asyncio subscriber queues. The SSE route (``app/api/events.py``)
subscribes ONE queue per session — a shared reader that turns each ``max_seq``
tick into a single DB read plus a single serialization pass, then fans the
resulting frames out to every client on that session.

Delivery must never depend on this gateway: the SSE route also polls
``get_max_seq`` on a keepalive timer, so if PostgreSQL is unreachable the
gateway degrades to "no notifies, warnings logged" and the app keeps working.
The listener runs on a single dedicated (non-pool) connection in a daemon
thread, reconnecting with capped exponential backoff on any error.
"""

from __future__ import annotations

import json
import logging
import select
import threading
from typing import Callable, Dict, List, Set, Tuple

import psycopg2

from app.core.config import settings

logger = logging.getLogger(__name__)

LISTEN_CHANNEL = "session_events"


class EventGateway:
    """Fan-out from a dedicated PG LISTEN connection to asyncio queues."""

    def __init__(self) -> None:
        # session_id -> set of (loop, queue) pairs.
        self._subscribers: Dict[str, Set[Tuple[object, object]]] = {}
        # All-sessions listeners: fn(session_id) invoked on every notify tick.
        self._global_listeners: List[Callable[[str], None]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._stop = False
        # Set by ``stop()`` to interrupt the reconnect backoff sleep at once, so
        # shutdown/teardown never has to wait out a multi-second backoff hop.
        self._stop_event = threading.Event()
        # True only while a LISTEN connection is established and being polled.
        # Consumers use this to decide whether NOTIFY delivery can be trusted as
        # the *primary* wake path (SSE readers then drop their per-tick poll to a
        # low-frequency correctness re-sync); False means "degraded — poll".
        self._connected = False

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def subscribe(self, session_id: str):
        """Register and return a fresh asyncio.Queue for ``session_id``.

        Captures the currently-running loop alongside the queue so the
        listener thread can hand values back thread-safely. Lazily starts the
        listener thread on first use.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(session_id, set()).add((loop, queue))
        self._ensure_listener()
        return queue

    def add_global_listener(self, fn: Callable[[str], None]) -> None:
        """Register ``fn(session_id)`` invoked on EVERY notify tick.

        Called from the listener thread, so ``fn`` must be cheap and
        non-blocking (hand real work back to a loop). Idempotent per fn.
        Lazily starts the listener thread (same as subscribe) so global
        listeners tick even before the first SSE subscriber arrives.
        """
        with self._lock:
            if fn not in self._global_listeners:
                self._global_listeners.append(fn)
        self._ensure_listener()

    def is_connected(self) -> bool:
        """True while the LISTEN connection is established and being polled.

        Notify delivery is only trustworthy while this holds. Readers that would
        otherwise poll the database on every keepalive tick (``app.api.events``)
        use it to tell "quiet session, notifies healthy" (skip the read) apart
        from "gateway degraded" (keep polling — the documented fallback).
        """
        return self._connected

    def unsubscribe(self, session_id: str, queue) -> None:
        """Remove a subscriber; drop the session key once empty."""
        with self._lock:
            pairs = self._subscribers.get(session_id)
            if not pairs:
                return
            for pair in list(pairs):
                if pair[1] is queue:
                    pairs.discard(pair)
            if not pairs:
                self._subscribers.pop(session_id, None)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, session_id: str, max_seq: int) -> None:
        """Push ``max_seq`` to every queue registered for ``session_id``.

        Tolerates a closed loop (``RuntimeError``) by dropping that subscriber.
        """
        with self._lock:
            pairs = list(self._subscribers.get(session_id, ()))
        for loop, queue in pairs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, max_seq)
            except RuntimeError:
                # Loop is closed — drop this subscriber.
                self.unsubscribe(session_id, queue)

    # ------------------------------------------------------------------
    # Listener thread
    # ------------------------------------------------------------------
    def _ensure_listener(self) -> None:
        """Start the listener thread exactly once (thread-safe)."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._listen_loop,
                name="event-gateway-listener",
                daemon=True,
            )
            self._thread.start()

    def _listen_loop(self) -> None:
        from app.db.base import parse_database_url

        backoff = 1.0
        consecutive_failures = 0
        while not self._stop:
            conn = None
            try:
                # Decompose the DSN like the pool does — a raw URL with a
                # literal '@' in the password makes libpq mis-parse the host.
                conn = psycopg2.connect(**parse_database_url(settings.get_database_url()))
                # LISTEN must run outside a transaction.
                conn.autocommit = True
                cursor = conn.cursor()
                cursor.execute(f"LISTEN {LISTEN_CHANNEL}")
                backoff = 1.0  # Reset on a successful connect.
                consecutive_failures = 0
                self._connected = True
                while not self._stop:
                    if select.select([conn], [], [], 5.0) == ([], [], []):
                        continue
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        self._handle_payload(notify.payload)
            except Exception as e:  # noqa: BLE001 — must never crash the app.
                self._connected = False
                logger.warning(
                    "event gateway listener error (will reconnect): %s", e
                )
                consecutive_failures += 1
                if consecutive_failures == 3:
                    # Without this connection every SSE reader silently
                    # degrades to the keepalive poll, so surface the streak
                    # durably (once — record_error dedups by fingerprint and
                    # never raises; it uses the pool, not this connection).
                    try:
                        from app.db.error_logs import record_error

                        record_error(
                            "backend",
                            "Event gateway LISTEN connection failing "
                            "repeatedly; live SSE delivery degraded to "
                            f"keepalive polling: {e}",
                            logger_name=__name__,
                            context={"consecutive_failures": consecutive_failures},
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("failed to record gateway error")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                if self._stop:
                    break
                self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _handle_payload(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            session_id = data["session_id"]
            max_seq = int(data["max_seq"])
        except (ValueError, TypeError, KeyError):
            logger.warning("event gateway: malformed notify payload: %r", payload)
            return
        with self._lock:
            listeners = list(self._global_listeners)
        for fn in listeners:
            try:
                fn(session_id)
            except Exception:  # noqa: BLE001 — a listener bug must not kill the loop
                logger.warning(
                    "event gateway: global listener failed for %s",
                    session_id, exc_info=True,
                )
        self._dispatch(session_id, max_seq)

    def _sleep(self, seconds: float) -> None:
        # Interruptible backoff sleep: ``stop()`` sets ``_stop_event`` to wake
        # this immediately, so shutdown never waits out a full backoff hop.
        # Returns early (does not raise) when stopped.
        self._stop_event.wait(seconds)

    def stop(self, join_timeout: float = 5.0) -> None:
        """Stop the listener thread and join it (tests/shutdown only).

        Idempotent and safe to call when the thread never started. Wakes the
        reconnect-backoff sleep immediately via ``_stop_event`` so the join
        returns promptly. A fresh ``subscribe``/``add_global_listener`` after a
        stop lazily restarts the listener (``_ensure_listener`` clears the flag
        and event)."""
        self._stop = True
        self._stop_event.set()
        self._connected = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)


_gateway: EventGateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> EventGateway:
    """Return the process-wide :class:`EventGateway` singleton."""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = EventGateway()
    return _gateway
