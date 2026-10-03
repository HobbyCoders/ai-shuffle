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
Chats started on another worker spend there and are not counted here.

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
* One plain notice is posted into the home conversation between turns: at
  80% (once per day), when the limit is reached, and when a job is stopped.
"""
from __future__ import annotations

import json
import logging
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


def _measure(now=None) -> dict:
    """Spend today plus what enforcement needs, from one connection."""
    day, start, resets_at, tz_name = day_window(now)
    with get_db() as conn:
        cursor = conn.cursor()
        tracked = _tracked(cursor)
        live = _live_turns(cursor, tracked) if tracked else []
        spent = (_booked(cursor, tracked, start) + _in_flight(cursor, tracked, live)) if tracked else 0.0
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
        "note": ("Today's spend for the assistant conversation and every job it started on this worker, "
                 "in US dollars at API prices (subscription use is an estimate). Only the owner can change "
                 "these limits."),
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
        state, stops = await run_sync(plan, now)
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
    try:
        await _post_notices(state)
    except Exception:
        logger.exception("Assistant limits: notices failed")


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
