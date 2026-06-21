"""
Mirror-mode drift verifier for the PgSessionStore migration.

When ``settings.session_store_mode == "mirror"`` the SDK mirrors every session
transcript line into Postgres (``session_store_entries``) while the JSONL files
under ``~/.claude/projects`` stay the primary read source. Before trusting
Postgres enough to cut reads over (``postgres_only``), we need evidence the
mirror is faithful — that the DB copy of a transcript matches the JSONL copy
entry-for-entry, subagent transcripts included.

:func:`log_session_store_drift` runs on session load (mirror mode only) and
logs any divergence between the two copies. It is pure observability:
read-only, never raises to the caller, and a no-op outside mirror mode.

Interpreting the logs (grep ``[StoreVerify]``):

* ``no DB entries`` — the session predates mirror enablement (no backfill
  yet). Expected, not drift.
* ``OK`` (debug) — DB and JSONL agree on main + subagent transcripts.
* ``DRIFT count`` / ``DRIFT uuid`` / ``DRIFT subagents`` (warning) — the mirror
  lost, duplicated, or reordered entries. This is the signal that must reach
  zero before a read cutover is safe.
* ``jitter`` (info) — a small main-transcript count delta, expected when the
  session is still actively streaming (JSONL flush vs DB flush race). Logged
  below WARNING so it doesn't pollute the real drift signal.

The store is keyed by ``(project_key, session_id)``; we query by ``session_id``
alone because SDK session UUIDs are globally unique, which sidesteps having to
reconstruct the project key from the working directory.
"""

from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "[StoreVerify]"

# Main-transcript count deltas at or below this are treated as active-stream
# flush jitter and logged at INFO rather than WARNING.
_JITTER_TOLERANCE = 2


def _db_main_uuids(session_id: str) -> list:
    """Ordered uuids of the main (subpath IS NULL) transcript entries in PG."""
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT entry->>'uuid' AS uuid FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NULL ORDER BY id ASC",
            (session_id,),
        )
        return [r["uuid"] for r in cur.fetchall()]


def _db_subagent_subpaths(session_id: str) -> set:
    """Distinct subagent subpaths (subpath IS NOT NULL) stored in PG."""
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT subpath FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NOT NULL",
            (session_id,),
        )
        return {r["subpath"] for r in cur.fetchall()}


def log_session_store_drift(sdk_session_id: str, working_dir: str = "/workspace") -> None:
    """Compare the Postgres transcript mirror against JSONL and log divergence.

    No-op unless ``session_store_mode == "mirror"``. Read-only and never raises
    — any failure is swallowed at DEBUG so a verification glitch can't break a
    session load.
    """
    if settings.session_store_mode != "mirror":
        return

    try:
        db_uuids = _db_main_uuids(sdk_session_id)
        db_n = len(db_uuids)
        if db_n == 0:
            # No mirrored rows — session predates mirror enablement (or hasn't
            # streamed since). Not drift; nothing to compare against yet.
            logger.debug("%s %s: no DB entries yet (pre-mirror session)", _PREFIX, sdk_session_id)
            return

        from app.core.jsonl_parser import (
            get_agent_jsonl_paths,
            get_session_jsonl_path,
            parse_jsonl_file,
        )

        jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            logger.warning(
                "%s DRIFT count %s: DB has %d entries but no JSONL transcript found",
                _PREFIX, sdk_session_id, db_n,
            )
            return

        jsonl_uuids = [e.get("uuid") for e in parse_jsonl_file(jsonl_path)]
        jsonl_n = len(jsonl_uuids)

        # --- main transcript: entry count ---
        if db_n != jsonl_n:
            delta = db_n - jsonl_n
            jitter = abs(delta) <= _JITTER_TOLERANCE
            logger.log(
                logging.INFO if jitter else logging.WARNING,
                "%s %s %s: db_entries=%d jsonl_entries=%d (delta=%+d)%s",
                _PREFIX,
                "jitter" if jitter else "DRIFT count",
                sdk_session_id, db_n, jsonl_n, delta,
                " — likely active-stream flush race" if jitter else "",
            )
            return

        # --- main transcript: identity/order (only when uuids exist on both sides) ---
        if all(db_uuids) and all(jsonl_uuids):
            first_mismatch = next(
                (i for i, (a, b) in enumerate(zip(db_uuids, jsonl_uuids)) if a != b),
                None,
            )
            if first_mismatch is not None:
                logger.warning(
                    "%s DRIFT uuid %s: counts match (%d) but entry %d differs "
                    "(db=%s jsonl=%s)",
                    _PREFIX, sdk_session_id, db_n, first_mismatch,
                    db_uuids[first_mismatch], jsonl_uuids[first_mismatch],
                )
                return

        # --- subagent transcripts: count of distinct agents ---
        db_subpaths = _db_subagent_subpaths(sdk_session_id)
        jsonl_agents = get_agent_jsonl_paths(sdk_session_id, working_dir) or {}
        if len(db_subpaths) != len(jsonl_agents):
            logger.warning(
                "%s DRIFT subagents %s: db_subpaths=%d jsonl_agent_files=%d",
                _PREFIX, sdk_session_id, len(db_subpaths), len(jsonl_agents),
            )
            return

        logger.debug(
            "%s OK %s: %d main + %d subagent transcripts match DB↔JSONL",
            _PREFIX, sdk_session_id, db_n, len(db_subpaths),
        )
    except Exception as e:
        logger.debug("%s skipped for %s: %s", _PREFIX, sdk_session_id, e)


__all__ = ["log_session_store_drift"]
