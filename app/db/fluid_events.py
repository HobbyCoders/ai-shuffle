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
  which has no other source once a session's rows are gone. Its accounting
  fields (token counters, ``raw``, ``node_id``, ``turn_id``, ``ts``, ``seq``)
  are kept byte-for-byte. Only its ``context_usage`` snapshot, the per-call
  context breakdown that is most of the row, is compacted for idle sessions
  to the usage-bar form (``compact_context_usage``, the same form the app log's
  ``turn.usage`` already stores). Nothing reads the dropped breakdown detail
  from an idle session's old rows: a resumed engine reads ``model`` from the
  snapshot, and the context panel builds its breakdown live;
* ``ToolUse`` and ``ToolResult`` are the ONLY record of a code-mode session's
  in-kernel tool calls — those never reach the node tree at all;
* ``ContextCompaction`` is what lets a restart replay a byte-identical view;
* ``TurnComplete`` is the turn count the backfill reads.

``BlockStart`` / ``BlockStop`` are deliberately kept too. They are the framing
around the deltas rather than deltas themselves, and dropping structure to save
rows that are already gone buys nothing.

Pruning and compaction are bounded to sessions idle longer than the retention
window, so a live or recently active session's replay is never touched. Both
run in the same walk: each bounded key range is pruned and compacted in one
short transaction (``maintain_fluid_event_log``).
"""

import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import psycopg2.extensions

from app.core.transcript_replay import _COMPACT_CATEGORY_KEYS, _COMPACT_CONTEXT_KEYS
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

# A session whose range DELETE fails skips that range and continues; this many
# failures in a row stop it for the pass, so a session where every statement
# times out costs a few statement timeouts, not one per range.
MAX_CONSECUTIVE_FAILED_RANGES = 3

# Rewrites one range's idle-session Usage snapshots to exactly
# ``compact_context_usage(snapshot)`` (app/core/transcript_replay.py), keyed by
# its own key tuples so there is one definition of the compact form. The WHERE
# matches only snapshots that compaction would change (an object with a
# top-level key outside the compact keys, or a category object with a key
# outside the category keys), so compacted, null or missing snapshots are never
# rewritten again and a compacted session costs only the range read.
_SNAPSHOT = "event -> 'context_usage'"
_COMPACT_USAGE_SQL = (
    "UPDATE fluid_session_events SET event = jsonb_set(event, '{context_usage}', ("
    "SELECT COALESCE(jsonb_object_agg(k, CASE "
    "WHEN k = 'categories' AND jsonb_typeof(v) = 'array' THEN ("
    "SELECT COALESCE(jsonb_agg(CASE WHEN jsonb_typeof(c) = 'object' THEN ("
    "SELECT COALESCE(jsonb_object_agg(ck, cv), '{}'::jsonb) "
    "FROM jsonb_each(c) AS f(ck, cv) WHERE ck = ANY(%(category_keys)s::text[])) "
    "ELSE c END ORDER BY n), '[]'::jsonb) "
    "FROM jsonb_array_elements(v) WITH ORDINALITY AS a(c, n)) "
    "ELSE v END), '{}'::jsonb) "
    f"FROM jsonb_each({_SNAPSHOT}) AS e(k, v) WHERE k = ANY(%(context_keys)s::text[]))) "
    "WHERE session_id = %(session)s AND seq >= %(low)s AND seq <= %(high)s "
    "AND event ->> '_cls' = 'Usage' "
    f"AND jsonb_typeof({_SNAPSHOT}) = 'object' "
    f"AND (({_SNAPSHOT}) - %(context_keys)s::text[] <> '{{}}'::jsonb "
    f"OR (jsonb_typeof({_SNAPSHOT} -> 'categories') = 'array' AND EXISTS ("
    f"SELECT 1 FROM jsonb_array_elements({_SNAPSHOT} -> 'categories') AS g(item) "
    "WHERE jsonb_typeof(item) = 'object' "
    "AND item - %(category_keys)s::text[] <> '{}'::jsonb))) "
    # Recheck idleness in the mutation, like the delta DELETE.
    "AND EXISTS (SELECT 1 FROM sessions WHERE sdk_session_id = %(session)s "
    "AND updated_at < %(cutoff)s)"
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
    Without a cursor, a range whose DELETE fails after its SELECT is skipped for
    this pass; any failure still ends in a RuntimeError with the committed count.

    Returns the number of rows deleted.
    """
    return _walk_idle_sessions(older_than_days, cursor=cursor, stop=stop,
                               compact_usage=False)["pruned"]


