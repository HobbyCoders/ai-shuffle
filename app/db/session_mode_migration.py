"""#553: finish the deferred projectless-session upgrade without touching history."""
from typing import Any

from app.db.base import row_to_dict, table_exists


def migrate(cursor: Any) -> list[str]:
    """Expose the no-project ceiling/attach affordance without changing other identities.

    Permission choices and history remain; the runtime clamps Accept Edits to Ask.
    Read principals on this transaction, not another connection that cannot see
    the schema/provenance upgrade yet. Old-peer arrivals also get a runtime fence.
    """
    from app.core.session_orchestration import is_owner_session

    cursor.execute(
        "SELECT * FROM sessions WHERE (mode IS NULL OR mode='code') "
        "AND project_id IS NULL AND project_v2_id IS NULL "
        "AND api_user_id IS NULL AND automation_id IS NULL "
        "AND parent_session_id IS NULL AND NOT COALESCE(is_worker,FALSE) "
        "AND NOT COALESCE(hidden,FALSE)"
    )
    candidates = [row_to_dict(row) for row in cursor.fetchall()]
    has_principals = table_exists(cursor, "mobile_tool_identity")
    memo: dict[str, Any] = {}
    count = 0
    for row in candidates:
        principal = None
        if has_principals:
            cursor.execute(
                "SELECT * FROM mobile_tool_identity WHERE session_id=%s", (row["id"],),
            )
            principal = row_to_dict(cursor.fetchone())
        if is_owner_session(row, principal=principal, memo=memo):
            cursor.execute(
                "UPDATE sessions SET mode='chat' WHERE id=%s AND (mode IS NULL OR mode='code')",
                (row["id"],),
            )
            count += cursor.rowcount
    return [f"sessions.projectless_chat({count})"] if count else []
