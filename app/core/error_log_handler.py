"""
Root-logger handler that funnels backend ERROR+ logs into the durable
``error_log`` table (see ``app/db/error_logs.py``).

Attached once at startup (``app/main.py`` lifespan, after the DB pool is up) via
``install_error_log_handler``. Every ``logger.error(...)`` /
``logger.exception(...)`` — including the global unhandled-exception handler —
then lands in the same store the ``GetAppErrors`` MCP tool and the admin UI read
from, with no scattered explicit calls.

Guards (all essential):
- Only records at ERROR or above are persisted.
- Records from ``app.db*`` loggers are skipped — a DB error logging into the DB
  would risk a feedback loop.
- A thread-local re-entrancy flag stops anything logged *inside* record_error
  from recursing.
- ``emit()`` never raises; logging must never break the app.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_local = threading.local()
_formatter = logging.Formatter()

# Module-level singleton so install/uninstall are idempotent.
_handler: "ErrorLogDBHandler | None" = None

# Conservative marker for the SDK reader-task fatal we most need forensics on.
_FATAL_READER_MARKER = "Fatal error in message reader"


def _fatal_stderr_tail(session_id: str) -> "str | None":
    """Return the pooled subprocess's captured stderr tail for ``session_id``.

    Used to enrich a "Fatal error in message reader" error_log row with the
    real CLI output the SDK's ProcessError never carries. Best-effort and never
    raises: pool import is deferred (avoid an import cycle) and every lookup is
    guarded, so a missing session / empty ring simply yields None.
    """
    try:
        # Deferred import: session_pool imports the provider stack; importing it
        # at module load could form a cycle with the logging bootstrap.
        # Read the module-level singleton directly rather than get_session_pool()
        # so a logging emit never *creates* a pool as a side effect.
        from app.core import session_pool as _sp

        pool = _sp._session_pool
        if pool is None:
            return None
        state = pool.peek(session_id)  # non-mutating (no LRU promote)
        if state is None:
            return None
        lines = getattr(state, "cli_stderr_lines", None)
        if not lines:
            return None
        tail = [ln.strip() for ln in list(lines) if ln and ln.strip()]
        if not tail:
            return None
        return "\n".join(tail)
    except Exception:  # noqa: BLE001 — enrichment must never break emit
        return None


class ErrorLogDBHandler(logging.Handler):
    """Persist ERROR+ log records to the ``error_log`` table."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        # Never capture our own DB layer or this handler — avoids feedback loops.
        if record.name.startswith("app.db") or record.name == __name__:
            return
        # Re-entrancy guard: anything logged while we persist must not recurse.
        if getattr(_local, "in_emit", False):
            return
        _local.in_emit = True
        try:
            from app.db import error_logs

            detail = None
            if record.exc_info:
                detail = _formatter.formatException(record.exc_info)
            elif record.stack_info:
                detail = _formatter.formatStack(record.stack_info)

            message = record.getMessage()

            # Session attribution: the SDK's detached message-reader task (which
            # logs the fatal below) inherits a ContextVar set at the spawn/query
            # site. Only fill it when the record doesn't already carry a session
            # id, and never let the read raise out of this logging path.
            session_id = getattr(record, "session_id", None)
            if session_id is None:
                try:
                    from app.core.session_context import get_current_session_id
                    session_id = get_current_session_id()
                except Exception:
                    session_id = None

            # Expected-shutdown demotion: when the fatal-reader record belongs to
            # a session the adapter just flagged for intentional teardown (options
            # reconnect, rewind, idle/LRU or interrupt eviction), the non-zero
            # subprocess exit is the force-terminate we asked for — not a live
            # crash. Log it at WARNING for visibility but do NOT persist a durable
            # ERROR row. One-shot + time-bounded (see expected_shutdown.py), so a
            # genuine reader crash on a later turn of the same session still lands
            # as an ERROR. Records without a flag keep the current behavior.
            if session_id and _FATAL_READER_MARKER in message:
                try:
                    from app.core.expected_shutdown import consume_expected_shutdown

                    if consume_expected_shutdown(session_id):
                        logger.warning(
                            "Suppressed expected-shutdown fatal-reader error for "
                            "session %s (adapter-initiated teardown): %s",
                            session_id,
                            message,
                        )
                        return
                except Exception:
                    # Never let the demotion check break the logging path — fall
                    # through and persist the record as before.
                    pass

            # Enrich the fatal-reader record with the real CLI stderr tail the
            # SDK's ProcessError never carries. Conservative substring match;
            # fully guarded so a missing state / empty ring is a no-op.
            if session_id and _FATAL_READER_MARKER in message:
                try:
                    tail = _fatal_stderr_tail(session_id)
                except Exception:
                    tail = None
                if tail:
                    prefix = f"{detail}\n\n" if detail else ""
                    detail = f"{prefix}[Captured CLI stderr]\n{tail}"

            error_logs.record_error(
                source="backend",
                message=message,
                level=record.levelname,
                detail=detail,
                logger_name=record.name,
                session_id=session_id,
                context={
                    "module": record.module,
                    "func": record.funcName,
                    "line": record.lineno,
                },
            )
        except Exception:
            pass
        finally:
            _local.in_emit = False


def install_error_log_handler() -> None:
    """Attach the DB error handler to the root logger (idempotent).

    Call after the DB pool is initialized. If the pool isn't ready when an error
    fires, ``record_error`` simply no-ops, so this is safe either way.
    """
    global _handler
    if _handler is not None:
        return
    _handler = ErrorLogDBHandler()
    logging.getLogger().addHandler(_handler)
    logger.info("ErrorLogDBHandler attached — backend errors persist to error_log")


def uninstall_error_log_handler() -> None:
    """Detach the DB error handler from the root logger (idempotent)."""
    global _handler
    if _handler is None:
        return
    logging.getLogger().removeHandler(_handler)
    _handler = None
