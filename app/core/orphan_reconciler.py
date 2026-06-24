"""
Self-healing reconciler for orphaned sessions.

Background
----------
A session row links to its Claude SDK transcript via ``sessions.sdk_session_id``.
That id is captured from the SDK ``init`` message early in a turn but, prior to
the persist-on-init fix in ``query_engine.py``, was only written to the row at
end-of-turn inside ``_persist_turn_results``. If a turn crashed mid-stream
(typically an **OOM/SIGKILL during a subagent fan-out**), the WS handler's
``except`` early-``return`` skipped that write and left ``sdk_session_id`` NULL.

In ``SESSION_STORE_MODE=postgres_only`` the history endpoints read the transcript
**only** from ``session_store_entries`` keyed by ``sdk_session_id``. A NULL link
therefore renders the chat blank even though the full transcript is sitting in
the store — the session is *orphaned*, not lost.

The persist-on-init fix prevents new orphans. This reconciler heals ones that
already happened (and any that slip through), by re-attaching the orphaned
transcript to its session row. Every heal is logged at **ERROR** level so the
operator is alerted that a crash occurred — healing is recovery, not "all clear".

Matching strategy (conservative — never mis-link)
-------------------------------------------------
For each session with ``sdk_session_id IS NULL`` we look for unlinked main-thread
transcripts (``subpath IS NULL``, not referenced by any session row) whose first
store entry was written within a tight window after the session's ``created_at``
(the SDK ``init`` lands within a second or two of the turn starting). We only
auto-link when there is **exactly one** candidate in the window. Zero candidates
→ a genuinely empty session (opened, never produced a turn). More than one →
ambiguous; we log a warning and leave it for manual review rather than risk
attaching the wrong transcript.
"""

import logging
from typing import Any, Dict, List, Optional

from app.db.base import get_db, get_cursor

logger = logging.getLogger(__name__)

# How long after a session's created_at the orphaned transcript's first entry
# may appear. The SDK init lands within ~1-2s; a generous window is fine because
# the exactly-one-candidate rule is what actually guards against mis-linking.
DEFAULT_WINDOW_SECONDS = 120


def find_orphan_candidates(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    max_age_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return one record per NULL-link session describing its match status.

    Each record: {session_id, created_at, confidence, candidates: [...]}.
    ``confidence`` is one of ``"confident"`` (exactly one candidate),
    ``"ambiguous"`` (>1), or ``"none"`` (0). This is read-only.
    """
    results: List[Dict[str, Any]] = []
    with get_db() as conn:
        cur = get_cursor(conn)

        age_clause = ""
        params: List[Any] = []
        if max_age_days is not None:
            age_clause = "AND created_at > now() - (%s || ' days')::interval"
            params.append(str(max_age_days))

        cur.execute(
            f"""
            SELECT id, created_at, status, turn_count, project_id, worktree_id
            FROM sessions
            WHERE sdk_session_id IS NULL
              {age_clause}
            ORDER BY created_at DESC
            """,
            params,
        )
        null_sessions = cur.fetchall()

        for s in null_sessions:
            created = s["created_at"]
            # Unlinked MAIN transcripts whose FIRST entry falls just after this
            # session was created. Window is asymmetric: a tiny lookback absorbs
            # clock skew, the forward window covers init latency.
            cur.execute(
                """
                SELECT e.session_id,
                       count(*)        AS n,
                       min(e.created_at) AS first_at,
                       max(e.created_at) AS last_at
                FROM session_store_entries e
                LEFT JOIN sessions s2 ON s2.sdk_session_id = e.session_id
                WHERE s2.id IS NULL
                  AND e.subpath IS NULL
                GROUP BY e.session_id
                HAVING min(e.created_at)
                       BETWEEN %s - interval '2 seconds'
                           AND %s + (%s || ' seconds')::interval
                ORDER BY min(e.created_at)
                """,
                (created, created, str(window_seconds)),
            )
            candidates = [dict(c) for c in cur.fetchall()]
            if len(candidates) == 1:
                confidence = "confident"
            elif len(candidates) > 1:
                confidence = "ambiguous"
            else:
                confidence = "none"
            results.append(
                {
                    "session_id": s["id"],
                    "created_at": created,
                    "status": s["status"],
                    "turn_count": s["turn_count"],
                    "confidence": confidence,
                    "candidates": candidates,
                }
            )
    return results


def reconcile_orphaned_sessions(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    max_age_days: Optional[int] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Relink orphaned sessions to their transcript. Returns healed records.

    Only sessions with exactly one candidate transcript in the window are
    healed. Each heal is logged at ERROR level (recovery implies a prior crash).
    Ambiguous matches are logged at WARNING and left untouched. Safe to call
    repeatedly; the UPDATE is guarded on ``sdk_session_id IS NULL``.
    """
    candidates = find_orphan_candidates(window_seconds, max_age_days)
    healed: List[Dict[str, Any]] = []
    ambiguous = 0

    for rec in candidates:
        if rec["confidence"] == "ambiguous":
            ambiguous += 1
            cand_ids = [c["session_id"] for c in rec["candidates"]]
            logger.warning(
                "[orphan-reconciler] session %s has %d candidate transcripts "
                "in window — NOT auto-linking (manual review): %s",
                rec["session_id"], len(rec["candidates"]), cand_ids,
            )
            continue
        if rec["confidence"] != "confident":
            continue

        cand = rec["candidates"][0]
        sdk_id = cand["session_id"]
        gap = (cand["first_at"] - rec["created_at"]).total_seconds()

        if dry_run:
            logger.info(
                "[orphan-reconciler] DRY-RUN would heal %s -> %s "
                "(%d entries, +%.1fs)",
                rec["session_id"], sdk_id, cand["n"], gap,
            )
            healed.append({**rec, "sdk_session_id": sdk_id, "entries": cand["n"], "dry_run": True})
            continue

        with get_db() as conn:
            cur = get_cursor(conn)
            # Guard on IS NULL so we never clobber a concurrently-written link.
            cur.execute(
                "UPDATE sessions SET sdk_session_id = %s "
                "WHERE id = %s AND sdk_session_id IS NULL",
                (sdk_id, rec["session_id"]),
            )
            updated = cur.rowcount

        if updated:
            # ERROR level on purpose: a heal means a session crashed mid-turn and
            # was orphaned. Surface it so the operator knows an issue occurred and
            # can investigate the underlying crash (usually an OOM during a
            # subagent fan-out), even though history is now recovered.
            logger.error(
                "[orphan-reconciler] HEALED orphaned session %s -> "
                "sdk_session_id=%s (%d store entries, first entry +%.1fs after "
                "creation). A turn crashed mid-stream and left the session "
                "unlinked; its chat history was blank until this relink. "
                "Investigate the crash (likely OOM during a subagent fan-out).",
                rec["session_id"], sdk_id, cand["n"], gap,
            )
            healed.append({**rec, "sdk_session_id": sdk_id, "entries": cand["n"], "dry_run": False})
        else:
            logger.info(
                "[orphan-reconciler] session %s already linked by the time we "
                "wrote — skipped", rec["session_id"],
            )

    if healed and not dry_run:
        logger.error(
            "[orphan-reconciler] healed %d orphaned session(s) this sweep "
            "(see per-session ERROR lines above)", len(healed),
        )
    elif ambiguous:
        logger.warning(
            "[orphan-reconciler] %d ambiguous orphan(s) left for manual review",
            ambiguous,
        )
    else:
        logger.debug("[orphan-reconciler] no orphaned sessions to heal")

    return healed
