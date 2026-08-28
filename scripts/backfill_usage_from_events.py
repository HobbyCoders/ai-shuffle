"""Recompute per-session token/turn accounting from the fluid event log.

Why this exists
---------------
Until the accounting fix, ``sessions`` recorded a distorted picture of what a
session cost:

* ``total_tokens_in`` was fed ``metadata["tokens_in"]``, which is FRESH input
  only — usage is normalized Anthropic-style, so cache reads are excluded. The
  per-turn cache numbers went exclusively to the OVERWRITE snapshot columns
  (``cache_read_tokens`` / ``cache_creation_tokens``), which hold the LAST call's
  context size and accumulate nothing. On a cache-heavy session that hid roughly
  90% of the prompt tokens ever spent — the audit found a 253k-token research
  child booked as 15 tokens.
* ``turn_count`` was fed ``metadata["num_turns"]``, which on the fluid path is
  the MODEL-CALL count (``harness_adapter`` sets ``num_turns =
  event.num_model_calls``). A one-turn child reported 16 turns.

New sessions are written correctly. This script repairs the ones already on
disk, and it can only do that where the evidence survives: ``fluid_session_events``
keeps every ``Usage`` event (one per model call, carrying ``input_tokens`` /
``output_tokens`` / ``cache_read_tokens`` / ``cache_creation_tokens``) and every
``TurnComplete`` (one per turn). Sessions with no fluid event log — delegated-leg
or pre-fluid rows — are left exactly as they are. So are sessions whose log is
only a FRAGMENT of their history (``sdk_session_id`` names the latest fluid log;
a re-keyed or mixed-leg session has booked usage the log never saw): when the
recomputed fresh in/out sums come in below what is already booked, the row is
reported as "partial event log" and skipped.

What it writes
--------------
For each session whose ``sdk_session_id`` names a fluid event log:

    total_tokens_in              = SUM(Usage.input_tokens)
    total_tokens_out             = SUM(Usage.output_tokens)
    total_cache_read_tokens      = SUM(Usage.cache_read_tokens)
    total_cache_creation_tokens  = SUM(Usage.cache_creation_tokens)
    total_model_calls            = COUNT(Usage events)
    turn_count                   = COUNT(TurnComplete events)

Absolute assignments, never increments, so the script is idempotent: run it
twice and the second run reports nothing to do. Cost is NOT touched — cost was
always accumulated correctly.

Nothing runs this automatically. It is an operator tool, invoked by hand.

Usage
-----
From the repo root, with POSTGRES_* pointing at the target database
(see .env.example)::

    # Preview every change, touch nothing:
    python -m scripts.backfill_usage_from_events --dry-run

    # Apply:
    python -m scripts.backfill_usage_from_events

    # One session (the APP session id, not the sdk/fluid one):
    python -m scripts.backfill_usage_from_events --session-id sess-abc --dry-run

Safe to run against a live database: each session is one small UPDATE, and the
values are derived from an append-only log, so a session that turns mid-run is
simply recomputed correctly by the next pass.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backfill_usage")

# Fluid serializes an event as its dataclass fields plus a ``_cls`` discriminator
# (see fluid/events/model.py::event_to_dict).
_USAGE_CLS = "Usage"
_TURN_COMPLETE_CLS = "TurnComplete"

_COLUMNS = (
    "total_tokens_in",
    "total_tokens_out",
    "total_cache_read_tokens",
    "total_cache_creation_tokens",
    "total_model_calls",
    "turn_count",
)


def _as_dict(event: Any) -> Dict[str, Any]:
    """A JSONB column comes back as a dict on psycopg, as text on some drivers."""
    if isinstance(event, dict):
        return event
    if isinstance(event, (str, bytes, bytearray)):
        try:
            loaded = json.loads(event)
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _totals_for(sdk_session_id: str) -> Optional[Dict[str, int]]:
    """Replay one session's fluid events into the six accounting numbers.

    Returns None when the session has no event log at all — that is "no evidence",
    which must not be confused with "zero", or the backfill would wipe whatever
    the old path did manage to record.
    """
    from app.db.base import get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT event FROM fluid_session_events WHERE session_id = {ph()} "
            f"ORDER BY seq",
            (sdk_session_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        return None

    totals = {c: 0 for c in _COLUMNS}
    for row in rows:
        event = _as_dict(row["event"] if hasattr(row, "keys") else row[0])
        cls = event.get("_cls")
        if cls == _USAGE_CLS:
            totals["total_tokens_in"] += _int(event.get("input_tokens"))
            totals["total_tokens_out"] += _int(event.get("output_tokens"))
            totals["total_cache_read_tokens"] += _int(event.get("cache_read_tokens"))
            totals["total_cache_creation_tokens"] += _int(
                event.get("cache_creation_tokens")
            )
            totals["total_model_calls"] += 1
        elif cls == _TURN_COMPLETE_CLS:
            totals["turn_count"] += 1

    if totals["total_model_calls"] == 0 and totals["turn_count"] == 0:
        # An event log that carries neither Usage nor TurnComplete tells us
        # nothing about accounting. Leave the row alone.
        return None
    return totals


def _sessions(session_id: Optional[str]) -> List[Dict[str, Any]]:
    from app.db.base import get_db
    from app.db.compat import ph

    where = "sdk_session_id IS NOT NULL AND sdk_session_id <> ''"
    params: List[Any] = []
    if session_id:
        where += f" AND id = {ph()}"
        params.append(session_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, sdk_session_id, title, "
            f"       {', '.join(_COLUMNS)} "
            f"FROM sessions WHERE {where} ORDER BY created_at",
            params,
        )
        return [dict(r) for r in cursor.fetchall()]


def _apply(session_id: str, totals: Dict[str, int]) -> None:
    from app.db.base import get_db
    from app.db.compat import ph

    assignments = ", ".join(f"{c} = {ph()}" for c in _COLUMNS)
    values = [totals[c] for c in _COLUMNS]
    values.append(session_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE sessions SET {assignments} WHERE id = {ph()}", values
        )


def _describe(row: Dict[str, Any], totals: Dict[str, int]) -> str:
    parts = []
    for column in _COLUMNS:
        before = _int(row.get(column))
        after = totals[column]
        marker = "" if before == after else "  <-- changed"
        parts.append(f"      {column:<28} {before:>12,} -> {after:>12,}{marker}")
    return "\n".join(parts)


def backfill(*, dry_run: bool, session_id: Optional[str] = None) -> int:
    rows = _sessions(session_id)
    if not rows:
        logger.info("No sessions with an sdk_session_id to examine.")
        return 0

    examined = skipped = partial = unchanged = changed = 0
    for row in rows:
        examined += 1
        totals = _totals_for(str(row["sdk_session_id"]))
        if totals is None:
            skipped += 1
            continue
        # Partial-evidence guard. ``sdk_session_id`` names only the session's
        # LATEST fluid event log; a session whose history spans re-keys or the
        # delegated leg has real usage that log never saw, and recomputing
        # absolute totals from the fragment would erase it. The repair only
        # ever ADDS previously unbooked usage — the fresh in/out semantics are
        # identical between the old path and the Usage events — so recomputed
        # fresh-token sums BELOW what is already booked mean missing evidence,
        # not a correction. Leave those rows alone.
        if (
            totals["total_tokens_in"] < _int(row.get("total_tokens_in"))
            or totals["total_tokens_out"] < _int(row.get("total_tokens_out"))
        ):
            partial += 1
            print(
                f"  {row['id']}  SKIPPED partial event log "
                f"(booked in/out {_int(row.get('total_tokens_in')):,}/"
                f"{_int(row.get('total_tokens_out')):,}, log holds only "
                f"{totals['total_tokens_in']:,}/{totals['total_tokens_out']:,})"
            )
            continue
        if all(_int(row.get(c)) == totals[c] for c in _COLUMNS):
            unchanged += 1
            continue
        changed += 1
        title = (row.get("title") or "(untitled)")[:60]
        print(f"  {row['id']}  {title}")
        print(_describe(row, totals))
        if not dry_run:
            _apply(str(row["id"]), totals)

    verb = "would update" if dry_run else "updated"
    logger.info(
        "%d session(s) examined: %s %d, already correct %d, "
        "no fluid event log %d, partial event log %d",
        examined, verb, changed, unchanged, skipped, partial,
    )
    if dry_run and changed:
        logger.info("Dry run — nothing was written.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute sessions' cumulative token columns, model calls and "
            "turn_count from the fluid event log."
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the before/after for every session and write nothing",
    )
    parser.add_argument(
        "--session-id", default=None,
        help="repair a single app session id instead of every session",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    from app.db.base import close_pool, init_pool

    init_pool(min_conn=1, max_conn=2)
    try:
        return backfill(dry_run=args.dry_run, session_id=args.session_id)
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
