"""Owner coordination admission and spending enforcement (legacy setting retained).

Accounting and the Assistant settings API still use assistant_limits' existing
storage, day boundaries and remote cumulative-cost protocol. Admission no longer
depends on the existence of the hidden assistant conversation.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def tracked_roles(now=None):
    """Only today's booked or currently running rows can affect today's limits."""
    from app.core import assistant_limits as accounting
    from app.db.base import get_db
    start = accounting.day_window(now)[1]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM usage_log WHERE created_at >= %s "
                       "UNION SELECT session_id FROM turns WHERE state IN ('running','interrupting','draining')",
                       (start,))
        ids = [row["session_id"] for row in cursor.fetchall() if row["session_id"]]
    # Principal checks may read storage; release the connection before ancestry.
    memo = {}
    return {sid: kind for sid in ids if accounting.role(sid) is None
            and (kind := _role(sid, memo)) in ("coordinator", "job")}


def _role(session_id, memo, depth=0):
    from app.core import assistant_limits as accounting
    from app.core.session_orchestration import is_owner_session, _start_turn
    from app.core.session_modes import session_mode_of
    from app.core.config import settings
    from app.network.runners import self_device_id
    from app.db import sessions
    if not session_id or session_id in memo or depth >= 12:
        return memo.get(session_id)
    memo[session_id] = None  # Cycles never acquire a role.
    legacy = accounting.role(session_id)
    if legacy:
        memo[session_id] = legacy
        return legacy
    row = sessions.get_session(session_id)
    if not row or row.get("api_user_id") or row.get("automation_id"):
        return None
    if (row.get("owner_device_id") != self_device_id() if row.get("owner_device_id")
            else row.get("instance_id") != settings.effective_instance_id):
        return None
    if row.get("is_worker"):
        kind = "job" if _role(row.get("parent_session_id"), memo, depth + 1) else None
    elif is_owner_session(row):
        payload = (_start_turn(session_id) or {}).get("payload") or {}
        sender = payload.get("orchestration_sender")
        if sender and not payload.get("orchestration_remote_origin"):
            # A project owner's independent project work is not a coordination job.
            kind = "job" if _role(sender, memo, depth + 1) else None
        else:
            kind = ("coordinator" if session_mode_of(row) == "chat" and not row.get("hidden")
                    and not (row.get("project_id") or row.get("project_v2_id")) else None)
    else:
        kind = None
    memo[session_id] = kind
    return kind


def role(session_id):
    """Resolve just this row and its durable starter/worker ancestry, never a scan."""
    return _role(session_id, {})


def start_refusal(session_id):
    from app.core import assistant_limits as accounting
    try:
        if role(session_id) is None:
            return None
        state = accounting.status()
        return accounting._reached_text(state) if state["limit_reached"] else None
    except Exception:  # noqa: BLE001 — legacy unrelated sessions must remain usable
        log.warning("Coordination spending admission failed for %s", session_id, exc_info=True)
        return None


def assert_can_start(session_id):
    reason = start_refusal(session_id)
    if reason:
        raise ValueError(reason)


def turn_refusal(session_id, source=None, *, payload=None):
    from app.core import assistant_limits as accounting
    try:
        kind = role(session_id)
        if kind is None or (kind != "home" and accounting._person_input(source, payload)):
            return None
        state = accounting.status()
        if state["limit_reached"]:
            return accounting._reached_text(state)
        if (kind == "job" and state["job_limit_enabled"]
                and accounting._job_over_time(session_id, state["max_job_minutes"], datetime.now(timezone.utc))):
            return (f"This background job passed its {accounting._duration(state['max_job_minutes'])} limit, so it does not "
                    "start again; only the owner can change the limit on the Assistant page under Limits.")
    except Exception:  # noqa: BLE001 — match existing accounting failure behaviour
        log.warning("Coordination turn admission failed for %s", session_id, exc_info=True)
    return None


async def enforce(now=None):
    """Stop work past the limits and post the owner's notices. Never raises."""
    from app.core import turn_manager, assistant_limits as accounting
    from app.db.async_database import run_sync
    try:
        observed = await accounting.refresh_remote(now)
    except Exception:
        log.exception("Assistant limits: could not collect the spend of jobs on other workers")
        observed = []
    try:
        state, stops = await run_sync(accounting.plan, now)
        remote = await run_sync(accounting.plan_remote, state, observed)
    except Exception:
        log.exception("Assistant limits: could not measure today's spend")
        return
    aid = accounting.assistant_id()
    for session_id, turn_id, reason, label in stops:
        try:
            row = await turn_manager.interrupt_async(session_id, "stop", turn_id=turn_id, source="assistant")
            if row is not None:
                await run_sync(accounting.limits_db.record_stop, aid, session_id, turn_id, reason, label)
                log.info("Assistant limits: stopped turn %s in %s (%s)", turn_id, session_id, reason)
        except Exception:
            log.exception("Assistant limits: could not stop turn %s", turn_id)
    for stop in remote:
        try:
            if await accounting._stop_remote(stop):
                await run_sync(accounting.limits_db.record_stop, aid, stop["session_id"], stop["turn_ids"][0],
                               stop["reason"], stop["label"])
                log.info("Assistant limits: stopped %s %s on %s (%s)", stop["kind"], stop["session_id"],
                            stop["device_id"], stop["reason"])
        except Exception:
            log.exception("Assistant limits: could not stop %s on %s", stop["session_id"], stop["device_id"])
    try:
        await accounting._post_notices(state)
    except Exception:
        log.exception("Assistant limits: notices failed")
