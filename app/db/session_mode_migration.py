"""#553: finish the deferred projectless-session upgrade without touching history."""
from typing import Any, Mapping

from app.db.base import row_to_dict, table_exists


def normalize_legacy_owner(cursor: Any, row: Mapping[str, Any]) -> bool:
    """Persist chat mode using the same owner fence for upgrades and peer arrivals.

    Read principals on the caller's transaction, including identities that have
    just been mirrored. Preserve history and stored permission choices: attaching
    a project remains the atomic boundary that resets access to Ask.
    """
    from app.core.session_orchestration import is_owner_session

    if row.get("mode") not in (None, "code") or any(
        row.get(key) for key in (
            "project_id", "project_v2_id", "api_user_id", "automation_id",
            "parent_session_id", "is_worker", "hidden",
        )
    ):
        return False
    principal = None
    if table_exists(cursor, "mobile_tool_identity"):
        cursor.execute(
            "SELECT * FROM mobile_tool_identity WHERE session_id=%s", (row["id"],),
        )
        principal = row_to_dict(cursor.fetchone())
    if not is_owner_session(row, principal=principal):
        return False
    cursor.execute(
        "UPDATE sessions SET mode='chat' WHERE id=%s AND (mode IS NULL OR mode='code') "
        "AND project_id IS NULL AND project_v2_id IS NULL "
        "AND api_user_id IS NULL AND automation_id IS NULL "
        "AND parent_session_id IS NULL AND NOT COALESCE(is_worker,FALSE) "
        "AND NOT COALESCE(hidden,FALSE)",
        (row["id"],),
    )
    return cursor.rowcount > 0


def migrate(cursor: Any) -> list[str]:
    """Expose the no-project ceiling/attach affordance without changing other identities."""
    cursor.execute(
        "SELECT * FROM sessions WHERE (mode IS NULL OR mode='code') "
        "AND project_id IS NULL AND project_v2_id IS NULL "
        "AND api_user_id IS NULL AND automation_id IS NULL "
        "AND parent_session_id IS NULL AND NOT COALESCE(is_worker,FALSE) "
        "AND NOT COALESCE(hidden,FALSE)"
    )
    candidates = [row_to_dict(row) for row in cursor.fetchall()]
    count = sum(normalize_legacy_owner(cursor, row) for row in candidates)
    return [f"sessions.projectless_chat({count})"] if count else []
