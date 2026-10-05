"""Resume a chat or a Task worker by itself once a usage limit resets (#1831).

A provider usage limit (HTTP 429, "You've hit your session limit · resets
11:10pm (America/New_York)") ends the turn: the turn scheduler's failover
policy sees ``api_error_status == 429`` on the settled turn and only posts the
"usage limit reached" notice (``account_failover``). With the per-provider
setting "Resume when the limit resets" on, this module also arranges one
resume shortly after the reset:

* **Reset time.** Structured readings first: the Claude windows the CLI
  reported (``usage_limits``: a ``rejected`` window is stored at 0% remaining
  with its ``resets_at``) or the provider's usage snapshot (Codex and Grok
  windows at 100% used, ``provider_usage_snapshots``). Only when none is found
  is the error text parsed: an epoch after ``limit reached|``, or "resets
  11:10pm (America/New_York)" with its time zone (the worker's local zone when
  the text names none). No reset time, no resume: the chat says so.
* **Schedule.** One durable wake-up (``pending_wakeups``, source
  :data:`LIMIT_RESUME_SOURCE`), so it survives a restart the way every wake-up
  does. It fires :data:`GRACE_SECONDS` after the reset, plus a random delay of
  up to :data:`JITTER_SECONDS`, plus :data:`STAGGER_SECONDS` for each other
  chat on this worker already due at about the same time, so several chats do
  not all start together. The row's ``reason`` column carries this module's
  metadata as JSON (provider, windows, limit key, owner of a worker).
* **Once per limit per chat.** The limit is identified by provider and reset
  minute (:func:`limit_key`). Every decision is recorded on the chat's own
  log as a ``system`` row with subtype :data:`SYSTEM_SUBTYPE`, which is both
  what the chat shows ("Paused: usage limit. Resumes at 11:10 pm") and the
  durable record that a limit was already handled.
* **Never for a chat the owner stopped, archived or moved.** Scheduling and
  firing both check the Stop latch, the archive guard and that the session
  still runs on this worker. A message from the owner cancels the wake-up
  (``turn_manager.submit`` drops a session's pending wake-ups on every
  user-authored turn), and so does Cancel (:func:`cancel`).
* **Push back.** Just before firing, the provider's usage is read again; a
  window that blocked the turn and is still exhausted moves the resume to its
  new reset, at most :data:`MAX_PUSHBACKS` times.
* **Compact first.** With "Compact before resuming" on (the default) a
  session whose leg can compact (:data:`COMPACTING_LEGS`) gets a real
  ``/compact`` turn queued ahead of the continuation, so the continuation
  re-reads a small context instead of the whole conversation.
* **Task workers.** A background worker stopped by the limit keeps its row in
  the parent's running tasks (marked paused) and its parent is not woken. The
  resume continues the worker through the same path a SendMessage to a
  finished worker uses (``turn_scheduler.on_agent_message`` with
  ``reply_to``), so the parent hears once, with the worker's report, that it
  was paused and resumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: ``pending_wakeups.source`` of a scheduled limit resume.
LIMIT_RESUME_SOURCE = "limit_resume"
#: ``system`` row subtype recorded on the chat's log.
SYSTEM_SUBTYPE = "limit_resume"
#: ``system_settings`` key prefix; the provider key follows.
SETTING_PREFIX = "limit_resume:"
#: The provider key of Claude Code (the delegated leg, which has no
#: ``model_providers`` row).
CLAUDE_KEY = "claude"

DEFAULTS: Dict[str, bool] = {
    "resume_on_reset": False,
    "compact_before_resume": True,
}

#: Wait this long after the reset before resuming.
GRACE_SECONDS = 60.0
#: Plus a random delay of up to this many seconds.
JITTER_SECONDS = 60.0
#: Plus this much per other chat already due near the same time.
STAGGER_SECONDS = 60.0
#: Resumes due within this many seconds of each other share a stagger cluster.
STAGGER_WINDOW_SECONDS = 15 * 60.0
#: How many times a resume is moved because the limit was still in force.
MAX_PUSHBACKS = 3
#: A structured window counts only when the provider reported it this recently
#: before the stop (an old exhausted bucket of another model says nothing about
#: this stop).
RECENT_WINDOW_SECONDS = 30 * 60.0
#: A reset further away than this is not believed.
MAX_RESET_AHEAD_SECONDS = 8 * 24 * 3600.0

#: Legs that carry out a ``/compact`` turn: Claude Code itself, and the native
#: legs through the engine's own compaction (``NativeLoop`` manual compaction).
COMPACTING_LEGS = frozenset({"delegated", "anthropic", "openai_responses", "openrouter"})

#: The continuation turn. Labelled, so it never reads as the owner's words.
RESUME_PROMPT = (
    "[auto-resume] Your previous turn was stopped by the provider's usage "
    "limit, which has now reset. Continue the task you were working on, "
    "picking up exactly where you left off. If everything was already "
    "complete, stop after a brief acknowledgement; do not invent new work."
)
COMPACT_PROMPT = "/compact"
#: The sender a resumed worker sees, and the message it gets.
RESUME_SENDER = "AI Shuffle"
WORKER_RESUME_TEXT = (
    "Your previous turn was stopped by the provider's usage limit, which has "
    "now reset. Continue your task where you left off and finish with your "
    "report."
)
#: The note a resumed worker's report carries to its parent.
WORKER_REPORT_NOTE = (
    "This worker was paused by a usage limit and resumed by itself after the "
    "limit reset."
)
#: Turn payload key marking a worker continuation this module started.
PAYLOAD_KEY = "limit_resumed"
#: The registry summary of a paused worker.
PAUSED_SUMMARY = "Paused: usage limit"

_STATUS_SCHEDULED = "scheduled"
_STATUS_DEFERRED = "deferred"
_STATUS_RESUMED = "resumed"
_STATUS_CANCELLED = "cancelled"
_STATUS_DROPPED = "dropped"
_STATUS_NO_RESET = "no_reset"
#: Statuses that mean this limit was handled for the chat.
_HANDLED = frozenset({_STATUS_SCHEDULED, _STATUS_DEFERRED, _STATUS_RESUMED,
                      _STATUS_CANCELLED, _STATUS_NO_RESET})

_rng = random.Random()


# ─── Settings ───────────────────────────────────────────────────────────────


def _setting_key(provider_key: str) -> str:
    return SETTING_PREFIX + provider_key


def _parse_setting(raw: Optional[str]) -> Dict[str, bool]:
    out = dict(DEFAULTS)
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return out
    if isinstance(data, dict):
        for name in DEFAULTS:
            if isinstance(data.get(name), bool):
                out[name] = data[name]
    return out


def get_settings(provider_key: Optional[str]) -> Dict[str, bool]:
    """One provider's two switches (defaults when never set). Sync (DB)."""
    if not provider_key:
        return dict(DEFAULTS)
    from app.db import database

    return _parse_setting(database.get_system_setting_cached(_setting_key(provider_key)))


