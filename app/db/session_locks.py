"""Shared row-lock order for operations mixing run fences, events and admission."""
from typing import Any

from app.db.compat import ph

# Stable 64-bit namespace (ASCII "ASmultiS"), separate from schema/device locks.
_MULTI_SESSION_LOCK_KEY = 4707226386630469971


def lock_multi_session_mutation(cursor: Any) -> None:
    """First blocking lock for a short, synchronous multi-session transaction.

    Family discovery is repeated after this gate: sorting an earlier snapshot
    misses a newly inserted child and can still reverse parent/child order.
    Never span provider/network calls or other awaits with this transaction.
    Ordinary append, turn admission and single-session repair MUST NOT use it.
    PostgreSQL is the only runtime DB; the serialized unit-test shim treats
    advisory transaction locks as no-ops, like the existing schema/slot locks.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MULTI_SESSION_LOCK_KEY,))


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


def lock_session_runtimes(cursor: Any, session_ids):
    """Take every requested runtime prefix in stable session-ID order."""
    return {sid: lock_session_runtime(cursor, sid) for sid in sorted(set(session_ids))}
