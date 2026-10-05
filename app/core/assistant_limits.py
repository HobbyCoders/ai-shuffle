"""The owner's spending and run-time limits for the persistent assistant.

The app enforces these, never the model. Only the owner-only assistant API
changes them (``app.api.assistant``); no model-facing tool can raise, switch
off or reset them.

What counts
-----------
The home conversation plus every session linked to it in ``assistant_runs``:
research runs, Task workers and chats it started or messaged (nested starts
link to the same home). A linked session counts from the moment it was
linked, so an older chat the assistant only messaged counts from then on.

Task workers and chats it started on another worker count too
(``assistant_remote_jobs``). That worker books their turns in its own
``usage_log`` and never links them to its own assistant, so the spend is
charged to this limit only. Every pass of :func:`enforce` asks each such
worker (control action ``remote-usage``) for the job's cumulative cost: the
job's own turns, the Task workers it started there and its running turns'
per-call costs. The rise over the highest total seen before is booked once,
here, when it is learned (``assistant_remote_costs``), so an answer seen
twice, a restart or a worker that comes back after being unreachable never
counts a dollar twice and never loses one. Cost a worker could not report yet
counts on the day it arrives. A worker on a release without the action is
read from its mirrored ``sessions.total_cost_usd`` instead.

The cost source is the one automations and Stats use: the per-turn
``usage_log`` booking of the turn's reported ``total_cost_usd``. A turn counts
on the day it is booked. For subscription legs that figure is the app's
API-price estimate (fluid prices the call from its model table; the Claude
CLI reports its own estimate), so the limit holds for them too. A model the
price table does not know books $0, which the run-time limit still covers.
Turns still running add their per-call ``turn.usage`` costs from the event
log, so one long turn cannot run past the limit before it settles.

The day starts at midnight in the owner's timezone preference when one is
recorded (Settings > General > Time zone), else UTC.

Enforcement
-----------
* At the limit, new background work is refused (Task, StartSession,
  MessageSession, research), the home starts no new turn and unattended
  turns in linked jobs do not start.
* ``enforce`` runs on the assistant manager's loop: at the limit it stops the
  live turns of the home and its jobs with the ordinary Stop path, and it
  stops a job that has worked longer than the job time limit. Turns a person
  started inside a job are left alone.
* A job on another worker is stopped the way that kind of job is stopped
  from here. A Task child goes through the one remote stop path, as Stop on
  its card or Swarm cancel does: the supervisor's teardown when it has a
  record here, else ``remote_task.stop_remote_child``. That stop is durable
  (sent again when the worker answers), withdraws the child's approval
  prompts here and makes a later SendMessage refuse to resume it. A chat gets
  the ``interrupt`` control action on each running turn, as Stop on a
  mirrored chat does. Turns a person started there are left alone too.
* SendMessage to a Task child on another worker is refused at the limit
  (``message_refusal``), as the turn it would start is for a local child.
* One plain notice is posted into the home conversation between turns: at
  80% (once per day), when the limit is reached, and when a job is stopped.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core import assistant_service as service
from app.db import assistant as store, assistant_limits as limits_db
from app.db.async_database import run_sync
from app.db.base import get_db, row_to_dict

logger = logging.getLogger(__name__)

NOTICE_SOURCE = "assistant_limit_notice"
WHERE_TO_CHANGE = "you can change it on the Assistant page under Limits"
WARN_FRACTION = 0.8
# Turn sources a person started. Inside a job these are never stopped or
# refused by the limits: the owner chose to run them.
PERSON_SOURCES = frozenset({"user", "queue", "worktree", "resume", "api", "answer"})
LIVE_STATES = ("running", "interrupting", "draining")
# A linked session created this close to (or after) its link was started by
# the assistant, so its whole working time is the job's run time.
CREATED_BY_ASSISTANT = timedelta(minutes=5)
_CHUNK = 400

# Jobs on another worker (``assistant_remote_jobs``).
REMOTE_USAGE_ACTION = "remote-usage"
REMOTE_WIRE_VERSION = 1
REMOTE_TIMEOUT = 15.0
MAX_REMOTE_IDS = 200
MAX_REMOTE_TURNS = 20
#: A cumulative cost above this is not believed.
MAX_REPORTED_USD = 1_000_000.0
#: A job its worker reported idle, with nothing new spent, for this long is
#: not asked about any more.
REMOTE_IDLE_FOR = timedelta(hours=24)
TASK_JOB, CHAT_JOB = "task", "chat"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def assistant_id():
    return service.assistant_id()


def _utc(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _money(value) -> str:
    return f"${value:,.2f}"


def _duration(minutes) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}-hour" if hours else f"{minutes}-minute"
    return f"{minutes}-minute"


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------

def owner_timezone():
    """``(tzinfo, name)`` from the owner's timezone preference, else UTC.

    "local" (follow the browser) is not a zone the server can know, so it
    reads as UTC like no preference at all.
    """
    from zoneinfo import ZoneInfo
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM user_preferences WHERE key='timezone' AND user_type<>'api_user' "
                           "ORDER BY updated_at DESC LIMIT 1")
            row = row_to_dict(cursor.fetchone())
        value = (row or {}).get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if isinstance(value, str) and value and value != "local":
            return ZoneInfo(value), value
    except Exception:  # noqa: BLE001 - an unreadable or unknown zone is UTC
        logger.debug("Assistant limits: owner timezone unreadable", exc_info=True)
    return timezone.utc, "UTC"


def day_window(now=None):
    """``(day, start, resets_at, timezone name)``; start and resets_at in UTC."""
    tz, name = owner_timezone()
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Next local midnight; normalise through UTC so DST days are right.
    following = (start + timedelta(days=1, hours=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.date().isoformat(), start.astimezone(timezone.utc), following.astimezone(timezone.utc), name


# ---------------------------------------------------------------------------
# What the assistant spent today
# ---------------------------------------------------------------------------

def _home_id(cursor):
    cursor.execute("SELECT home_session_id FROM assistant_state WHERE id=%s", (assistant_id(),))
    return (row_to_dict(cursor.fetchone()) or {}).get("home_session_id")


def _tracked(cursor):
    """``{session_id: counted-from}`` for the home (None: all day) and its jobs."""
    home = _home_id(cursor)
    if not home:
        return {}
    tracked: dict = {home: None}
    cursor.execute("SELECT session_id, created_at FROM assistant_runs WHERE assistant_id=%s "
                   "AND session_id IS NOT NULL", (assistant_id(),))
    for row in cursor.fetchall():
        sid = row["session_id"]
        if sid == home:
            continue
        since = _utc(row["created_at"])
        if sid not in tracked or (since and (tracked[sid] is None or since < tracked[sid])):
            tracked[sid] = since
    return tracked


def _chunks(ids):
    ids = list(ids)
    for index in range(0, len(ids), _CHUNK):
        yield ids[index:index + _CHUNK]


def _booked(cursor, tracked, start):
    total = 0.0
    for chunk in _chunks(tracked):
        marks = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT session_id, cost_usd, created_at FROM usage_log WHERE session_id IN ({marks}) "
                       "AND created_at >= %s", (*chunk, start))
        for row in cursor.fetchall():
            since = tracked.get(row["session_id"])
            booked_at = _utc(row["created_at"])
            if since is not None and booked_at is not None and booked_at < since:
                continue
            total += max(0.0, float(row["cost_usd"] or 0))
    return total


def _live_turns(cursor, session_ids):
    turns = []
    states = ", ".join(["%s"] * len(LIVE_STATES))
    for chunk in _chunks(session_ids):
        marks = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT * FROM turns WHERE session_id IN ({marks}) AND state IN ({states})",
                       (*chunk, *LIVE_STATES))
        turns.extend(row_to_dict(row) for row in cursor.fetchall())
    return turns


def _in_flight(cursor, tracked, live):
    """Per-call costs of turns not booked yet (their current attempt only).

    Records without a ``model_call_index`` are coalesced gauges and would
    count a call several times; they wait for the turn's booking instead.
    """
    total = 0.0
    for turn in live:
        since = max(filter(None, (_utc(turn.get("started_at")), tracked.get(turn["session_id"]))), default=None)
        cursor.execute("SELECT payload, created_at FROM session_events WHERE session_id=%s AND turn_id=%s "
                       "AND event_type='turn.usage'", (turn["session_id"], turn["id"]))
        seen = set()
        for row in cursor.fetchall():
            at = _utc(row["created_at"])
            if since is not None and at is not None and at < since:
                continue
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            usage = (payload or {}).get("usage")
            if not isinstance(usage, dict) or usage.get("model_call_index") is None:
                continue
            if usage["model_call_index"] in seen:
                continue
            seen.add(usage["model_call_index"])
            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total += max(0.0, float(cost))
    return total


def _remote_booked(cursor, start):
    """What jobs on other workers spent, as booked here since ``start``."""
    cursor.execute("SELECT cost_usd FROM assistant_remote_costs WHERE assistant_id=%s AND booked_at >= %s",
                   (assistant_id(), start))
    return sum(max(0.0, float(row["cost_usd"] or 0)) for row in cursor.fetchall())


def _measure(now=None) -> dict:
    """Spend today plus what enforcement needs, from one connection."""
    day, start, resets_at, tz_name = day_window(now)
    with get_db() as conn:
        cursor = conn.cursor()
        tracked = _tracked(cursor)
        live = _live_turns(cursor, tracked) if tracked else []
        spent = (_booked(cursor, tracked, start) + _in_flight(cursor, tracked, live)
                 + _remote_booked(cursor, start)) if tracked else 0.0
    return dict(day=day, start=start, resets_at=resets_at, timezone=tz_name,
                spent=spent, tracked=tracked, live=live)


def status(now=None):
    """Today's spend against the owner's limits (the API and GetUsage view)."""
    limits = limits_db.get(assistant_id())
    measured = _measure(now)
    spent = measured["spent"]
    cap = limits["daily_spend_limit_usd"]
    on = limits["spend_limit_enabled"]
    return dict(
        spend_limit_enabled=on, daily_spend_limit_usd=cap,
        job_limit_enabled=limits["job_limit_enabled"], max_job_minutes=limits["max_job_minutes"],
        spent_today_usd=round(spent, 4), day=measured["day"], timezone=measured["timezone"],
        resets_at=measured["resets_at"].isoformat(),
        limit_reached=bool(on and spent >= cap),
        warning=bool(on and spent >= cap * WARN_FRACTION),
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def role(session_id):
    """'home', 'job' or None for a session on this worker's assistant."""
    if not session_id:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        home = _home_id(cursor)
        if not home:
            return None
        if session_id == home:
            return "home"
        cursor.execute("SELECT 1 FROM assistant_runs WHERE assistant_id=%s AND session_id=%s LIMIT 1",
                       (assistant_id(), session_id))
        return "job" if cursor.fetchone() else None


