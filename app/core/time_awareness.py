"""Thread-side, fail-soft owner time context for all harness legs."""
from __future__ import annotations

import json

from app.db.base import get_db


def preference_identity(session, principal=None):
    from app.core.preference_identity import principal_preference_identity
    from app.db.mobile_tool_identity import mirrored

    if session.get("api_user_id"):
        principal = {"kind": "api_user", "principal_id": session["api_user_id"]}
    else:
        principal = principal or mirrored(session) or {"kind": "admin"}
    return principal_preference_identity(principal)


def session_time_context(session_id: str | None, source: str = "wakeup") -> dict:
    """Called off-loop for each turn. Current owner input is already in the log.

    Reads at most two indexed human-message rows, with a short statement deadline.
    Only human sources count, never automation/task/peer/hand-off deliveries.
    The source determines whether to skip the current human input, not whether
    to show the gap: automatic wakes use the last human input itself.
    """
    if not session_id:
        return {}
    result = {}
    try:
        from app.db import database

        session = database.get_session(session_id) or {}
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SET LOCAL statement_timeout = '250ms'")
            cur.execute("SELECT kind, principal_id, account_id FROM mobile_tool_identity WHERE session_id = %s", (session_id,))
            identity = preference_identity(session, cur.fetchone())
            cur.execute("SELECT key, value FROM user_preferences WHERE user_type = %s AND user_id = %s "
                        "AND key IN ('timezone', 'timezone_resolved')", identity)
            prefs = {}
            for row in cur.fetchall():
                value = row["value"]
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                prefs[row["key"]] = value
            explicit = prefs.get("timezone")
            result["zone"] = explicit if explicit and explicit != "local" else prefs.get("timezone_resolved")
            if not isinstance(result["zone"], str):
                result["zone"] = None
            owner_session_id = session_id
            if session.get("is_worker"):
                # A supervisor assignment is not an owner message. Follow the
                # bounded lineage in one indexed query, including nested tasks.
                cur.execute("WITH RECURSIVE lineage AS ("
                            "SELECT id, parent_session_id, is_worker, 0 AS depth FROM sessions WHERE id = %s "
                            "UNION ALL SELECT s.id, s.parent_session_id, s.is_worker, l.depth + 1 "
                            "FROM sessions s JOIN lineage l ON s.id = l.parent_session_id "
                            "WHERE l.is_worker AND l.depth < 8) "
                            "SELECT id, is_worker FROM lineage ORDER BY depth DESC LIMIT 1", (session_id,))
                owner = cur.fetchone()
                owner_session_id = owner["id"] if owner and not owner["is_worker"] else None
                source = "wakeup"
            if owner_session_id:
                cur.execute("SELECT created_at, payload FROM session_events WHERE session_id = %s "
                            "AND event_type = 'message.user' "
                            "AND COALESCE(payload->>'source', 'user') IN ('user', 'queue') "
                            "AND COALESCE(payload->>'agent_wake', 'false') != 'true' "
                            "AND COALESCE(payload->>'not_sent', 'false') != 'true' "
                            "ORDER BY seq DESC LIMIT 2", (owner_session_id,))
                result["last_owner_at"] = previous_owner_time(cur.fetchall(), source)
    except Exception:  # a reminder must never fail a turn or log session text
        pass
    return result


def previous_owner_time(rows, source):
    skip = 1 if source in ("user", "queue") else 0
    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (payload.get("source", "user") not in ("user", "queue")
                or payload.get("agent_wake") or payload.get("not_sent")):
            continue
        if skip:
            skip -= 1
        else:
            return row.get("created_at")
    return None
