"""Retire superseded transcript GINs online, only after projection backfill.

Keep generated recall_text columns: removing them needs ACCESS EXCLUSIVE and
saves no index write I/O. No source-table rewrite or startup DDL belongs here.
"""
from __future__ import annotations

import logging
import time

import psycopg2

from app.db import history_indexing, session_recall

logger = logging.getLogger(__name__)
INDEXES = (
    "idx_fluid_nodes_trgm", "idx_fluid_recall_text_trgm",
    "idx_sse_entry_trgm", "idx_sse_recall_text_trgm",
    "idx_session_events_recall_trgm", "idx_events_recall_text_trgm",
)


def run_pass(*, seconds: float = 2.0) -> bool:
    """Bounded, restart-safe maintenance; False means retry on a later pass.

    Share history indexing's advisory lock so it cannot split an index while
    we retire it. Autocommit is required for CONCURRENTLY. Both waits and each
    statement are bounded; a failed concurrent drop may leave an invalid index,
    which IF EXISTS will finish dropping on the next pass. Never CASCADE.
    """
    deadline = time.monotonic() + seconds
    try:
        with history_indexing._online_cursor() as cur:
            if cur is None:
                return False
            cur.execute("SET lock_timeout = '250ms'")
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (f"{max(1, int(seconds * 1000))}ms",))
            if not session_recall.ready(cur):
                return False
            for name in INDEXES:
                for suffix in ("", "_h0", "_h2"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    cur.execute("SELECT set_config('statement_timeout', %s, false)",
                                (f"{max(1, int(remaining * 1000))}ms",))
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}{suffix}")
            return True
    except psycopg2.Error:
        # Lock contention, cancellation, or missing DDL privileges: the next
        # background pass retries. The ready projection remains authoritative.
        logger.info("Transcript GIN retirement deferred; will retry", exc_info=True)
        return False