def provider_keys() -> List[str]:
    """Every provider a setting can be kept for: Claude and each provider row."""
    from app.db import database

    keys = [CLAUDE_KEY]
    try:
        keys += [str(row.get("id")) for row in database.get_all_model_providers() if row.get("id")]
    except Exception:  # noqa: BLE001 — a listing failure still serves Claude
        logger.debug("limit_resume: provider listing failed", exc_info=True)
    return keys


def valid_provider_key(provider_key: str) -> bool:
    if provider_key == CLAUDE_KEY:
        return True
    from app.db import database

    return database.get_model_provider(provider_key, include_token=False) is not None


def all_settings() -> Dict[str, Any]:
    """The settings payload: every provider's switches and the defaults."""
    return {
        "providers": {key: get_settings(key) for key in provider_keys()},
        "defaults": dict(DEFAULTS),
    }


def update_settings(provider_key: str, **changes: Optional[bool]) -> Dict[str, bool]:
    """Change one provider's switches. ``None`` leaves a switch alone.

    Raises ``KeyError`` for an unknown provider and ``ValueError`` for an
    empty change or a value that is not a bool. Stored as one shared
    ``system_settings`` row, so it reaches the owner's other workers like the
    loop guard's settings.
    """
    if not valid_provider_key(provider_key):
        raise KeyError(provider_key)
    current = get_settings(provider_key)
    touched = False
    for name in DEFAULTS:
        value = changes.get(name)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        current[name] = value
        touched = True
    if not touched:
        raise ValueError("Nothing to change: send resume_on_reset or compact_before_resume")
    from app.db import database

    database.set_system_setting(_setting_key(provider_key), json.dumps(current, sort_keys=True))
    return current


