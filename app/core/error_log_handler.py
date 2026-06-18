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

            error_logs.record_error(
                source="backend",
                message=record.getMessage(),
                level=record.levelname,
                detail=detail,
                logger_name=record.name,
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
