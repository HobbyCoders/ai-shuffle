"""One durable next-reply hint per session, separate from messages and drafts."""
from app.db.base import get_db


def save(session_id: str, turn_id: str, suggestion: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        # A late generation must not overwrite a newer turn's saved hint.
        cursor.execute(
            "INSERT INTO composer_suggestions (session_id, turn_id, suggestion) "
            "SELECT %s, %s, %s WHERE %s = (SELECT id FROM turns WHERE session_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "turn_id=excluded.turn_id, suggestion=excluded.suggestion",
            (session_id, turn_id, suggestion, turn_id, session_id),
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
            "WHERE s.session_id=%s AND EXISTS (SELECT 1 FROM session_events e "
            "WHERE e.session_id=s.session_id AND e.turn_id=s.turn_id "
            "AND e.event_type='turn.completed')",
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
