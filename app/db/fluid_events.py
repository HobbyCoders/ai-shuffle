"""Retention for the fluid event log (``fluid_session_events``).

The log is append-only and the engine persists every bus event to it
unfiltered, so streaming deltas dominate it by volume. Left alone the table becomes the
largest thing in the database. It has no timestamp column; maintenance dates
sessions first, then walks bounded ranges of the existing (session_id, seq)
primary key, including nonmatching rows so sparse deltas cannot cause a full scan.

Only PURE STREAMING DELTAS are prunable, plus ``ProviderAlive``, a content-free
signal that only matters while its model call is running. A delta
carries an incremental fragment of something the record already holds in
completed form:

* ``ToolArgsDelta`` fragments are superseded by the ``ToolUse`` row carrying the
  finished arguments — fluid's own docstring says consumers "never depend on
  them for the call's actual arguments";
* ``TextDelta`` and ``ThinkingDelta`` fragments are superseded by the assistant
  message stored in ``fluid_session_nodes``.

Everything else in the log is evidence and is retained:

* ``Usage`` backs the accounting backfill (scripts/backfill_usage_from_events.py),
  which has no other source once a session's rows are gone;
* ``ToolUse`` and ``ToolResult`` are the ONLY record of a code-mode session's
  in-kernel tool calls — those never reach the node tree at all;
* ``ContextCompaction`` is what lets a restart replay a byte-identical view;
* ``TurnComplete`` is the turn count the backfill reads.

``BlockStart`` / ``BlockStop`` are deliberately kept too. They are the framing
around the deltas rather than deltas themselves, and dropping structure to save
rows that are already gone buys nothing.

Pruning is bounded to sessions idle longer than the retention window, so a live
or recently active session's replay is never touched.
"""

import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from app.db import retention
from app.db.retention import BATCH_SIZE, set_timeout

logger = logging.getLogger(__name__)

# Event classes safe to delete: each is a pure incremental fragment of
# something the record keeps in completed form. See the module docstring for
# why nothing else qualifies. Matched against the ``_cls`` discriminator that
# fluid.events.model.event_to_dict writes into every persisted event.
PRUNABLE_EVENT_CLASSES: Tuple[str, ...] = (
    "ToolArgsDelta",
    "TextDelta",
    "ThinkingDelta",
    # Not a fragment of anything: a content-free "the provider accepted the
    # call and is streaming" signal, which is evidence of nothing once the
    # call's Usage row exists.
    "ProviderAlive",
)


def prune_fluid_delta_events(
    older_than_days: int = 7,
    *,
    cursor: Optional[Any] = None,
    stop=None,
) -> int:
    """Delete reconstructible streaming deltas for long-idle sessions.

    ``older_than_days`` is the idle window; 0 or negative disables the pass and
    returns 0 without touching the database. A session's age comes from
    ``sessions.updated_at``, which a streaming session bumps constantly — so an
    active session can never fall inside the cutoff. Fluid sessions with no
    ``sessions`` row are left alone entirely: there is no signal to age them by,
    and pruning something we cannot date is not worth the rows.

    Pass ``cursor`` to retain a caller's transaction; otherwise each bounded
    batch commits and releases its connection (including the maintenance job).

    Returns the number of rows deleted.
    """
    if older_than_days <= 0:
        logger.debug(
            "fluid_events: retention disabled (older_than_days=%s)", older_than_days
        )
        return 0

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).isoformat()

    placeholders = ", ".join(["%s"] * len(PRUNABLE_EVENT_CLASSES))
    total = 0
    after_session = None
    failures = 0
    while stop is None or not stop.is_set():
        with nullcontext(None) if cursor is not None else retention.get_db() as conn:
            cur = cursor if cursor is not None else conn.cursor()
            set_timeout(cur)
            session_boundary = " AND sdk_session_id > %s" if after_session is not None else ""
            session_params = (cutoff, after_session) if after_session is not None else (cutoff,)
            cur.execute(
                "SELECT DISTINCT sdk_session_id FROM sessions "
                "WHERE sdk_session_id IS NOT NULL AND updated_at < %s"
                + session_boundary + " ORDER BY sdk_session_id LIMIT %s",
                (*session_params, BATCH_SIZE),
            )
            sessions = cur.fetchall()
        if not sessions:
            break
        for session in sessions:
            after_session = session["sdk_session_id"]
            after_seq = None
            while stop is None or not stop.is_set():
                try:
                    with nullcontext(None) if cursor is not None else retention.get_db() as conn:
                        cur = cursor if cursor is not None else conn.cursor()
                        set_timeout(cur)
                        # Bound EXAMINED rows, not just matching rows. The old
                        # global class/idle filter could scan millions of retained
                        # events to find the next 5,000 deltas (or none at all).
                        boundary = " AND seq > %s" if after_seq is not None else ""
                        params = (after_session, after_seq) if after_seq is not None else (after_session,)
                        cur.execute(
                            "SELECT seq FROM fluid_session_events WHERE session_id = %s"
                            + boundary + " ORDER BY seq LIMIT %s",
                            (*params, BATCH_SIZE),
                        )
                        keys = cur.fetchall()
                        if not keys:
                            break
                        high = keys[-1]["seq"]
                        cur.execute(
                            "DELETE FROM fluid_session_events WHERE session_id = %s "
                            "AND seq >= %s AND seq <= %s "
                            f"AND event ->> '_cls' IN ({placeholders}) "
                            # Recheck idleness in the mutation, not just selection.
                            "AND EXISTS (SELECT 1 FROM sessions WHERE sdk_session_id = %s "
                            "AND updated_at < %s)",
                            (after_session, keys[0]["seq"], high, *PRUNABLE_EVENT_CLASSES,
                             after_session, cutoff),
                        )
                        changed = cur.rowcount or 0
                    total += changed
                    after_seq = high
                    if len(keys) < BATCH_SIZE:
                        break
                except Exception:
                    if cursor is not None:
                        raise  # The caller owns rollback of its transaction.
                    failures += 1
                    logger.exception("fluid_events: delta pruning failed for one session; "
                                     "committed batches survive and other sessions will continue")
                    break
        if len(sessions) < BATCH_SIZE:
            break
    if failures:
        raise RuntimeError(f"fluid delta pruning failed for {failures} session(s); "
                           f"pruned {total} rows in committed batches; will retry next pass")

    if total:
        logger.info(
            "fluid_events: pruned %s streaming-delta rows from sessions idle "
            "more than %s days",
            total,
            older_than_days,
        )
    return total
