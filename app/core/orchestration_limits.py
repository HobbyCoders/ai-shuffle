"""Owner coordination admission and spending enforcement.

Accounting and the limits API use assistant_limits' storage, day boundaries
and remote cumulative-cost protocol.
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _retired_homes():
    """Ids of the retired assistant homes the #371 migration deleted.

    Chats a home started with StartSession survive it, but their starter row
    is gone; they stay coordination jobs so the limits still cover them.
    """
    from app.db.database import RETIRED_ASSISTANT_HOMES_MARKER
    from app.db.settings import get_system_setting
    try:
        raw = get_system_setting(RETIRED_ASSISTANT_HOMES_MARKER)
        return frozenset(json.loads(raw)) if raw else frozenset()
    except Exception:  # noqa: BLE001 — an unreadable marker leaves the ordinary rule
        log.warning("Coordination roles: could not read the retired assistant homes", exc_info=True)
        return frozenset()


def tracked_roles(now=None):
    """Coordinators and jobs among today's booked or currently running rows.

    Only those rows can affect today's limits. Their rows, start markers,
    principals and ancestry are read in bounded batches (one pass per
    ancestry level), never one query per session.
    """
    from app.core import assistant_limits as accounting
    from app.db.base import get_db
    start = accounting.day_window(now)[1]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM usage_log WHERE created_at >= %s "
                       "UNION SELECT session_id FROM turns WHERE state IN ('running','interrupting','draining')",
                       (start,))
        ids = [row["session_id"] for row in cursor.fetchall() if row["session_id"]]
    if not ids:
        return {}
    view = _Batch(ids)
    memo = {}
    return {sid: kind for sid in ids if (kind := _role(sid, memo, view=view)) in ("coordinator", "job")}


class _Reads:
    """One session's role inputs, read on demand (``role``)."""

    def row(self, session_id):
        from app.db import sessions
        return sessions.get_session(session_id)

    def owner(self, row):
        from app.core.session_orchestration import is_owner_session
        return is_owner_session(row)

    def start_payload(self, session_id):
        from app.core.session_orchestration import _start_turn
        return (_start_turn(session_id) or {}).get("payload") or {}

    def retired_home(self, session_id):
        if not hasattr(self, "_retired"):
            self._retired = _retired_homes()
        return session_id in self._retired


class _Batch(_Reads):
    """The same inputs for many sessions, preloaded with bounded queries."""

    def __init__(self, ids):
        from app.core import assistant_limits as accounting
        self.rows, self.starts, self.principals, self.memo = {}, {}, {}, {}
        self.principals_ok = True
        self._retired = _retired_homes()
        wanted, asked = set(ids), set()
        # One pass per ancestry level (workers' parents, StartSession senders).
        for _ in range(_MAX_DEPTH + 1):
            wanted -= asked
            if not wanted:
                break
            asked |= wanted
            for chunk in accounting._chunks(sorted(wanted)):
                self._load(chunk)
            wanted = {ancestor for sid in wanted if (ancestor := self._ancestor(sid))}

    def _load(self, chunk):
        from app.db.base import get_db, row_to_dict
        from app.db.turns import _parse_json
        marks = ", ".join(["%s"] * len(chunk))
        with get_db() as conn:
            cursor = conn.cursor()
            # Each row joined with its StartSession marker (earliest wins, as in _start_turn).
            cursor.execute(
                "SELECT s.*, t.payload AS start_payload_, t.created_at AS start_at_ "
                "FROM sessions s LEFT JOIN turns t ON t.session_id=s.id "
                f"AND t.command_id LIKE 'orchestration:start:%%' WHERE s.id IN ({marks})", tuple(chunk))
            for raw in cursor.fetchall():
                row = row_to_dict(raw)
                payload, at = row.pop("start_payload_"), row.pop("start_at_")
                self.rows[row["id"]] = row
                earlier = self.starts.get(row["id"])
                if payload is not None and (earlier is None or (at is not None and at < earlier[0])):
                    self.starts[row["id"]] = (at, _parse_json(payload) or {})
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(f"SELECT * FROM mobile_tool_identity WHERE session_id IN ({marks})", tuple(chunk))
                for raw in cursor.fetchall():
                    record = row_to_dict(raw)
                    self.principals[record["session_id"]] = record
        except Exception:  # noqa: BLE001 — unreadable identities grant no role
            log.warning("Coordination roles: could not read session principals", exc_info=True)
            self.principals_ok = False

    def _ancestor(self, session_id):
        row = self.rows.get(session_id) or {}
        if row.get("is_worker"):
            return row.get("parent_session_id")
        payload = self.start_payload(session_id)
        return None if payload.get("orchestration_remote_origin") else payload.get("orchestration_sender")

    def row(self, session_id):
        return self.rows.get(session_id)

    def owner(self, row):
        if not self.principals_ok:
            return False
        from app.core.session_orchestration import is_owner_session
        return is_owner_session(row, principal=self.principals.get(row["id"]), memo=self.memo)

    def start_payload(self, session_id):
        start = self.starts.get(session_id)
        return start[1] if start and isinstance(start[1], dict) else {}


_MAX_DEPTH = 12


def _role(session_id, memo, depth=0, *, view=None):
    from app.core.session_modes import session_mode_of
    from app.core.config import settings
    from app.network.runners import self_device_id
    view = view or _Reads()
    if not session_id or session_id in memo or depth >= _MAX_DEPTH:
        return memo.get(session_id)
    memo[session_id] = None  # Cycles never acquire a role.
    row = view.row(session_id)
    if not row or row.get("api_user_id") or row.get("automation_id"):
        return None
    if (row.get("owner_device_id") != self_device_id() if row.get("owner_device_id")
            else row.get("instance_id") != settings.effective_instance_id):
        return None
    if row.get("is_worker"):
        kind = "job" if _role(row.get("parent_session_id"), memo, depth + 1, view=view) else None
    elif view.owner(row):
        payload = view.start_payload(session_id)
        sender = payload.get("orchestration_sender")
        if sender and not payload.get("orchestration_remote_origin"):
            if view.retired_home(sender) and view.row(sender) is None:
                # Started by a retired assistant home the migration deleted (#371).
                kind = "job"
            else:
                # A project owner's independent project work is not a coordination job.
                kind = "job" if _role(sender, memo, depth + 1, view=view) else None
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
        if kind is None or accounting._person_input(source, payload):
            return None
        state = accounting.status()
        if state["limit_reached"]:
            return accounting._reached_text(state)
        if (kind == "job" and state["job_limit_enabled"]
                and accounting._job_over_time(session_id, state["max_job_minutes"], datetime.now(timezone.utc),
                                              coordination=True)):
            return (f"This background job passed its {accounting._duration(state['max_job_minutes'])} limit, so it does not "
                    f"start again; {accounting.WHERE_TO_CHANGE}.")
    except Exception:  # noqa: BLE001 — match existing accounting failure behaviour
        log.warning("Coordination turn admission failed for %s", session_id, exc_info=True)
    return None


async def enforce(now=None):
    """Stop work past the limits. Never raises."""
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
