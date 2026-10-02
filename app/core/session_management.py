"""Owner-scoped sidebar management; never starts or stops execution."""
from app.core import session_orchestration as orchestration
from app.core.config import settings
from app.db import sessions


def visible_session(row):
    from app.network import engine, membership
    from app.network.runners import self_device_id
    if not row:
        return False
    owner = row.get("owner_device_id")
    local = (row.get("instance_id") == settings.effective_instance_id
             and owner in (None, self_device_id()))
    network = engine.context()
    member = network.member(owner) if network and owner else None
    remote = bool(member and member.status == membership.STATUS_ACTIVE)
    return bool((local or remote) and not row.get("hidden") and not row.get("is_worker")
                and not row.get("parent_session_id") and row.get("mode") != "voice")


def authorize(caller_id, data):
    from app.core import mobile_tool_identity
    caller = orchestration._caller(caller_id, allow_settled=True)
    target_id = orchestration._text(data, "session_id", 200)
    row = sessions.get_session(target_id)
    # Let the shared transaction enforce the persistent assistant-home guard.
    from app.core.assistant_service import is_primary_session
    visible = visible_session(row) or (row and target_id == caller_id and is_primary_session(target_id))
    if not visible or orchestration._owner(row) != orchestration._owner(caller):
        raise ValueError("Session is not an accessible top-level chat; use ListSessions.")
    mobile_tool_identity.validate(orchestration._principal(target_id))
    return row


async def manage(caller_id, data, action):
    from app.db.async_database import run_sync
    from app.network.session_active import update_membership
    row = await run_sync(authorize, caller_id, data)
    if action != "archive" and row.get("status") == "archived":
        raise ValueError("This session is archived. Restore it in the app before settling or unsettling it.")
    updated = await update_membership(row, action == "unsettle", True if action == "archive" else None)
    return {"session_id": updated["id"], "is_active": bool(updated.get("is_active")),
            "status": updated.get("status"), "action": action,
            "note": "Sidebar state updated. Execution was not started or stopped; no conversation was deleted."}
