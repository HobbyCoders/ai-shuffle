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
import time
from typing import Any, Dict, List, Optional

from app.db.base import get_db, get_cursor

logger = logging.getLogger(__name__)

# How long after a session's created_at the orphaned transcript's first entry
# may appear. The SDK init lands within ~1-2s; a generous window is fine because
# the exactly-one-candidate rule is what actually guards against mis-linking.
DEFAULT_WINDOW_SECONDS = 120

# A session is only a heal candidate once it is at least this old. A genuinely
# orphaned session was left NULL by a crash that already happened, so it will
# always be older than this floor by the time a sweep runs; a session that is
# merely mid-FIRST-turn (init landed ~1s ago, end-of-turn persist not reached
# yet) is younger. Without this floor the reconciler "heals" healthy running
# sessions — and because the timing heuristic can match the SAME live transcript
# to two young sessions created seconds apart, it double-relinks them to one
# sdk_session_id. PR #477's init-time early link starves this for the app's own
# query paths; the floor + in-sweep/DB uniqueness below defend the reconciler
# itself.
MIN_SESSION_AGE_SECONDS = 60

# ─── Inconclusive-result memo (F6) ────────────────────────────────────────
#
# The overwhelming majority of NULL-link sessions are not orphans at all: they
# are sessions that were opened and never produced a turn. They stay NULL
# forever, so every sweep re-examined every one of them — and the examination is
# an aggregation over the whole session store. On the old 5-minute cadence that
# was the heaviest recurring query on the box, repeated for 14 days per session,
# to re-derive the same "no candidates" answer every time.
#
# We therefore remember which session ids came back INCONCLUSIVE ("none" or
# "ambiguous") and skip them until the memo entry ages out. Two properties make
# this safe:
#
#   * It can only ever DELAY a heal, never prevent one. The memo is per-process
#     and TTL-bounded, so a session that later becomes healable (it gained store
#     entries after a crash) is re-examined within MEMO_TTL_SECONDS, and again
#     on every restart. Healing is a recovery path with no deadline — the
#     transcript is not going anywhere.
#   * It never memoizes a CONFIDENT result, so nothing that could be healed is
#     ever skipped on the strength of a previous look.
#
# Combined with the single-aggregation rewrite below (one GROUP BY per sweep
# instead of one per NULL session), a steady-state sweep costs exactly one small
# indexed SELECT and nothing else.
MEMO_TTL_SECONDS = 6 * 3600

# Bound on the memo so a database full of never-used sessions cannot grow it
# without limit. Oldest entries are dropped first; a dropped entry just means
# that session is re-examined sooner.
MEMO_MAX_ENTRIES = 5000

# session_id -> monotonic deadline after which it must be re-examined.
_inconclusive_until: Dict[str, float] = {}


def _now() -> float:
    """Monotonic clock, indirected so tests can advance it."""
    return time.monotonic()


def reset_memo() -> None:
    """Forget every memoized inconclusive result (tests / operator tooling)."""
    _inconclusive_until.clear()


def _memo_skip(session_id: str) -> bool:
    """True when this session was recently examined with no usable candidate."""
    deadline = _inconclusive_until.get(session_id)
    if deadline is None:
        return False
    if _now() >= deadline:
        _inconclusive_until.pop(session_id, None)
        return False
    return True


def _memo_record(session_id: str) -> None:
    """Remember that ``session_id`` came back inconclusive just now."""
    if MEMO_TTL_SECONDS <= 0:
        return
    _inconclusive_until[session_id] = _now() + MEMO_TTL_SECONDS
    while len(_inconclusive_until) > MEMO_MAX_ENTRIES:
        oldest = min(_inconclusive_until, key=_inconclusive_until.get)
        _inconclusive_until.pop(oldest, None)


def find_orphan_candidates(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    max_age_days: Optional[int] = None,
    min_age_seconds: int = MIN_SESSION_AGE_SECONDS,
    skip_memoized: bool = False,
) -> List[Dict[str, Any]]:
    """Return one record per NULL-link session describing its match status.

    Each record: {session_id, created_at, confidence, candidates: [...]}.
    ``confidence`` is one of ``"confident"`` (exactly one candidate),
    ``"ambiguous"`` (>1), or ``"none"`` (0). This is read-only.

    Sessions younger than ``min_age_seconds`` are excluded: a NULL link that
    recent belongs to a session that is still mid-first-turn, not one orphaned
    by a past crash. Healing it would clobber a live run — and can double-link
    two young sessions to one live transcript (see ``MIN_SESSION_AGE_SECONDS``).

    ``skip_memoized`` (used by the periodic sweep, not by operator inspection)
    drops sessions whose last examination was inconclusive and is still inside
    ``MEMO_TTL_SECONDS`` — see the memo notes above.

    COST (F6): the unlinked-transcript aggregation runs ONCE per call, over the
    union of every examined session's match window, and its rows are then
    assigned to individual sessions in Python. It used to run once PER NULL
    session with the per-session window inlined into ``HAVING``. The two are
    equivalent — the per-session ``HAVING`` was a filter on ``min(created_at)``,
    which is exactly what the Python assignment now applies — but the rewrite
    turns N aggregations over ``session_store_entries`` into one. When every
    NULL session is memoized, the aggregation is skipped entirely.
    """
    results: List[Dict[str, Any]] = []
    with get_db() as conn:
        cur = get_cursor(conn)

        age_clause = ""
        params: List[Any] = []
        if max_age_days is not None:
            age_clause = "AND created_at > now() - (%s || ' days')::interval"
            params.append(str(max_age_days))

        min_age_clause = ""
        if min_age_seconds and min_age_seconds > 0:
            min_age_clause = "AND created_at < now() - (%s || ' seconds')::interval"
            params.append(str(min_age_seconds))

        cur.execute(
            f"""
            SELECT id, created_at, status, turn_count, project_id, worktree_id
            FROM sessions
            WHERE sdk_session_id IS NULL
              {age_clause}
              {min_age_clause}
            ORDER BY created_at DESC
            """,
            params,
        )
        null_sessions = cur.fetchall()

        if skip_memoized:
            null_sessions = [
                s for s in null_sessions if not _memo_skip(s["id"])
            ]
        if not null_sessions:
            return results

        # ONE aggregation over the union of every examined session's window.
        # Unlinked MAIN transcripts whose FIRST entry falls just after some
        # examined session was created. The window is asymmetric: a tiny
        # lookback absorbs clock skew, the forward window covers init latency.
        created_values = [s["created_at"] for s in null_sessions]
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
            (min(created_values), max(created_values), str(window_seconds)),
        )
        transcripts = [dict(c) for c in cur.fetchall()]

        for s in null_sessions:
            created = s["created_at"]
            # Per-session window filter — identical to the HAVING clause the
            # old per-session query applied, just evaluated here instead.
            candidates = [
                t for t in transcripts
                if t["first_at"] is not None
                and _in_window(t["first_at"], created, window_seconds)
            ]
            if len(candidates) == 1:
                confidence = "confident"
            elif len(candidates) > 1:
                confidence = "ambiguous"
            else:
                confidence = "none"
            if confidence != "confident":
                _memo_record(s["id"])
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


