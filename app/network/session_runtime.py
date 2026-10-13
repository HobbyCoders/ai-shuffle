"""Replicate display state without copying leases, turns, or background jobs.

Only the session owner samples its execution tables. Peers consume this small
summary through session-list enrichment; they cannot schedule work from it.
The regular catalog pump publishes transitions (including returning to idle).
"""
from typing import Dict, List, Optional

from app.db.base import get_db
from app.network import engine

STATES = frozenset({"working", "parked", "permission", "question", "idle"})


def publish_owned(ctx: engine.NetworkContext, *, session_ids: Optional[List[str]] = None) -> int:
    """Refresh and publish owned runtime summaries (only ``session_ids``, if given)."""
    from app.network.catalog import _my_instance_id

    if session_ids is not None and not session_ids:
        return 0
    only_params = tuple(session_ids or ())

    def only(column: str) -> str:
        return f" AND {column} IN ({', '.join(['%s'] * len(only_params))})" if only_params else ""

    with get_db() as conn:
        cursor = conn.cursor()
        # Network tables are bootstrapped before sessions, so this table has no
        # FK. Retract only our own deleted sessions; scans emit their tombstones.
        cursor.execute("""DELETE FROM network_session_runtime
            WHERE owner_device_id = %s AND NOT EXISTS
                (SELECT 1 FROM sessions s WHERE s.id = network_session_runtime.session_id)
        """ + only("session_id"), (ctx.device_id, *only_params))
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
                -- a sub-agent) means the chat is parked on it, marker or not.
                WHEN s.parked_task_kind IS NOT NULL OR EXISTS (SELECT 1 FROM background_tasks b
                    WHERE b.session_id = s.id AND b.status = 'running') THEN 'parked'
                ELSE 'idle' END AS state
            FROM sessions s
            WHERE (s.owner_device_id = %s OR
                (s.owner_device_id IS NULL AND s.instance_id = %s))
        """ + only("s.id"), (ctx.device_id, _my_instance_id(), *only_params))
        summaries = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT session_id, owner_device_id, state FROM network_session_runtime WHERE 1 = 1" + only("session_id"),
                       only_params)
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
