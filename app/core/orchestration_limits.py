"""Owner coordination admission and spending enforcement (legacy setting retained).

Accounting and the Assistant settings API still use assistant_limits' existing
storage, day boundaries and remote cumulative-cost protocol. Admission no longer
depends on the existence of the hidden assistant conversation.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def tracked_roles():
    from app.core.session_orchestration import is_owner_session
    from app.core import assistant_limits as accounting
    from app.db import turns
    from app.core.config import settings
    from app.network.runners import self_device_id
    from app.db.base import row_to_dict, get_db
    aid = accounting.assistant_id()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions")
        rows = [row_to_dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM turns WHERE command_id LIKE 'orchestration:start:%%' ORDER BY created_at ASC")
        starts = {}
        for raw in cursor.fetchall():
            turn = turns._row(raw)
            starts.setdefault(turn["session_id"], turn.get("payload") or {})
        cursor.execute("SELECT session_id FROM assistant_runs WHERE assistant_id=%s AND session_id IS NOT NULL", (aid,))
        linked = [row["session_id"] for row in cursor.fetchall()]
    # Principal/account checks can read storage or contact the account service.
    # Never hold a pool connection while doing them: concurrent fanout otherwise
    # exhausts the pool with nested checkouts.
    device_id = self_device_id()
    owners = {row["id"] for row in rows if (
        row.get("owner_device_id") == device_id if row.get("owner_device_id")
        else row.get("instance_id") == settings.effective_instance_id) and is_owner_session(row)}
    roles = {row["id"]: "coordinator" for row in rows if row["id"] in owners
             and not row.get("hidden") and not (row.get("project_id") or row.get("project_v2_id"))}
    for row in rows:
        if row["id"] not in owners:
            continue
        payload = starts.get(row["id"], {})
        if payload.get("orchestration_sender") in owners and not payload.get("orchestration_remote_origin"):
            roles[row["id"]] = "job"
    changed = True
    while changed:
        changed = False
        for row in rows:
            if (row.get("is_worker") and not row.get("api_user_id") and row["id"] not in roles
                    and row.get("parent_session_id") in roles):
                roles[row["id"]] = "job"
                changed = True
    # Preserve the existing per-job timer for legacy linked jobs, even when
    # their old rows have no project and were never stamped as workers.
    for session_id in linked:
        if session_id in roles:
            roles[session_id] = "job"
    return roles


def role(session_id):
    from app.core import assistant_limits as accounting
    legacy = accounting.role(session_id)
    if legacy:
        return legacy
    return tracked_roles().get(session_id)


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


def turn_refusal(session_id, source=None):
    from app.core import assistant_limits as accounting
    try:
        kind = role(session_id)
        if kind is None or (kind == "job" and source in accounting.PERSON_SOURCES):
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
