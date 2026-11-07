"""Shared row-lock order for operations mixing run fences, events and admission."""
from typing import Any

from app.db.compat import ph


def lock_session_runtime(cursor: Any, session_id: str):
    """Lock run -> event counter -> session, before dependent/turn rows.

    Use this BEFORE taking admission/turn locks if the transaction can append
    events or expire a run. It matches fenced append and deletion; taking just
    admission first reverses the recorder's counter order. Return the locked
    session row (or None when deletion won). The caller owns the transaction.
    """
    placeholder = ph()
    cursor.execute(
        f"SELECT id FROM runs WHERE session_id = {placeholder} ORDER BY id FOR UPDATE",
        (session_id,),
    )
    cursor.fetchall()
    cursor.execute(
        "INSERT INTO session_event_counters (session_id, next_seq) "
        f"SELECT id, 0 FROM sessions WHERE id = {placeholder} "
        "ON CONFLICT (session_id) DO UPDATE "
        "SET next_seq = session_event_counters.next_seq",
        (session_id,),
    )
    cursor.execute(f"SELECT * FROM sessions WHERE id = {placeholder} FOR UPDATE", (session_id,))
    row = cursor.fetchone()
    if row is None:
        # With no run row to wait on, the INSERT's snapshot can predate a
        # delete whose counter lock it then waits behind. A no-op settlement
        # must not commit the counter that INSERT recreated after deletion.
        cursor.execute(f"DELETE FROM session_event_counters WHERE session_id = {placeholder}", (session_id,))
    return row
