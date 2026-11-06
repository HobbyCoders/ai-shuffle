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

    Share history indexing's and startup's schema advisory locks so other DDL
    cannot queue behind us and indirectly block writers. Autocommit is required
    for CONCURRENTLY. The pass budget is checked BETWEEN statements; a drop may
    take up to three minutes. The one-time tail index build has no statement
    timeout, so slow heap scans can finish. Do not cancel virtualxid waits:
    the concurrent DDL's ShareUpdateExclusive lock does not block ordinary DML.
    Failed concurrent DDL is repaired on a later pass. Never CASCADE.
    """
    deadline = time.monotonic() + seconds
    try:
        with history_indexing._online_cursor() as cur:
            if cur is None:
                return False
            cur.execute("SET lock_timeout = '0'")
            cur.execute("SET statement_timeout = '3min'")
            if not session_recall.ready(cur):
                return False
            # A native session-list prefilter must include rows promoted by old
            # builds whose text was truncated at INDEX_MAX_CHARS. Index the
            # existing cap-reaching part numbers: no shape/version change or
            # projection rebuild. Keep old GINs until this access path is valid.
            if not session_recall.native_tail_index_ready(cur):
                name = session_recall.NATIVE_TAIL_INDEX
                cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                # Restarting both heap scans every three minutes can starve a
                # slow worker forever. This one-time concurrent build does not
                # block DML; schema-lock coordination keeps other DDL off it.
                cur.execute("SET statement_timeout = '0'")
                try:
                    cur.execute(f"CREATE INDEX CONCURRENTLY {name} ON {session_recall.TABLE} "
                                f"(session_id, row_key) WHERE source = 'n' "
                                f"AND part >= {session_recall.NATIVE_CAPPED_PART}")
                finally:
                    cur.execute("SET statement_timeout = '3min'")
            for name in INDEXES:
                for suffix in ("", "_h0", "_h2"):
                    if time.monotonic() >= deadline:
                        return False
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}{suffix}")
            return True
    except psycopg2.Error:
        # Lock contention, cancellation, or missing DDL privileges: the next
        # background pass retries. The ready projection remains authoritative.
        logger.info("Transcript GIN retirement deferred; will retry", exc_info=True)
        return False
