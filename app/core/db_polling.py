"""Retry classification and bounded diagnostics for read-only DB polling.

These errors are safe to retry only at a polling read, never around a write or
an entire dispatch. Each wait owns its warning budget (no global session map).
"""

import logging
import time

from psycopg2 import InterfaceError, OperationalError
from psycopg2.pool import PoolError


DB_POLL_ERRORS = (PoolError, OperationalError, InterfaceError)


class DBPollWarnings:
    """Log the first failed probe, then at most once every 30 seconds per wait."""

    def __init__(self) -> None:
        self._next_warning = 0.0

    def warn(self, logger: logging.Logger, probe: str, session_id: str) -> None:
        now = time.monotonic()
        if now >= self._next_warning:
            self._next_warning = now + 30.0
            logger.warning(
                "%s DB probe unavailable for session %s; keeping worker busy and retrying",
                probe, session_id, exc_info=True,
            )