# ─── Which provider a session runs on ───────────────────────────────────────


def session_leg(session: Any) -> Optional[str]:
    """The leg ``session`` runs on (``delegated``, ``anthropic``...), or None."""
    if not session:
        return None
    try:
        from app.api.command_context import command_provider_kind

        return command_provider_kind(None, session, None, None, None)
    except Exception:  # noqa: BLE001 — unknown leg: no resume
        logger.debug("limit_resume: leg lookup failed for %s", session.get("id"), exc_info=True)
        return None


def session_provider_key(session: Any, leg: Optional[str]) -> Optional[str]:
    """The provider key whose setting governs ``session``.

    Claude Code (the delegated leg) is :data:`CLAUDE_KEY`; any other leg is the
    session's bound provider row, read from the session or its ``prov:`` model.
    """
    if not session or not leg or leg in ("voice", "unavailable"):
        return None
    if leg == "delegated":
        return CLAUDE_KEY
    provider_id = session.get("provider_id")
    if not provider_id:
        try:
            from app.core.query_options import decode_provider_model

            decoded = decode_provider_model(session.get("model"))
        except Exception:  # noqa: BLE001
            decoded = None
        if decoded:
            provider_id = decoded[0]
    return provider_id or None


# ─── Reset time ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResetInfo:
    """When a limit resets, and how that was learned."""

    at: float
    source: str  # "structured" | "text"
    windows: Tuple[str, ...] = field(default_factory=tuple)
    tz: Optional[str] = None