def _reached_text(state):
    return (f"Today's {_money(state['daily_spend_limit_usd'])} assistant spending limit is reached "
            f"({_money(state['spent_today_usd'])} spent), so no new work starts until tomorrow; "
            f"only the owner can change it on the Assistant page under Limits.")


def start_refusal(session_id):
    """Why ``session_id`` may not start or wake background work now, or None.

    Covers Task workers, StartSession, MessageSession and research admission
    for the home and every job linked to it.
    """
    try:
        if role(session_id) is None:
            return None
        state = status()
    except Exception:  # noqa: BLE001 - never break unrelated orchestration
        logger.warning("Assistant limits: start check failed for %s", session_id, exc_info=True)
        return None
    return _reached_text(state) if state["limit_reached"] else None


def assert_can_start(session_id):
    reason = start_refusal(session_id)
    if reason:
        raise ValueError(reason)


def _job_minutes(session, now):
    worked = int(session.get("work_ms") or 0)
    started = _utc(session.get("work_started_at"))
    if started is not None:
        worked += max(0, int((now - started).total_seconds() * 1000))
    return worked / 60000.0


def _created_by_assistant(cursor, session):
    cursor.execute("SELECT MIN(created_at) AS linked FROM assistant_runs WHERE assistant_id=%s AND session_id=%s",
                   (assistant_id(), session["id"]))
    linked = _utc((row_to_dict(cursor.fetchone()) or {}).get("linked"))
    created = _utc(session.get("created_at"))
    return linked is not None and created is not None and created >= linked - CREATED_BY_ASSISTANT


