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

    One exception precedes it: deletion first takes its own chat's hand-off
    headers (``lock_handoff_history``). A publication can hold those for a
    whole history import, which must not stall every gated writer, and no
    gate holder ever waits for another chat's hand-off rows.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MULTI_SESSION_LOCK_KEY,))


def lock_handoff_history(cursor: Any, session_id: str) -> None:
    """Lock a chat's private hand-off headers (token order): the OUTERMOST lock.

    Order for a chat with staged hand-off history: headers H -> run -> event
    counter C -> session S. Publication, finalize and acceptance take H and
    then C/S; staging cleanup takes H then S. Deletion takes H here before its
    runtime prefix, because its FK cascade deletes the staged rows. Taking C
    or S first and reaching H through that cascade deadlocks (#1215). A
    header covers its token's chunk rows: nothing locks a chunk without it.
    """
    placeholder = ph()
    cursor.execute(
        f"SELECT token FROM handoff_history_chunks WHERE session_id = {placeholder} "
        "AND table_name = '' AND batch_no = 0 ORDER BY token FOR UPDATE",
        (session_id,),
    )
    cursor.fetchall()


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