_EPOCH_RE = re.compile(r"limit\s+reached\s*\|\s*(\d{10,13})", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_RESETS_RE = re.compile(
    r"\bresets?\s+(?:at\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?\s?m\b\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\))?",
    re.IGNORECASE,
)


def _zone(name: Optional[str]) -> Optional[tzinfo]:
    if not name:
        return None
    if name.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone name: fall back
        return None


def _local_zone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def reset_from_text(text: Optional[str], *, now: Optional[float] = None) -> Optional[ResetInfo]:
    """The reset time a limit message states, or None.

    Understands ``Claude AI usage limit reached|<epoch>`` and Claude Code's
    "resets 11:10pm (America/New_York)" / "resets Oct 6, 3pm (Europe/Berlin)".
    A time without a date is the next such time after ``now``. With no zone in
    the text the worker's local zone is used.
    """
    if not text:
        return None
    current = time.time() if now is None else now
    match = _EPOCH_RE.search(text)
    if match:
        stamp = float(match.group(1))
        stamp = stamp / 1000.0 if stamp >= 1e12 else stamp
        return _believable(ResetInfo(at=stamp, source="text"), current)
    for match in _RESETS_RE.finditer(text):
        info = _clock_reset(match, current)
        if info is not None:
            return info
    return None


def _clock_reset(match: "re.Match[str]", now: float) -> Optional[ResetInfo]:
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower().replace(".", "").replace(" ", "")
    if not ampm and match.group("minute") is None:
        return None  # "resets 3" is not a time
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    elif hour > 23:
        return None
    if minute > 59:
        return None
    tz_name = match.group("tz")
    zone = _zone(tz_name) or _local_zone()
    here = datetime.fromtimestamp(now, zone)
    try:
        if match.group("mon"):
            month = _MONTHS[match.group("mon")[:3].lower()]
            day = int(match.group("day"))
            when = here.replace(month=month, day=day, hour=hour, minute=minute,
                                second=0, microsecond=0)
            if when < here - timedelta(days=1):
                when = when.replace(year=when.year + 1)
        else:
            when = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= here:
                when = when + timedelta(days=1)
    except ValueError:
        return None
    return _believable(
        ResetInfo(at=when.timestamp(), source="text", tz=tz_name if _zone(tz_name) else None),
        now,
    )


def _believable(info: ResetInfo, now: float) -> Optional[ResetInfo]:
    if info.at <= now - 60 or info.at - now > MAX_RESET_AHEAD_SECONDS:
        return None
    return info


def _claude_windows() -> List[Dict[str, Any]]:
    from app.core.usage_limits import roll_over_windows, usage_limits_monitor

    return roll_over_windows(list(usage_limits_monitor._windows))


def _provider_windows(provider_id: str) -> List[Dict[str, Any]]:
    from app.db import database

    for row in database.get_all_provider_usage_snapshots():
        if row.get("provider_id") == provider_id:
            return [w for w in row.get("windows") or [] if isinstance(w, dict)]
    return []


def _exhausted(provider_key: str, window: Dict[str, Any]) -> bool:
    util = window.get("utilization")
    if isinstance(util, bool) or not isinstance(util, (int, float)):
        return False
    # Claude's windows carry REMAINING percent; provider snapshots carry USED.
    return util <= 0.0 if provider_key == CLAUDE_KEY else util >= 100.0


def _blocking_windows(
    provider_key: str,
    *,
    now: float,
    only: Optional[Iterable[str]] = None,
    recent_since: Optional[float] = None,
) -> List[Tuple[str, float]]:
    """``(type, reset epoch)`` for each exhausted window with a future reset."""
    from app.core.usage_limits import reset_epoch_seconds

    try:
        windows = _claude_windows() if provider_key == CLAUDE_KEY else _provider_windows(provider_key)
    except Exception:  # noqa: BLE001 — no reading is "unknown", never a guess
        logger.debug("limit_resume: usage read failed for %s", provider_key, exc_info=True)
        return []
    wanted = set(only) if only is not None else None
    out: List[Tuple[str, float]] = []
    for window in windows:
        kind = window.get("type")
        if not isinstance(kind, str) or (wanted is not None and kind not in wanted):
            continue
        if not _exhausted(provider_key, window):
            continue
        reset = reset_epoch_seconds(window.get("resets_at"))
        if reset is None or reset <= now:
            continue
        if recent_since is not None:
            observed = window.get("observed_at")
            # Claude windows record when they were reported (epoch ms);
            # provider snapshots are re-read on every poll and carry none.
            if isinstance(observed, (int, float)) and observed / 1000.0 < recent_since:
                continue
        out.append((kind, reset))
    return out


def structured_reset(provider_key: Optional[str], *, now: Optional[float] = None) -> Optional[ResetInfo]:
    """The reset of the windows the provider reports exhausted right now.

    The latest reset wins: every window still exhausted blocks the resume.
    Sync (reads the usage snapshot).
    """
    if not provider_key:
        return None
    current = time.time() if now is None else now
    blocking = _blocking_windows(
        provider_key, now=current, recent_since=current - RECENT_WINDOW_SECONDS,
    )
    if not blocking:
        return None
    at = max(reset for _, reset in blocking)
    return _believable(
        ResetInfo(at=at, source="structured", windows=tuple(sorted({k for k, _ in blocking}))),
        current,
    )


def find_reset(
    provider_key: Optional[str], error_text: Optional[str], *, now: Optional[float] = None,
) -> Optional[ResetInfo]:
    """Structured reset time first, the error text only as the fallback."""
    return structured_reset(provider_key, now=now) or reset_from_text(error_text, now=now)


def still_blocked_until(
    provider_key: Optional[str], windows: Iterable[str], *, now: Optional[float] = None,
) -> Optional[float]:
    """When the windows that stopped the turn reset, if they are still exhausted.

    Only the windows named at the stop are read, so an unrelated bucket cannot
    hold the resume. None when nothing structured says the limit is in force.
    """
    names = [w for w in windows or () if isinstance(w, str)]
    if not provider_key or not names:
        return None
    current = time.time() if now is None else now
    blocking = _blocking_windows(provider_key, now=current, only=names)
    return max((reset for _, reset in blocking), default=None)


def limit_key(provider_key: str, reset_at: float) -> str:
    """One limit: a provider and the minute it resets."""
    return f"{provider_key}:{int(reset_at // 60)}"


def plan_fire_at(
    reset_at: float, *, now: float, taken: Iterable[float], rng: Optional[random.Random] = None,
) -> float:
    """When to resume: after the reset, staggered behind other resumes due then.

    ``taken`` are the fire times of the resumes already scheduled on this
    worker. Each one within :data:`STAGGER_WINDOW_SECONDS` of this resume's
    base time pushes it back by :data:`STAGGER_SECONDS`; a random delay of up
    to :data:`JITTER_SECONDS` keeps two workers apart as well.
    """
    base = max(reset_at, now) + GRACE_SECONDS
    slot = sum(1 for at in taken if abs(float(at) - base) < STAGGER_WINDOW_SECONDS)
    return base + slot * STAGGER_SECONDS + (rng or _rng).uniform(0.0, JITTER_SECONDS)


# ─── Durable bits ───────────────────────────────────────────────────────────


def meta_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata a limit-resume wake-up row carries in ``reason``."""
    try:
        meta = json.loads(row.get("reason") or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _taken_fire_times(exclude_session: Optional[str]) -> List[float]:
    from app.core.config import settings
    from app.db import wakeups as wakeups_db

    out = []
    for row in wakeups_db.get_pending_wakeups(settings.effective_instance_id):
        if row.get("source") != LIMIT_RESUME_SOURCE or row.get("session_id") == exclude_session:
            continue
        try:
            out.append(float(row.get("fire_at") or 0.0))
        except (TypeError, ValueError):
            continue
    return out


def paused_workers_of(owner_session_id: str) -> List[str]:
    """Worker session ids this session waits on that are paused by a limit.

    The park watchdog leaves such a parent alone: its worker is not lost, it
    resumes at a known time. Sync (DB).
    """
    from app.core.config import settings
    from app.db import wakeups as wakeups_db

    out = []
    for row in wakeups_db.get_pending_wakeups(settings.effective_instance_id):
        if row.get("source") == LIMIT_RESUME_SOURCE and meta_of(row).get("owner") == owner_session_id:
            out.append(row.get("session_id"))
    return [s for s in out if s]


def handled_limit_keys(session_id: str) -> set:
    """Limit keys already handled for this chat, from its own log. Sync (DB)."""
    from app.db.base import get_db, parse_jsonb

    keys = set()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM session_events WHERE session_id=%s "
            "AND event_type='system.note' ORDER BY seq DESC LIMIT 500",
            (session_id,),
        )
        rows = cursor.fetchall()
    for row in rows:
        payload = parse_jsonb(row["payload"])
        if not isinstance(payload, dict) or payload.get("subtype") != SYSTEM_SUBTYPE:
            continue
        data = payload.get("data") or {}
        if isinstance(data, dict) and data.get("status") in _HANDLED and data.get("limit_key"):
            keys.add(data["limit_key"])
    return keys


_CONTENT = {
    _STATUS_SCHEDULED: "Paused: usage limit. Resumes by itself after the reset.",
    _STATUS_DEFERRED: "Paused: usage limit, still in force. Resume moved to the next reset.",
    _STATUS_RESUMED: "Resumed after the usage limit reset.",
    _STATUS_CANCELLED: "Automatic resume cancelled.",
    _STATUS_DROPPED: "Automatic resume dropped.",
    _STATUS_NO_RESET: "Stopped by a usage limit with no reset time, so it will not resume by itself.",
}


async def _record(session_id: str, status: str, data: Dict[str, Any]) -> None:
    """Record one ``limit_resume`` row on a chat's log. Never raises."""
    content = data.pop("content", None) or _CONTENT.get(status, "")
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {
            "type": "system", "subtype": SYSTEM_SUBTYPE,
            "data": {**data, "status": status, "content": content},
        })
    except Exception:  # noqa: BLE001 — the row is a report
        logger.warning("limit_resume: could not record %s row for %s", status, session_id,
                       exc_info=True)


