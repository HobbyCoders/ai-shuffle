"""Retention for the fluid event log (``fluid_session_events``).

The log is append-only and the engine persists every bus event to it
unfiltered, so streaming deltas dominate it by volume — one audited session
held 27,510 ``ToolArgsDelta`` rows by itself. Left alone the table becomes the
largest thing in the database, and it has no timestamp column or secondary
index to make that cheap to live with.

Only PURE STREAMING DELTAS are prunable, plus ``ProviderAlive``, a content-free
liveness signal that only matters while its model call is running. A delta
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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from app.db.base import get_db

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
) -> int:
    """Delete reconstructible streaming deltas for long-idle sessions.

    ``older_than_days`` is the idle window; 0 or negative disables the pass and
    returns 0 without touching the database. A session's age comes from
    ``sessions.updated_at``, which a streaming session bumps constantly — so an
    active session can never fall inside the cutoff. Fluid sessions with no
    ``sessions`` row are left alone entirely: there is no signal to age them by,
    and pruning something we cannot date is not worth the rows.

    Pass ``cursor`` to run inside a caller's transaction (the maintenance job
    does); otherwise this opens its own.

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
    sql = (
        "DELETE FROM fluid_session_events "
        f"WHERE event ->> '_cls' IN ({placeholders}) "
        "AND session_id IN ("
        "    SELECT sdk_session_id FROM sessions "
        "    WHERE sdk_session_id IS NOT NULL AND updated_at < %s"
        ")"
    )
    params = (*PRUNABLE_EVENT_CLASSES, cutoff)

    if cursor is not None:
        cursor.execute(sql, params)
        deleted = cursor.rowcount or 0
    else:
        with get_db() as conn:
            own = conn.cursor()
            own.execute(sql, params)
            deleted = own.rowcount or 0

    if deleted:
        logger.info(
            "fluid_events: pruned %s streaming-delta rows from sessions idle "
            "more than %s days",
            deleted,
            older_than_days,
        )
    return deleted
