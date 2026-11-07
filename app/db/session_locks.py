"""Shared row-lock order for operations mixing run fences, events and admission.

ONE order for every transaction that touches more than one of these locks.
Take them top to bottom; skip the ones a transaction does not need, never go
back up. PostgreSQL resolves a cycle by aborting one side (DeadlockDetected).

  1. G   multi-session gate     ``lock_multi_session_mutation`` (advisory)
  2. N   ``network_state`` row  ``store.lock_state`` / ``observe_clock``;
                                ``engine.record`` also writes it (clock bump)
  3. W   ``handoff_wakes`` rows of one chat, by token
  4. H   hand-off headers of one chat, by token (``lock_handoff_history``),
         then the active hold runs of those tokens, by run id
  5. R   ``runs`` of the chat(s), by (session id, run id)
  6. C   ``session_event_counters``, by session id
  7. S   ``sessions`` rows, by session id
  8.     everything else: turns, events, admission rows, mirrors, FK children

Who takes what (keep this table and the code in step):

  - local delete (``sessions.delete_session``): G, H, then R/C/S of the sorted
    family. Its FK cascade deletes the staged hand-off rows, so H must be held
    before S: reaching H through the cascade after S deadlocks (#1215).
  - bulk membership / settlement: G, N, then R/C/S of the sorted sessions.
  - incoming sync: N first. A session tombstone takes H (whole batch, before
    any counter) and then S through its DELETE (``engine.materialize``).
    Mirror events reserve C (``engine._lock_mirror_event_counters``).
  - hand-off publication / acceptance / finalize: W, H (+ hold runs), C, S.
    Staging cleanup (``handoff._discard_history``): H (+ hold runs), S.
  - source commit (``handoff._commit``): N, then its lease and hold runs in
    one sorted lock (``lock_handoff_holds``), then S. Its retry of an already
    transferred move and the cleanup after a refused commit lock only the
    hold runs, sorted, before releasing them.
  - source cancel (``handoff.finish`` phase "cancel"): the lease and hold runs
    in one sorted lock, then the release. Locking the moving chat's lease
    first and a parent's hold run later deadlocks a family delete.
  - export failure / begin's abandoned-attempt cleanup: ``runs.release_run``
    one run per transaction, or ``_discard_history`` (H, holds, S).
  - single-session append, turn admission, park repair: R, C, S
    (``lock_session_runtime``); never G.

A hand-off publication holds H (and C) for the whole history import. A local
delete of that chat therefore waits behind it while holding G; that bounded
wait is the price of one acyclic order (a delete taking H before G formed a
G -> N -> H -> G cycle with bulk membership and incoming sync deletion).
"""
import threading
from typing import Any, Iterable

from app.db.compat import ph

# Stable 64-bit namespace (ASCII "ASmultiS"), separate from schema/device locks.
_MULTI_SESSION_LOCK_KEY = 4707226386630469971

# Runner id prefix of a hand-off's run-slot reservation (``handoff.HOLD``).
HANDOFF_HOLD_PREFIX = "handoff-hold:"


def lock_multi_session_mutation(cursor: Any) -> None:
    """First blocking lock (G) for a short, synchronous multi-session transaction.

    Family discovery is repeated after this gate: sorting an earlier snapshot
    misses a newly inserted child and can still reverse parent/child order.
    Never span provider/network calls or other awaits with this transaction.
    Ordinary append, turn admission and single-session repair MUST NOT use it.
    PostgreSQL is the only runtime DB; the serialized unit-test shim treats
    advisory transaction locks as no-ops, like the existing schema/slot locks.
    Nothing may be locked before it (see the module's lock-order table).
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MULTI_SESSION_LOCK_KEY,))


def lock_handoff_history(cursor: Any, session_id: str) -> list:
    """Lock a chat's hand-off headers (H) in token order, then their hold runs.

    Taken after G/N/W and before R/C/S (module table). Deletion and incoming
    session tombstones take it because their FK cascade deletes the staged
    rows; publication, acceptance and finalize because they later take C/S
    and release the hold runs. A header covers its token's chunk rows:
    nothing locks a chunk without it. Returns the locked tokens.
    """
    placeholder = ph()
    cursor.execute(
        f"SELECT token FROM handoff_history_chunks WHERE session_id = {placeholder} "
        "AND table_name = '' AND batch_no = 0 ORDER BY token FOR UPDATE",
        (session_id,),
    )
    tokens = [row["token"] for row in cursor.fetchall()]
    lock_handoff_holds(cursor, tokens)
    return tokens


def lock_handoff_holds(cursor: Any, tokens: Iterable[str], *, run_ids: Iterable[str] = ()) -> None:
    """Lock the active hold runs of these hand-off tokens (part of step H).

    Acceptance and cleanup release them after taking C/S. Locking them only
    then reverses R -> C -> S against a writer that holds the run (#1215).
    Hold runs belong to other chats, so they are taken in (session, id)
    order, the order ``lock_session_runtimes`` uses for a family. ``run_ids``
    joins extra runs (the source commit's lease) into that same sorted lock.
    No statement when there is nothing to lock.
    """
    holders = sorted({HANDOFF_HOLD_PREFIX + str(token) for token in tokens})
    extra = sorted({str(run_id) for run_id in run_ids})
    if not holders and not extra:
        return
    placeholder = ph()
    clauses, params = [], []
    if holders:
        clauses.append(f"(runner_id IN ({', '.join([placeholder] * len(holders))}) AND state = 'active')")
        params.extend(holders)
    if extra:
        clauses.append(f"id IN ({', '.join([placeholder] * len(extra))})")
        params.extend(extra)
    cursor.execute(
        f"SELECT id FROM runs WHERE {' OR '.join(clauses)} ORDER BY session_id, id FOR UPDATE",
        tuple(params),
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


# Chats deleted in this process, or seen deleted under a mirror reservation
# (#1222). A mirror op that arrives after the delete must store nothing, but
# an absent session row alone can also mean "not arrived yet". Deletion marks
# the chat while it holds the chat's counter; a reservation that finds the row
# again forgets the mark, so a rolled-back delete never hides a live chat.
# Only a cache: the reservation falls back to sync_records (durable, survives
# eviction and restart), so losing a mark costs one lookup, never correctness.
_deleted_lock = threading.Lock()
_DELETED: dict[str, None] = {}
_DELETED_MAX = 1024


def mark_sessions_deleted(session_ids: Iterable[str]) -> None:
    with _deleted_lock:
        for sid in session_ids:
            _DELETED.pop(sid, None)
            _DELETED[sid] = None
        while len(_DELETED) > _DELETED_MAX:
            _DELETED.pop(next(iter(_DELETED)))


def deleted_sessions(candidates: Iterable[str], *, present: Iterable[str] = ()) -> set:
    """Forget marks of chats found present, then return the marked candidates."""
    with _deleted_lock:
        for sid in present:
            _DELETED.pop(sid, None)
        return {sid for sid in candidates if sid in _DELETED}
