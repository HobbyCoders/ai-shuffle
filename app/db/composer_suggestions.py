"""One durable next-reply hint per session, separate from messages and drafts."""
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


def save(session_id: str, turn_id: str, suggestion: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        # Serialize with supersede(): its activity rows and deletion must commit
        # before a racing generation checks whether its completion is current.
        cursor.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        # A late generation must not overwrite a newer turn's saved hint or
        # revive a hint after a same-id continuation / user message.
        cursor.execute(
            "INSERT INTO composer_suggestions (session_id, turn_id, suggestion) "
            f"SELECT %s, %s, %s WHERE %s = ({_LATEST_TURN}) "
            f"AND EXISTS ({_CURRENT_COMPLETION.format(session='%s', turn='%s')}) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "turn_id=excluded.turn_id, suggestion=excluded.suggestion",
            (session_id, turn_id, suggestion, turn_id, session_id, session_id, turn_id),
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
            f"WHERE s.session_id=%s AND s.turn_id = ({_LATEST_TURN}) "
            f"AND EXISTS ({_CURRENT_COMPLETION.format(session='s.session_id', turn='s.turn_id')})",
            (session_id, session_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def supersede(conn, session_id: str) -> None:
    """Remove a hint in the activity transaction; notify only after its commit."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
    cursor.execute(
        "DELETE FROM composer_suggestions WHERE session_id=%s RETURNING turn_id",
        (session_id,),
    )
    row = cursor.fetchone()
    if row:
        from app.core.composer_suggestions import notify_superseded

        after_commit(conn, lambda: notify_superseded(session_id, row["turn_id"]))
