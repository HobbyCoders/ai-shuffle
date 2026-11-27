"""Replicate display state without copying leases, turns, or background jobs.

Only the session owner samples its execution tables. Peers consume this small
summary through session-list enrichment; they cannot schedule work from it.
The regular catalog pump publishes transitions (including returning to idle).
"""
from typing import Dict, List, Optional

from app.db import turns
from app.db.base import get_db
from app.network import engine

# "failed" is "idle" whose latest turn ended in a failure: nothing is running, and
# a mirror has no turns of its own to read that from.
STATES = frozenset({"working", "parked", "permission", "question", "idle", "failed"})


def prune_owned(*, stop=None, ctx=None, session_ids=None):
    """Retract orphan summaries in the expiry task or a targeted deletion pass."""
    from app.db.retention import mutate_batches
    if stop is not None and stop.is_set():
        return 0
    ctx = ctx or engine.context()
    if ctx is None or session_ids == []:
        return 0
    where = """owner_device_id = %s AND NOT EXISTS
        (SELECT 1 FROM sessions s WHERE s.id = network_session_runtime.session_id)"""
    params = (ctx.device_id,)
    if session_ids is not None:
        where += " AND session_id IN (" + ", ".join(["%s"] * len(session_ids)) + ")"
        params += tuple(session_ids)
    return mutate_batches("network_session_runtime", where, params,
        keys=("session_id",), stop=stop)


def _only(column: str, params: tuple) -> str:
    return f" AND {column} IN ({', '.join(['%s'] * len(params))})" if params else ""


def owned_states(device_id: Optional[str], *, session_ids: Optional[List[str]] = None) -> Dict[str, str]:
    """``{session_id: state}`` sampled now for the chats this worker runs.

    ``device_id``: this worker's network id (None when not networked; rows
    with no owner are matched on the install id either way). ``session_ids``
    limits the read; an empty list reads nothing.
    """
    from app.network.catalog import _my_instance_id
    from app.db.wakeups import armed_wake_sql

    if session_ids is not None and not session_ids:
        return {}
    only_params = tuple(session_ids or ())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, CASE
                WHEN EXISTS (SELECT 1 FROM pending_interactions p
                    WHERE p.session_id = s.id AND p.state = 'pending'
                    AND (p.expires_at IS NULL OR p.expires_at >= NOW())
                    AND p.kind <> 'question') THEN 'permission'
                WHEN EXISTS (SELECT 1 FROM pending_interactions p
                    WHERE p.session_id = s.id AND p.state = 'pending'
                    AND (p.expires_at IS NULL OR p.expires_at >= NOW())) THEN 'question'
                WHEN EXISTS (SELECT 1 FROM runs r WHERE r.session_id = s.id
                    AND r.state = 'active' AND r.lease_expires_at > NOW())
                    OR EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.id
                        AND t.state IN ('queued', 'running', 'blocked_setup')) THEN 'working'
                -- No turn is running: live background work (a Monitor shell,
                -- a sub-agent) or a scheduled wakeup means the chat is parked
                -- on it, marker or not.
                WHEN s.parked_task_kind IS NOT NULL OR EXISTS (SELECT 1 FROM background_tasks b
                    WHERE b.session_id = s.id AND b.status = 'running')
                    OR """ + armed_wake_sql("s.id") + """ THEN 'parked'
                ELSE 'idle' END AS state
            FROM sessions s
            WHERE (s.owner_device_id = %s OR
                (s.owner_device_id IS NULL AND s.instance_id = %s))
        """ + _only("s.id", only_params), (device_id, _my_instance_id(), *only_params))
        states = {r["id"]: r["state"] for r in cursor.fetchall()}

    failed = turns.sessions_with_failed_latest_turn([sid for sid, state in states.items() if state == "idle"])
    return {sid: "failed" if sid in failed else state for sid, state in states.items()}


def publish_owned(ctx: engine.NetworkContext, *, session_ids: Optional[List[str]] = None) -> int:
    """Refresh and publish owned runtime summaries (only ``session_ids``, if given)."""
    if session_ids is not None and not session_ids:
        return 0
    only_params = tuple(session_ids or ())
    summaries = [{"id": sid, "state": state}
                 for sid, state in owned_states(ctx.device_id, session_ids=session_ids).items()]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id, owner_device_id, state FROM network_session_runtime WHERE 1 = 1"
                       + _only("session_id", only_params), only_params)
        previous = {r["session_id"]: dict(r) for r in cursor.fetchall()}
        for row in summaries:
            old = previous.get(row["id"], {})
            if old.get("state") == row["state"] and old.get("owner_device_id") == ctx.device_id:
                continue
            cursor.execute("""
                INSERT INTO network_session_runtime (session_id, owner_device_id, state)
                VALUES (%s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET
                    owner_device_id = EXCLUDED.owner_device_id, state = EXCLUDED.state,
                    updated_at = NOW()
            """, (row["id"], ctx.device_id, row["state"]))
    return engine.scan_collection("session_runtime", ctx=ctx, owner_only=True, record_ids=session_ids)


def for_sessions(session_ids: List[str]) -> Dict[str, dict]:
    """One bounded read for the sidebar page, not one request per owner/session."""
    if not session_ids:
        return {}
    with get_db() as conn:
        cursor = conn.cursor()
        placeholders = ", ".join(["%s"] * len(session_ids))
        cursor.execute(
            f"SELECT * FROM network_session_runtime WHERE session_id IN ({placeholders})",
            tuple(session_ids),
        )
        return {r["session_id"]: dict(r) for r in cursor.fetchall()}