# ─── Guards ─────────────────────────────────────────────────────────────────


def _user_stopped(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    from app.core.task_registry import task_registry

    return task_registry.is_user_stopped(session_id)


def is_local(session: Any) -> bool:
    """Whether ``session`` still runs on this worker (it was not moved)."""
    if not session:
        return False
    from app.core.config import settings

    if session.get("instance_id") not in (None, "", settings.effective_instance_id):
        return False
    owner = session.get("owner_device_id")
    if owner:
        try:
            from app.network.runners import self_device_id

            return owner == self_device_id()
        except Exception:  # noqa: BLE001 — off a network there is no other owner
            return True
    return True


async def _refused(session_id: str, session: Any) -> Optional[str]:
    """Why ``session_id`` must not be resumed now, or None."""
    from app.core import archive_guard

    if session is None:
        return "deleted"
    if _user_stopped(session_id):
        return "stopped"
    if await archive_guard.is_archived_async(session_id):
        return "archived"
    if not is_local(session):
        return "moved"
    return None


# ─── Scheduling ─────────────────────────────────────────────────────────────


def _errors_text(terminal: Dict[str, Any]) -> str:
    errors = terminal.get("errors")
    if isinstance(errors, list):
        return " ".join(str(e) for e in errors)
    return str(errors or terminal.get("message") or "")


async def on_limit_stop(
    session_id: str,
    turn: Dict[str, Any],
    terminal: Dict[str, Any],
    *,
    worker_owner: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """A turn of ``session_id`` was stopped by a usage limit: schedule its resume.

    ``turn`` is the settled turn row, ``terminal`` its ``turn.completed``
    payload. ``worker_owner`` is the session waiting on a background Task
    worker (the worker path in ``provider_agent``); a worker session reached
    without one is left to that path. Returns the scheduled resume
    (``{"fire_at", "reset_at", "limit_key", ...}``) or None. Never raises.
    """
    try:
        return await _on_limit_stop(session_id, turn, terminal, worker_owner, now)
    except Exception:  # noqa: BLE001 — a resume must never break settlement
        logger.warning("limit_resume: scheduling failed for %s", session_id, exc_info=True)
        return None


async def _on_limit_stop(
    session_id: str,
    turn: Dict[str, Any],
    terminal: Dict[str, Any],
    worker_owner: Optional[str],
    now: Optional[float],
) -> Optional[Dict[str, Any]]:
    from app.core import wakeup_service
    from app.db import database
    from app.db.async_database import run_sync

    session: Any = await run_sync(database.get_session, session_id)
    if session and session.get("parent_session_id") and not worker_owner:
        return None  # a Task worker: provider_agent decides for it
    refused = await _refused(session_id, session)
    if refused is None and worker_owner:
        owner_row = await run_sync(database.get_session, worker_owner)
        refused = await _refused(worker_owner, owner_row)
    if refused is not None:
        logger.info("limit_resume: not resuming %s (%s)", session_id, refused)
        return None
    leg = await run_sync(session_leg, session)
    provider_key = session_provider_key(session, leg)
    switches = await run_sync(get_settings, provider_key)
    if not provider_key or not switches["resume_on_reset"]:
        return None

    current = time.time() if now is None else now
    reset = await run_sync(lambda: find_reset(provider_key, _errors_text(terminal), now=current))
    note_sessions = [session_id] + ([worker_owner] if worker_owner else [])
    if reset is None:
        for sid in note_sessions:
            await _record(sid, _STATUS_NO_RESET, {
                "provider": provider_key,
                **({"task_id": session_id} if sid != session_id else {}),
            })
        logger.info("limit_resume: %s stopped by a limit with no reset time", session_id)
        return None

    key = limit_key(provider_key, reset.at)
    if key in await run_sync(handled_limit_keys, session_id):
        logger.info("limit_resume: %s already handled limit %s — not again", session_id, key)
        return None

    taken = await run_sync(_taken_fire_times, session_id)
    fire_at = plan_fire_at(reset.at, now=current, taken=taken)
    compact = bool(switches["compact_before_resume"]) and leg in COMPACTING_LEGS
    meta = {
        "provider": provider_key,
        "leg": leg,
        "reset_at": reset.at,
        "windows": list(reset.windows),
        "reset_source": reset.source,
        "tz": reset.tz,
        "limit_key": key,
        "compact": compact and not worker_owner,
        "owner": worker_owner,
        "pushbacks": 0,
    }
    row = await wakeup_service.schedule(
        session_id=session_id,
        profile_id=turn.get("profile_id") or session.get("profile_id"),
        project_id=turn.get("project_id") or session.get("project_id"),
        prompt=RESUME_PROMPT,
        delay_seconds=max(0.0, fire_at - time.time()),
        reason=json.dumps(meta, sort_keys=True),
        model=(turn.get("overrides") or {}).get("model"),
        source=LIMIT_RESUME_SOURCE,
    )
    if not row:
        return None
    data = {
        "provider": provider_key, "resume_at": float(row.get("fire_at") or fire_at),
        "reset_at": reset.at, "limit_key": key, "compact": meta["compact"],
    }
    await _record(session_id, _STATUS_SCHEDULED, dict(data))
    if worker_owner:
        await _record(worker_owner, _STATUS_SCHEDULED, {**data, "task_id": session_id})
    logger.info(
        "limit_resume: %s paused by %s limit (reset %s from %s) — resumes in %.0fs",
        session_id, provider_key, int(reset.at), reset.source, max(0.0, fire_at - current),
    )
    return {**data, "fire_at": data["resume_at"], "wakeup_id": row.get("id")}


# ─── Firing ─────────────────────────────────────────────────────────────────


async def fire(row: Dict[str, Any]) -> Optional[str]:
    """Run one claimed limit-resume wake-up (``wakeup_service._fire``).

    Returns the continuation turn id, or None when it was pushed back,
    dropped, or handed back to the wake-up service's retry. Never raises.
    """
    try:
        return await _fire(row)
    except Exception:  # noqa: BLE001
        logger.warning("limit_resume: firing %s failed", row.get("id"), exc_info=True)
        return None


async def _fire(row: Dict[str, Any]) -> Optional[str]:
    from app.core import wakeup_service
    from app.db import database
    from app.db.async_database import run_sync

    meta = meta_of(row)
    session_id = row["session_id"]
    owner = meta.get("owner")
    note_sessions = [session_id] + ([owner] if owner else [])
    session = await run_sync(database.get_session, session_id)
    refused = await _refused(session_id, session)
    if refused is None and owner:
        refused = await _refused(owner, await run_sync(database.get_session, owner))
    if refused is not None:
        logger.info("limit_resume: dropping resume of %s (%s)", session_id, refused)
        if refused not in ("moved", "deleted"):
            for sid in note_sessions:
                await _record(sid, _STATUS_DROPPED, {
                    "reason": refused, "limit_key": meta.get("limit_key"),
                    **({"task_id": session_id} if sid != session_id else {}),
                })
        return None

    now = time.time()
    blocked = await run_sync(
        lambda: still_blocked_until(meta.get("provider"), meta.get("windows") or [], now=now)
    )
    if blocked is not None and int(meta.get("pushbacks") or 0) < MAX_PUSHBACKS:
        meta["pushbacks"] = int(meta.get("pushbacks") or 0) + 1
        taken = await run_sync(_taken_fire_times, session_id)
        fire_at = plan_fire_at(blocked, now=now, taken=taken)
        again = await wakeup_service.schedule(
            session_id=session_id, profile_id=row.get("profile_id"),
            project_id=row.get("project_id"), prompt=row.get("prompt") or RESUME_PROMPT,
            delay_seconds=max(0.0, fire_at - now), reason=json.dumps(meta, sort_keys=True),
            model=row.get("model"), source=LIMIT_RESUME_SOURCE,
        )
        if again:
            for sid in note_sessions:
                await _record(sid, _STATUS_DEFERRED, {
                    "provider": meta.get("provider"), "resume_at": float(again.get("fire_at") or fire_at),
                    "reset_at": blocked, "limit_key": meta.get("limit_key"),
                    **({"task_id": session_id} if sid != session_id else {}),
                })
            logger.info("limit_resume: %s still limited — moved to %s", session_id, int(fire_at))
        return None

    if owner:
        return await _resume_worker(session_id, owner, meta)
    return await _resume_chat(row, meta)


async def _resume_worker(worker_id: str, owner: str, meta: Dict[str, Any]) -> Optional[str]:
    from app.core import turn_scheduler

    turn_id = await turn_scheduler.on_agent_message(
        worker_id, RESUME_SENDER, WORKER_RESUME_TEXT, reply_to=owner,
        extra_payload={PAYLOAD_KEY: {"limit_key": meta.get("limit_key"),
                                     "reset_at": meta.get("reset_at")}},
    )
    if not turn_id or turn_id == turn_scheduler.HELD:
        for sid, extra in ((worker_id, {}), (owner, {"task_id": worker_id})):
            await _record(sid, _STATUS_DROPPED, {"reason": "refused", **extra,
                                                 "limit_key": meta.get("limit_key")})
        return None
    for sid, extra in ((worker_id, {}), (owner, {"task_id": worker_id})):
        await _record(sid, _STATUS_RESUMED, {"limit_key": meta.get("limit_key"), **extra})
    logger.info("limit_resume: worker %s resumed for %s as turn %s", worker_id, owner, turn_id)
    return turn_id


async def _resume_chat(row: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    from app.core import archive_guard, turn_manager, wakeup_service

    session_id = row["session_id"]
    profile_id = await wakeup_service._resolve_profile_id(row)
    if not profile_id:
        await wakeup_service._announce_dropped_wake(row, "no agent profile could be resolved")
        return None
    api_user_id = await asyncio.to_thread(wakeup_service._session_api_user_id, session_id)
    overrides = {"model": row["model"]} if row.get("model") else None
    payload = {"api_user_id": api_user_id} if api_user_id else None

    async def submit(prompt: str) -> Optional[str]:
        return await turn_manager.submit_async(
            session_id, "wakeup", prompt=prompt, profile_id=profile_id,
            project_id=row.get("project_id"), overrides=overrides,
            payload=dict(payload) if payload else None,
        )

    compacted = bool(meta.get("compact")) and meta.get("leg") in COMPACTING_LEGS
    try:
        if compacted:
            # Queued first, so the continuation re-reads the compacted context.
            await submit(COMPACT_PROMPT)
        turn_id = await submit(RESUME_PROMPT)
    except archive_guard.SessionArchived:
        return None
    except Exception as exc:  # noqa: BLE001 — the wake-up service retries it
        await wakeup_service._rearm_after_failed_submit(row, f"{type(exc).__name__}: {exc}")
        return None
    if not turn_id:
        await wakeup_service._rearm_after_failed_submit(row, "the turn manager refused the resume")
        return None
    await _record(session_id, _STATUS_RESUMED, {
        "limit_key": meta.get("limit_key"), "compacted_first": compacted,
    })
    logger.info("limit_resume: %s resumed as turn %s (compact first: %s)",
                session_id, turn_id, compacted)
    return turn_id


# ─── Cancel ─────────────────────────────────────────────────────────────────


def pending(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's scheduled resume (``{"fire_at", ...meta}``), or None. Sync."""
    from app.db import wakeups as wakeups_db

    rows = wakeups_db.get_session_wakeups(session_id, LIMIT_RESUME_SOURCE)
    if not rows:
        return None
    row = rows[0]
    return {**meta_of(row), "fire_at": float(row.get("fire_at") or 0.0), "wakeup_id": row.get("id")}


async def cancel(session_id: str) -> bool:
    """Cancel ``session_id``'s scheduled resume, or that of the paused worker
    with this id. Returns whether one was pending."""
    from app.core import wakeup_service
    from app.db.async_database import run_sync

    current = await run_sync(pending, session_id)
    removed = await wakeup_service.cancel_session_wakeups_async(session_id, LIMIT_RESUME_SOURCE)
    if not removed:
        return False
    owner = (current or {}).get("owner")
    await _record(session_id, _STATUS_CANCELLED, {"limit_key": (current or {}).get("limit_key")})
    if owner:
        await _record(owner, _STATUS_CANCELLED, {"limit_key": (current or {}).get("limit_key"),
                                                 "task_id": session_id})
        await _settle_cancelled_worker(owner, session_id)
    return True


async def _settle_cancelled_worker(owner: str, worker_id: str) -> None:
    """A paused worker whose resume was cancelled will not finish: settle its
    row and tell the parent, the way any failed background worker does."""
    try:
        from app.core.task_registry import task_registry
        from app.core.turn_recorder import record_task_settle_frame

        summary = "Stopped by a usage limit; its automatic resume was cancelled."
        task_registry.complete_task(owner, worker_id, "killed", summary=summary)
        if task_registry.mark_chipped(owner, worker_id, "terminal"):
            await record_task_settle_frame(owner, worker_id, "killed", summary,
                                           tool_use_id=worker_id)
    except Exception:  # noqa: BLE001
        logger.warning("limit_resume: could not settle cancelled worker %s", worker_id,
                       exc_info=True)


def report_note(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """The line a resumed worker's report carries to its parent, or None."""
    if isinstance(payload, dict) and payload.get(PAYLOAD_KEY):
        return WORKER_REPORT_NOTE
    return None
