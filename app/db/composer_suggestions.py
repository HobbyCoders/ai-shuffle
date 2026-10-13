"""One durable next-reply hint per session, separate from messages and drafts."""
import hashlib

from app.db.base import after_commit, get_db


_LATEST_TURN = (
    "SELECT CASE WHEN stop_reason='steered' THEN COALESCE(parent_turn_id, id) ELSE id END "
    "FROM turns WHERE session_id=%s ORDER BY created_at DESC, id DESC LIMIT 1"
)
# A continued turn can reuse its id. Completion must follow *all* activity,
# not merely exist somewhere in that turn's history.
_CURRENT_COMPLETION = (
    "SELECT 1 FROM session_events e WHERE e.session_id={session} AND e.turn_id={turn} "
    "AND e.event_type='turn.completed' AND NOT EXISTS ("
    "SELECT 1 FROM session_events a WHERE a.session_id=e.session_id "
    "AND a.seq>e.seq AND a.event_type IN ('turn.started', 'message.user'))"
)


def _lock(cursor, session_id: str) -> None:
    # Match device_identity's namespaced advisory lock pattern. A row lock on
    # sessions would conflict with activity inserts' foreign-key locks.
    key = int.from_bytes(hashlib.sha256(
        ("shuffle-composer-suggestion:" + session_id).encode("utf-8")
    ).digest()[:8], "big", signed=True)
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def revision(session_id: str) -> int | None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT activity_revision FROM composer_suggestion_revisions WHERE session_id=%s",
                       (session_id,))
        row = cursor.fetchone()
        return row["activity_revision"] if row else None


def complete(conn, session_id: str) -> None:
    """A durable completion acknowledges all preceding activity, including legacy writes."""
    cursor = conn.cursor()
    _lock(cursor, session_id)
    cursor.execute(
        "INSERT INTO composer_suggestion_revisions (session_id, completed_revision) "
        "SELECT id, 0 FROM sessions WHERE id=%s "
        "ON CONFLICT (session_id) DO UPDATE SET completed_revision=composer_suggestion_revisions.activity_revision",
        (session_id,),
    )


def save(session_id: str, turn_id: str, suggestion: str, *, expected_revision: int | None = None) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        # Serialize with supersede(): its activity rows and deletion must commit
        # before a racing generation checks whether its completion is current.
        _lock(cursor, session_id)
        # A late generation must not overwrite a newer turn's saved hint or
        # revive a hint after a same-id continuation / user message.
        cursor.execute(
            "INSERT INTO composer_suggestions (session_id, turn_id, suggestion, activity_revision) "
            "SELECT %s, %s, %s, r.activity_revision FROM composer_suggestion_revisions r "
            "WHERE r.session_id=%s AND r.completed_revision=r.activity_revision "
            "AND (CAST(%s AS BIGINT) IS NULL OR r.activity_revision=%s) "
            f"AND %s = ({_LATEST_TURN}) "
            f"AND EXISTS ({_CURRENT_COMPLETION.format(session='%s', turn='%s')}) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "turn_id=excluded.turn_id, suggestion=excluded.suggestion, activity_revision=excluded.activity_revision",
            (session_id, turn_id, suggestion, session_id, expected_revision, expected_revision,
             turn_id, session_id, session_id, turn_id),
        )
        saved = cursor.rowcount > 0
        conn.commit()
        return saved


def get(session_id: str) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor()
        # Rewind/clear removes completion events. Never revive their old hint.
        cursor.execute(
            "SELECT s.session_id, s.turn_id, s.suggestion FROM composer_suggestions s "
            "JOIN composer_suggestion_revisions r ON r.session_id=s.session_id "
            "AND r.activity_revision=s.activity_revision AND r.completed_revision=r.activity_revision "
            f"WHERE s.session_id=%s AND s.turn_id = ({_LATEST_TURN}) "
            f"AND EXISTS ({_CURRENT_COMPLETION.format(session='s.session_id', turn='s.turn_id')})",
            (session_id, session_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def supersede(conn, session_id: str) -> None:
    """Remove a hint in the activity transaction; notify only after its commit."""
    cursor = conn.cursor()
    _lock(cursor, session_id)
    cursor.execute(
        "INSERT INTO composer_suggestion_revisions (session_id, activity_revision) "
        "SELECT id, 1 FROM sessions WHERE id=%s "
        "ON CONFLICT (session_id) DO UPDATE SET activity_revision=composer_suggestion_revisions.activity_revision+1",
        (session_id,),
    )
    cursor.execute(
        "DELETE FROM composer_suggestions WHERE session_id=%s RETURNING turn_id",
        (session_id,),
    )
    row = cursor.fetchone()
    if row:
        from app.core.composer_suggestions import notify_superseded

        after_commit(conn, lambda: notify_superseded(session_id, row["turn_id"]))