def maintain_fluid_event_log(
    older_than_days: int = 7,
    *,
    cursor: Optional[Any] = None,
    stop=None,
) -> dict[str, int]:
    """Prune deltas and compact Usage snapshots of long-idle sessions in one walk.

    The same idle rule, window, ranges, timeouts, stop handling and failure
    recovery as :func:`prune_fluid_delta_events`; each range's DELETE and
    Usage-snapshot UPDATE share one short transaction, so a failed range is
    rolled back whole (its rows stay fully old) and retried next pass. The
    UPDATE runs only on PostgreSQL cursors: the test shim cannot evaluate it.

    Returns ``{"pruned": deleted rows, "compacted": rewritten Usage rows}``.
    """
    return _walk_idle_sessions(older_than_days, cursor=cursor, stop=stop,
                               compact_usage=True)


def _walk_idle_sessions(older_than_days, *, cursor, stop, compact_usage) -> dict[str, int]:
    counts = {"pruned": 0, "compacted": 0}
    if older_than_days <= 0:
        logger.debug(
            "fluid_events: retention disabled (older_than_days=%s)", older_than_days
        )
        return counts

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).isoformat()

    placeholders = ", ".join(["%s"] * len(PRUNABLE_EVENT_CLASSES))
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
            session_failed = False
            consecutive_failed = 0
            while stop is None or not stop.is_set():
                # Set only once this attempt's SELECT completes; never carried over.
                high = None
                full = False
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
                        full = len(keys) >= BATCH_SIZE
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
                        pruned = cur.rowcount or 0
                        compacted = 0
                        if compact_usage and isinstance(cur, psycopg2.extensions.cursor):
                            cur.execute(_COMPACT_USAGE_SQL, {
                                "session": after_session, "low": keys[0]["seq"], "high": high,
                                "cutoff": cutoff, "context_keys": list(_COMPACT_CONTEXT_KEYS),
                                "category_keys": list(_COMPACT_CATEGORY_KEYS),
                            })
                            compacted = cur.rowcount or 0
                    counts["pruned"] += pruned
                    counts["compacted"] += compacted
                    consecutive_failed = 0
                    after_seq = high
                    if not full:
                        break
                except Exception:
                    if cursor is not None:
                        raise  # The caller owns rollback of its transaction.
                    # get_db has rolled this range back and released its connection.
                    session_failed = True
                    consecutive_failed += 1
                    if high is not None and full and consecutive_failed < MAX_CONSECUTIVE_FAILED_RANGES:
                        # The range is known: leave it for the next pass (which
                        # restarts the session) and try the rest of this session now.
                        logger.exception("fluid_events: retention failed for one range; "
                                         "skipping to the next range, the next pass retries it")
                        after_seq = high
                        continue
                    # No completed SELECT (no safe high key), the last range, or
                    # too many failures in a row: stop this session for this pass.
                    logger.exception("fluid_events: retention failed for one session; "
                                     "committed batches survive and other sessions will continue")
                    break
            if session_failed:
                failures += 1
        if len(sessions) < BATCH_SIZE:
            break
    total, compacted = counts["pruned"], counts["compacted"]
    if failures:
        if compact_usage:
            raise RuntimeError(f"fluid event retention failed for {failures} session(s); "
                               f"pruned {total} rows and compacted {compacted} Usage snapshots "
                               "in committed batches; will retry next pass")
        raise RuntimeError(f"fluid delta pruning failed for {failures} session(s); "
                           f"pruned {total} rows in committed batches; will retry next pass")

    if total:
        logger.info(
            "fluid_events: pruned %s streaming-delta rows from sessions idle "
            "more than %s days",
            total,
            older_than_days,
        )
    if compacted:
        logger.info(
            "fluid_events: compacted %s Usage context snapshots from sessions idle "
            "more than %s days",
            compacted,
            older_than_days,
        )
    return counts
