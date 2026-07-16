"""Expected-shutdown registry for adapter-initiated subprocess teardown.

When :meth:`ClaudeAgentAdapter.disconnect` tears down the CLI subprocess on an
*intentional* path (options-changed reconnect, rewind, idle/LRU eviction,
interrupt-escalation eviction), the exit-grace can expire and the subprocess is
force-terminated. The SDK's still-live message-reader task then sees a non-zero
exit with no error-result and logs ``Fatal error in message reader`` at ERROR,
which :mod:`app.core.error_log_handler` persists to the durable ``error_log``
table. That row is expected shutdown noise, not a live failure — the teardown
was deliberate.

``disconnect`` marks the session here *before* terminating; ``error_log_handler``
consults the registry when it sees the fatal-reader marker and demotes the
record to WARNING instead of writing an ERROR row. Genuine mid-turn reader
crashes (no mark) keep their ERROR rows.

Why a plain dict + threading.Lock (not a ContextVar): the SDK reader task logs
from a *different* asyncio task than the one running ``disconnect``, and the
logging handler's ``emit`` runs on the synchronous logging path (any thread).
A module-level dict guarded by a lock is the simplest mechanism both the async
teardown side and the sync logging side can share safely.

Marks are one-shot (popped on the first matching read) *and* time-bounded: a
disconnect whose subprocess exits cleanly never provokes the fatal, so its mark
would otherwise linger and could suppress a genuine crash on a later reconnect
of the same session id. The TTL guards against that.
"""

from __future__ import annotations

import threading
import time

# session_id -> monotonic timestamp the mark was set.
_expected: "dict[str, float]" = {}
_lock = threading.Lock()

# A mark is only honored within this window. Long enough to cover the
# terminate -> fatal-reader-log race (observed ~31ms, but allow generous slack
# for the SDK's own close escalation), short enough that an unmatched mark
# cannot mask a real crash on a subsequent reconnect of the same session.
_EXPECTED_SHUTDOWN_TTL_SECONDS = 30.0


def mark_expected_shutdown(session_id: "str | None") -> None:
    """Flag ``session_id`` as undergoing an intentional subprocess teardown."""
    if not session_id:
        return
    now = time.monotonic()
    with _lock:
        _expected[session_id] = now
        # Opportunistically prune stale marks (marks whose fatal never fired
        # because the subprocess exited cleanly) so the dict cannot grow
        # unbounded across a long-lived process.
        if len(_expected) > 1:
            cutoff = now - _EXPECTED_SHUTDOWN_TTL_SECONDS
            stale = [sid for sid, ts in _expected.items() if ts < cutoff]
            for sid in stale:
                _expected.pop(sid, None)


def consume_expected_shutdown(session_id: "str | None") -> bool:
    """Return True if ``session_id`` carries a live expected-shutdown mark.

    One-shot: the mark is removed on the first matching read. Only honored
    within :data:`_EXPECTED_SHUTDOWN_TTL_SECONDS`; an expired mark is discarded
    and returns False. Never raises — safe to call from the sync logging path.
    """
    if not session_id:
        return False
    try:
        with _lock:
            ts = _expected.pop(session_id, None)
    except Exception:  # noqa: BLE001 — a check on the logging path must not raise
        return False
    if ts is None:
        return False
    return (time.monotonic() - ts) <= _EXPECTED_SHUTDOWN_TTL_SECONDS


def clear_expected_shutdown(session_id: "str | None") -> None:
    """Drop any mark for ``session_id`` (test/utility helper). Never raises."""
    if not session_id:
        return
    try:
        with _lock:
            _expected.pop(session_id, None)
    except Exception:  # noqa: BLE001
        pass