def _job_over_time(session_id, minutes, now):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
        session = row_to_dict(cursor.fetchone())
        if not session or not _created_by_assistant(cursor, session):
            return False
        return _job_minutes(session, now) >= minutes


def turn_refusal(session_id, source=None):
    """Why a new model turn in ``session_id`` may not start, or None.

    The home starts no turn at the limit, whoever asked. A job starts no
    unattended turn at the limit or past its job time limit; a turn the owner
    started in it runs.
    """
    try:
        kind = role(session_id)
        if kind is None or (kind == "job" and source in PERSON_SOURCES):
            return None
        state = status()
        if state["limit_reached"]:
            return _reached_text(state)
        if (kind == "job" and state["job_limit_enabled"]
                and _job_over_time(session_id, state["max_job_minutes"], datetime.now(timezone.utc))):
            return (f"This background job passed its {_duration(state['max_job_minutes'])} limit, so it does not "
                    f"start again; only the owner can change the limit on the Assistant page under Limits.")
    except Exception:  # noqa: BLE001 - a failed check must not block unrelated turns
        logger.warning("Assistant limits: turn check failed for %s", session_id, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# What the model sees
# ---------------------------------------------------------------------------

ROLE_CONTEXT = (
    "Spending limits: the owner sets a daily spending limit for you and all the work you start, and a run-time "
    "limit for each background job. The app enforces them and stops work that reaches them; you cannot change, "
    "raise or reset them. Only the owner can, on the Assistant page under Limits. To answer how much you have "
    "spent today, call GetUsage and read its session_usage field.")


def role_context(session_id):
    """Static text for the home's system prompt (no per-turn values)."""
    try:
        return ROLE_CONTEXT if service.is_primary_session(session_id) else ""
    except Exception:  # noqa: BLE001
        logger.debug("Assistant limits: role context unavailable", exc_info=True)
        return ""


def session_usage(session_id):
    """GetUsage's ``session_usage`` block for the home and its jobs, else None."""
    if role(session_id) is None:
        return None
    state = status()
    return {
        "available": True,
        "scope": "assistant_today",
        "spent_today_usd": state["spent_today_usd"],
        "daily_limit_usd": state["daily_spend_limit_usd"] if state["spend_limit_enabled"] else None,
        "daily_limit_on": state["spend_limit_enabled"],
        "limit_reached": state["limit_reached"],
        "max_job_minutes": state["max_job_minutes"] if state["job_limit_enabled"] else None,
        "day": state["day"], "timezone": state["timezone"], "resets_at": state["resets_at"],
        "note": ("Today's spend for the assistant conversation and every job it started, on this worker "
                 "or another one, in US dollars at API prices (subscription use is an estimate). Another "
                 "worker's spend counts once that worker reports it, about once a minute. Only the owner "
                 "can change these limits."),
    }


# ---------------------------------------------------------------------------
# Enforcement (the assistant manager's loop)
# ---------------------------------------------------------------------------

def _label(cursor, session_id):
    cursor.execute("SELECT title FROM sessions WHERE id=%s", (session_id,))
    return (row_to_dict(cursor.fetchone()) or {}).get("title") or None


def plan(now=None):
    """``(status, stops)``: the turns the limits stop on this pass.

    Each stop is ``(session_id, turn_id, reason, label)`` with reason
    ``daily_limit`` or ``job_time``. Turns already stopped are left out.
    """
    now = now or datetime.now(timezone.utc)
    limits = limits_db.get(assistant_id())
    measured = _measure(now)
    state = dict(
        spend_limit_enabled=limits["spend_limit_enabled"], daily_spend_limit_usd=limits["daily_spend_limit_usd"],
        job_limit_enabled=limits["job_limit_enabled"], max_job_minutes=limits["max_job_minutes"],
        spent_today_usd=round(measured["spent"], 4), day=measured["day"],
        limit_reached=bool(limits["spend_limit_enabled"] and measured["spent"] >= limits["daily_spend_limit_usd"]),
    )
    tracked = measured["tracked"]
    home = next((sid for sid, since in tracked.items() if since is None), None)
    stops = []
    with get_db() as conn:
        cursor = conn.cursor()
        for turn in measured["live"]:
            sid = turn["session_id"]
            if limits_db.stopped(turn["id"]):
                continue
            person = sid != home and turn.get("source") in PERSON_SOURCES
            if state["limit_reached"] and not person:
                stops.append((sid, turn["id"], "daily_limit", _label(cursor, sid) if sid != home else None))
                continue
            if sid == home or person or not limits["job_limit_enabled"]:
                continue
            cursor.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
            session = row_to_dict(cursor.fetchone())
            if (session and _created_by_assistant(cursor, session)
                    and _job_minutes(session, now) >= limits["max_job_minutes"]):
                stops.append((sid, turn["id"], "job_time", session.get("title") or None))
    return state, stops


async def enforce(now=None):
    """Stop work past the limits and post the owner's notices. Never raises."""
    from app.core import turn_manager
    try:
        observed = await refresh_remote(now)
    except Exception:
        logger.exception("Assistant limits: could not collect the spend of jobs on other workers")
        observed = []
    try:
        state, stops = await run_sync(plan, now)
        remote = await run_sync(plan_remote, state, observed)
    except Exception:
        logger.exception("Assistant limits: could not measure today's spend")
        return
    aid = assistant_id()
    for session_id, turn_id, reason, label in stops:
        try:
            row = await turn_manager.interrupt_async(session_id, "stop", turn_id=turn_id, source="assistant")
            if row is not None:
                await run_sync(limits_db.record_stop, aid, session_id, turn_id, reason, label)
                logger.info("Assistant limits: stopped turn %s in %s (%s)", turn_id, session_id, reason)
        except Exception:
            logger.exception("Assistant limits: could not stop turn %s", turn_id)
    for stop in remote:
        try:
            if await _stop_remote(stop):
                await run_sync(limits_db.record_stop, aid, stop["session_id"], stop["turn_ids"][0],
                               stop["reason"], stop["label"])
                logger.info("Assistant limits: stopped %s %s on %s (%s)", stop["kind"], stop["session_id"],
                            stop["device_id"], stop["reason"])
        except Exception:
            logger.exception("Assistant limits: could not stop %s on %s", stop["session_id"], stop["device_id"])
    try:
        await _post_notices(state)
    except Exception:
        logger.exception("Assistant limits: notices failed")


# ---------------------------------------------------------------------------
# Jobs on another worker
# ---------------------------------------------------------------------------

def _payload(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _started_by(payload, device_id):
    """Whether a session's first turn records ``device_id`` as the worker that started it."""
    return bool(payload) and device_id in (payload.get("task_report_device"),
                                           payload.get("orchestration_remote_origin"))


def _job_usage(cursor, session_id, from_device, now):
    """What one job ``from_device`` started here has spent, and its live turns."""
    from app.network import control

    cursor.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
    session = row_to_dict(cursor.fetchone())
    if not session or session.get("api_user_id") or control.owner_of(session) is not None:
        return {"known": False}
    cursor.execute("SELECT payload FROM turns WHERE session_id=%s ORDER BY created_at ASC LIMIT 1", (session_id,))
    first = row_to_dict(cursor.fetchone())
    if not _started_by(_payload((first or {}).get("payload")), from_device):
        return {"known": False}
    # Task workers it started here: every one of them, nested or not, names
    # the job (their chain's root on this worker) as its parent.
    cursor.execute("SELECT id FROM sessions WHERE parent_session_id=%s AND is_worker=TRUE", (session_id,))
    ids = [session_id] + [row["id"] for row in cursor.fetchall()]
    tracked = {sid: None for sid in ids}
    live = _live_turns(cursor, ids)
    cost = _booked(cursor, tracked, _EPOCH) + _in_flight(cursor, tracked, live)
    own = [turn for turn in live if turn["session_id"] == session_id][:MAX_REMOTE_TURNS]
    return {
        "known": True,
        "cost_usd": round(cost, 6),
        "running": bool(live),
        "turns": [{"turn_id": turn["id"], "person": turn.get("source") in PERSON_SOURCES} for turn in own],
        "job_minutes": round(_job_minutes(session, now), 3),
    }


def answer_remote_usage(body, from_device, now=None):
    """``remote-usage`` on the worker that runs the jobs. Blocking.

    Answers only for sessions this worker owns whose first turn records
    ``from_device`` as the worker that started them (a Task child it
    dispatched, a chat it started); any other id reads as unknown. The cost
    is cumulative: the job's own turns, the Task workers it started here and
    its running turns' per-call costs. Raises ``PermissionError`` /
    ``ValueError`` (a named refusal).
    """
    from app.core.remote_task import _require_worker_member

    _require_worker_member(from_device)
    if not isinstance(body, dict) or body.get("version") != REMOTE_WIRE_VERSION:
        raise ValueError("Unsupported usage request version")
    ids = body.get("session_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_REMOTE_IDS
            or any(not isinstance(i, str) or not i.strip() or len(i) > 200 for i in ids)):
        raise ValueError(f"session_ids must list 1 to {MAX_REMOTE_IDS} ids")
    now = now or datetime.now(timezone.utc)
    with get_db() as conn:
        cursor = conn.cursor()
        jobs = {sid: _job_usage(cursor, sid, from_device, now) for sid in dict.fromkeys(ids)}
    return {"version": REMOTE_WIRE_VERSION, "jobs": jobs}


async def handle_remote_usage(body, from_device):
    """``remote-usage`` control action."""
    return await run_sync(answer_remote_usage, body, from_device)


def _number(value, ceiling):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return None
    return min(float(value), ceiling)


def _entry(value):
    """One job in a ``remote-usage`` answer, validated, else None."""
    if not isinstance(value, dict):
        return None
    if value.get("known") is False:
        return {"known": False}
    cost = _number(value.get("cost_usd"), MAX_REPORTED_USD)
    if value.get("known") is not True or cost is None:
        return None
    raw = value.get("turns") if isinstance(value.get("turns"), list) else []
    turns = [{"turn_id": t["turn_id"], "person": t.get("person") is not False}
             for t in raw[:MAX_REMOTE_TURNS]
             if isinstance(t, dict) and isinstance(t.get("turn_id"), str) and 0 < len(t["turn_id"]) <= 200]
    return {"known": True, "cost_usd": cost, "running": value.get("running") is True or bool(turns),
            "turns": turns, "job_minutes": _number(value.get("job_minutes"), 10.0 ** 7) or 0.0}


def _book_mirrored(aid, session_ids, at):
    """A worker without ``remote-usage``: book from its mirrored session cost."""
    from app.db import database

    for session_id in session_ids:
        row = database.get_session(session_id)
        if row is not None:
            store.record_remote_usage(aid, session_id, row.get("total_cost_usd") or 0, active=False, at=at)


async def refresh_remote(now=None):
    """Ask each worker that runs a remote job what the job has spent; book the rise.

    Returns the jobs whose worker answered, each with its live turns and
    minutes worked, for :func:`plan_remote`. A worker that cannot be reached
    keeps what was counted for it and is asked again on the next pass; one on
    an older release is read from the mirror. Never asks anyone when the
    assistant has no remote jobs.
    """
    from app.network import control

    aid = assistant_id()
    at = now or datetime.now(timezone.utc)
    jobs = await run_sync(store.remote_jobs_to_check, aid, REMOTE_IDLE_FOR, at)
    by_device: dict = {}
    for job in jobs:
        by_device.setdefault(job["device_id"], []).append(job)
    observed = []
    for device_id, group in by_device.items():
        for index in range(0, len(group), MAX_REMOTE_IDS):
            chunk = group[index:index + MAX_REMOTE_IDS]
            ids = [job["session_id"] for job in chunk]
            try:
                answer = await control.call(device_id, REMOTE_USAGE_ACTION,
                                            {"version": REMOTE_WIRE_VERSION, "session_ids": ids},
                                            timeout=REMOTE_TIMEOUT)
            except control.ControlRefused as exc:
                logger.info("Assistant limits: %s refused remote-usage (%s); reading its mirror", device_id, exc)
                await run_sync(_book_mirrored, aid, ids, at)
                continue
            except Exception as exc:  # noqa: BLE001 - unreachable: asked again next pass
                logger.info("Assistant limits: %s did not answer remote-usage: %s", device_id, exc)
                continue
            entries = answer.get("jobs") if isinstance(answer, dict) else None
            for job in chunk:
                entry = _entry(entries.get(job["session_id"])) if isinstance(entries, dict) else None
                if entry is None:
                    continue
                if not entry["known"]:
                    await run_sync(store.note_remote_unknown, aid, job["session_id"], at)
                    continue
                await run_sync(store.record_remote_usage, aid, job["session_id"], entry["cost_usd"],
                               active=entry["running"], at=at)
                observed.append({**job, **entry})
    return observed


def plan_remote(state, observed):
    """The remote jobs the limits stop on this pass.

    Each stop is a dict with ``session_id``, ``device_id``, ``kind``,
    ``label``, ``reason`` and ``turn_ids`` (the live turns no person started).
    A job with a live turn already stopped is left out.
    """
    stops = []
    for job in observed:
        turn_ids = [turn["turn_id"] for turn in job["turns"] if not turn["person"]]
        if not turn_ids:
            continue
        if state["limit_reached"]:
            reason = "daily_limit"
        elif state["job_limit_enabled"] and job["job_minutes"] >= state["max_job_minutes"]:
            reason = "job_time"
        else:
            continue
        if any(limits_db.stopped(turn["turn_id"]) for turn in job["turns"]):
            continue
        stops.append(dict(session_id=job["session_id"], device_id=job["device_id"], kind=job["kind"],
                          label=job.get("label"), reason=reason, turn_ids=turn_ids))
    return stops


_STOP_TEXT = {"daily_limit": "Stopped at the assistant's daily spending limit",
              "job_time": "Stopped at the assistant's job time limit"}


async def _stop_remote(stop):
    """Stop one remote job. True when the stop was made (recorded once, then announced).

    A Task child goes through the S8 stop path only (teardown, else
    ``stop_remote_child``), so its approvals are withdrawn and a message
    cannot resume it, as for any other stop. A stop its worker refused
    before (an older release) is not sent again every pass.
    """
    from app.network import control

    session_id, device_id = stop["session_id"], stop["device_id"]
    if stop["kind"] == TASK_JOB:
        from app.core import provider_agent_supervisor as supervisor
        from app.core.remote_task import stop_remote_child
        from app.db import remote_tasks

        dispatch = await run_sync(remote_tasks.get_dispatch, session_id)
        if dispatch and dispatch.get("stop_state") == remote_tasks.STOP_REFUSED:
            logger.info("Assistant limits: %s refused to stop %s before; not asking again",
                        device_id, session_id)
            return False
        text = _STOP_TEXT[stop["reason"]]
        reason = "assistant_" + stop["reason"]
        record = supervisor.get_worker(dispatch["root_session_id"], session_id) if dispatch else None
        if record is not None:
            result = await supervisor.teardown_worker(record, reason=reason, summary=text,
                                                      preview=text + ".")
        else:
            result = await stop_remote_child(session_id, dispatch["target_device_id"] if dispatch else device_id,
                                             reason=reason, summary=text)
        if (result or {}).get("outcome") == "remote_stop_refused":
            logger.warning("Assistant limits: could not stop %s on %s: %s", session_id, device_id,
                           (result or {}).get("detail"))
            return False
        return True
    stopped = False
    for turn_id in stop["turn_ids"]:
        answer = await control.interrupt(device_id, session_id, "stop", turn_id=turn_id, source="assistant")
        stopped = stopped or bool((answer or {}).get("interrupted"))
    return stopped


def message_refusal(worker_session_id):
    """Why SendMessage may not reach the assistant's Task child on another
    worker now, or None. Never raises.

    At the limit a message would start an unattended turn there that this
    worker's turn check cannot see, so it is refused here, as that turn is
    refused for a local child.
    """
    try:
        if store.get_remote_job(assistant_id(), worker_session_id) is None:
            return None
        state = status()
    except Exception:  # noqa: BLE001 - never block unrelated messages
        logger.warning("Assistant limits: message check failed for %s", worker_session_id, exc_info=True)
        return None
    return _reached_text(state) if state["limit_reached"] else None


def wake_remote_job(worker_session_id):
    """A message resumed the assistant's Task child on another worker: ask
    about its spend again even if it had aged out as idle. Never raises."""
    try:
        store.wake_remote_job(assistant_id(), worker_session_id)
    except Exception:  # noqa: BLE001
        logger.debug("Assistant limits: could not wake %s", worker_session_id, exc_info=True)


def _list(labels):
    named = [f'"{label}"' for label in labels if label]
    shown = named[:3]
    more = len(labels) - len(shown)
    if not shown:
        return ""
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def reached_notice(state, stopped, home_id=None):
    jobs = [s["label"] for s in stopped if s["session_id"] != home_id]
    reply = any(s["session_id"] == home_id for s in stopped)
    parts = []
    if reply:
        parts.append("my current reply")
    if jobs:
        names = _list(jobs)
        parts.append(f"{len(jobs)} background {'job' if len(jobs) == 1 else 'jobs'}" + (f" ({names})" if names else ""))
    what = "stopped " + " and ".join(parts) + " and " if parts else ""
    return (f"Today's {_money(state['daily_spend_limit_usd'])} spending limit is reached "
            f"({_money(state['spent_today_usd'])} spent), so I {what}won't start new work until tomorrow; "
            f"{WHERE_TO_CHANGE}.")


def warning_notice(state):
    percent = int(state["spent_today_usd"] / state["daily_spend_limit_usd"] * 100)
    return (f"Heads up: I've used {percent}% of today's {_money(state['daily_spend_limit_usd'])} spending limit "
            f"({_money(state['spent_today_usd'])} so far); {WHERE_TO_CHANGE}.")


def time_notice(state, stopped):
    labels = [s["label"] for s in stopped]
    limit = _duration(state["max_job_minutes"])
    if len(labels) == 1:
        subject = f'The background job "{labels[0]}"' if labels[0] else "A background job"
        return f"{subject} ran past its {limit} limit, so I stopped it; {WHERE_TO_CHANGE}."
    names = _list(labels)
    return (f"{len(labels)} background jobs ran past their {limit} limit, so I stopped them"
            + (f" ({names})" if names else "") + f"; {WHERE_TO_CHANGE}.")


async def _post(home_id, text):
    from app.core import turn_recorder
    return await turn_recorder.record_brief(home_id, text, extra_payload={"source": NOTICE_SOURCE})


async def _post_notices(state):
    """Post pending notices into the home, only between its turns."""
    home_id, hold = await run_sync(service.brief_destination)
    if hold is not None or not home_id:
        return
    aid = assistant_id()
    key = f"{state['day']}|{state['daily_spend_limit_usd']:g}"
    pending = await run_sync(limits_db.unreported_stops, aid)
    by_limit = [s for s in pending if s["reason"] == "daily_limit"]
    by_time = [s for s in pending if s["reason"] == "job_time"]
    if state["limit_reached"]:
        if await run_sync(limits_db.claim_notice, aid, "reached", key):
            # Reaching the limit also covers the 80% notice for today.
            await run_sync(limits_db.claim_notice, aid, "warned", key)
            if await _post(home_id, reached_notice(state, by_limit, home_id)) is None:
                await run_sync(limits_db.release_notice, aid, "reached", key)
                return
        await run_sync(limits_db.mark_reported, [s["turn_id"] for s in by_limit])
    elif (state["spend_limit_enabled"] and state["spent_today_usd"] >= state["daily_spend_limit_usd"] * WARN_FRACTION
          and await run_sync(limits_db.claim_notice, aid, "warned", key)):
        if await _post(home_id, warning_notice(state)) is None:
            await run_sync(limits_db.release_notice, aid, "warned", key)
    if by_time and await _post(home_id, time_notice(state, by_time)) is not None:
        await run_sync(limits_db.mark_reported, [s["turn_id"] for s in by_time])