def _in_window(first_at: Any, created: Any, window_seconds: int) -> bool:
    """``created - 2s <= first_at <= created + window_seconds``.

    Mirrors the SQL ``HAVING ... BETWEEN`` the per-session query used to run, so
    the single-aggregation rewrite selects exactly the same candidate sets.
    """
    try:
        delta = (first_at - created).total_seconds()
    except (TypeError, AttributeError):
        return False
    return -2.0 <= delta <= float(window_seconds)


def reconcile_orphaned_sessions(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    max_age_days: Optional[int] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Relink orphaned sessions to their transcript. Returns healed records.

    Only sessions with exactly one candidate transcript in the window are
    healed. Each heal is logged at ERROR level (recovery implies a prior crash).
    Ambiguous matches are logged at WARNING and left untouched. Safe to call
    repeatedly; the UPDATE is guarded on ``sdk_session_id IS NULL`` and refuses
    to assign a transcript that any session already owns (both an in-sweep
    ``claimed_sdk_ids`` set and a ``NOT EXISTS`` write guard), so it can never
    double-link two sessions to one ``sdk_session_id``.

    Uses the inconclusive-result memo (``skip_memoized=True``): sessions that
    were recently examined and had no usable candidate are not re-examined until
    the memo entry ages out. That can only delay a heal, never skip one — see
    the memo notes at the top of this module.
    """
    candidates = find_orphan_candidates(
        window_seconds, max_age_days, skip_memoized=True
    )
    healed: List[Dict[str, Any]] = []
    ambiguous = 0
    # sdk_session_ids claimed earlier in THIS sweep. find_orphan_candidates runs
    # one read pass before any write, so it cannot know that an earlier iteration
    # already took a transcript: two orphaned sessions created seconds apart can
    # each list the same lone transcript as their single "confident" candidate.
    # The per-row ``sdk_session_id IS NULL`` guard protects each TARGET row but
    # does nothing to stop one transcript being written onto two different rows.
    # Track claims here and refuse the second — two sessions must never share an
    # sdk_session_id (it would render the same transcript under both chats).
    claimed_sdk_ids: set = set()

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

        if sdk_id in claimed_sdk_ids:
            # A transcript already claimed by an earlier session this sweep.
            # Two sessions matched one transcript — leave this one for manual
            # review rather than double-link and corrupt both histories.
            ambiguous += 1
            logger.warning(
                "[orphan-reconciler] session %s matched transcript %s which was "
                "already claimed by another session this sweep — NOT double-"
                "linking (manual review).",
                rec["session_id"], sdk_id,
            )
            continue

        if dry_run:
            logger.info(
                "[orphan-reconciler] DRY-RUN would heal %s -> %s "
                "(%d entries, +%.1fs)",
                rec["session_id"], sdk_id, cand["n"], gap,
            )
            claimed_sdk_ids.add(sdk_id)
            healed.append({**rec, "sdk_session_id": sdk_id, "entries": cand["n"], "dry_run": True})
            continue

        with get_db() as conn:
            cur = get_cursor(conn)
            # Two guards, both required:
            #  - ``id = ... AND sdk_session_id IS NULL`` — never clobber a link
            #    another writer (a concurrent turn) may have just written.
            #  - ``NOT EXISTS (... sdk_session_id = %s)`` — never assign a
            #    transcript that ANOTHER session already owns. This is the
            #    DB-level twin of the in-sweep ``claimed_sdk_ids`` guard: it also
            #    stops a concurrent reconcile pass (another process/interval)
            #    from creating the duplicate we just refused to make in-process.
            cur.execute(
                "UPDATE sessions SET sdk_session_id = %s "
                "WHERE id = %s AND sdk_session_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM sessions s3 "
                "                WHERE s3.sdk_session_id = %s)",
                (sdk_id, rec["session_id"], sdk_id),
            )
            updated = cur.rowcount

        if updated:
            claimed_sdk_ids.add(sdk_id)
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
            # Either the target row was linked by a concurrent turn, or another
            # session already owns this transcript (the NOT EXISTS guard) — a
            # concurrent reconcile pass beat us. Both mean "do not double-link".
            logger.info(
                "[orphan-reconciler] session %s not healed: already linked, or "
                "transcript %s already owned by another session — skipped",
                rec["session_id"], sdk_id,
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
