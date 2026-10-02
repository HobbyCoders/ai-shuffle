"""Subscription loop guard: unattended turns on a Claude plan get a budget.

In subscription mode every Claude agent run spends the user's own plan limits.
A runaway automation, an agent that keeps re-arming wake-ups, a flood of
connector messages or a fan-out of background workers can use a plan up
without anyone watching. The guard adds three things, all configurable in
Settings > Providers > Claude (the subscription's Policy tab):

1. **Unattended budget.** Turns from unattended sources (``automation``,
   ``wakeup``, ``provider_agent`` workers, ``connector``) that run on the
   Claude Code CLI in subscription mode are counted per session and per worker
   over a rolling hour. Past either budget the next such turn does not start:
   it is held, the session shows why, and it waits for the owner to confirm it
   (``POST /api/v1/settings/loop-guard/held/{id}/confirm`` re-submits it) or
   dismiss it. Defaults: 12 per session, 60 per worker, per hour.
2. **Worker time limit.** A background worker on the Claude CLI in
   subscription mode gets a default wall-clock cap (60 minutes) when nothing
   else set one.
3. **Guidance.** Creating an automation or arming a wake-up in subscription
   mode returns "use an API key for scheduled runs".

Nothing here applies in API key mode (the Console spend limit is the control)
or to turns a person started (chat, queue, resume, API, worktree). The counts
live in memory and start again when the worker restarts; held turns are stored
per worker (``loop_guard_held:<instance id>``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

UNATTENDED_SOURCES = ("automation", "wakeup", "provider_agent", "connector")
SOURCE_LABELS = {
    "automation": "automation",
    "wakeup": "scheduled wake-up",
    "provider_agent": "background worker",
    "connector": "connector",
}

WINDOW_SECONDS = 3600.0
MAX_HELD = 50
CONFIRMED_FLAG = "loop_guard_confirmed"
HOLD_CODE = "loop_guard_confirm"

SETTING_ENABLED = "loop_guard_enabled"
SETTING_SESSION_LIMIT = "loop_guard_session_turns_per_hour"
SETTING_INSTALL_LIMIT = "loop_guard_install_turns_per_hour"
SETTING_WORKER_MINUTES = "loop_guard_worker_max_minutes"
HELD_PREFIX = "loop_guard_held:"

DEFAULTS = {
    "enabled": True,
    "session_turns_per_hour": 12,
    "install_turns_per_hour": 60,
    "worker_max_minutes": 60,
}
LIMITS = {
    "session_turns_per_hour": (1, 1000),
    "install_turns_per_hour": (1, 10000),
    "worker_max_minutes": (0, 24 * 60),
}

SCHEDULED_RUN_GUIDANCE = (
    "Claude Code is in subscription mode, so this runs on your Claude plan while "
    "you are away. Anthropic sizes plans for one person's hands-on use; use an "
    "API key for scheduled runs (Settings > Providers > Claude). The "
    "loop guard holds unattended turns past its budget until you confirm them."
)


@dataclass
class LoopGuardConfig:
    enabled: bool = DEFAULTS["enabled"]
    session_turns_per_hour: int = DEFAULTS["session_turns_per_hour"]
    install_turns_per_hour: int = DEFAULTS["install_turns_per_hour"]
    worker_max_minutes: int = DEFAULTS["worker_max_minutes"]


# ─── Settings ───────────────────────────────────────────────────────────────

def _int_setting(key: str, name: str) -> int:
    raw = database.get_system_setting_cached(key)
    lo, hi = LIMITS[name]
    try:
        value = int(raw) if raw not in (None, "") else DEFAULTS[name]
    except (TypeError, ValueError):
        value = DEFAULTS[name]
    return max(lo, min(hi, value))


def config() -> LoopGuardConfig:
    raw = database.get_system_setting_cached(SETTING_ENABLED)
    enabled = DEFAULTS["enabled"] if raw in (None, "") else raw.strip().lower() in ("1", "true", "yes", "on")
    return LoopGuardConfig(
        enabled=enabled,
        session_turns_per_hour=_int_setting(SETTING_SESSION_LIMIT, "session_turns_per_hour"),
        install_turns_per_hour=_int_setting(SETTING_INSTALL_LIMIT, "install_turns_per_hour"),
        worker_max_minutes=_int_setting(SETTING_WORKER_MINUTES, "worker_max_minutes"),
    )


def update_config(**changes: Any) -> LoopGuardConfig:
    keys = {
        "session_turns_per_hour": SETTING_SESSION_LIMIT,
        "install_turns_per_hour": SETTING_INSTALL_LIMIT,
        "worker_max_minutes": SETTING_WORKER_MINUTES,
    }
    for name, value in changes.items():
        if value is None:
            continue
        if name == "enabled":
            database.set_system_setting(SETTING_ENABLED, "true" if value else "false")
        elif name in keys:
            lo, hi = LIMITS[name]
            number = int(value)
            if not lo <= number <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
            database.set_system_setting(keys[name], str(number))
        else:
            raise ValueError(f"Unknown loop guard setting: {name}")
    return config()


def _subscription_mode() -> bool:
    from app.core import claude_auth_mode

    return claude_auth_mode.current_mode() == claude_auth_mode.MODE_SUBSCRIPTION


def applies() -> bool:
    """The guard is on and this worker runs Claude Code on a subscription."""
    try:
        return config().enabled and _subscription_mode()
    except Exception:  # noqa: BLE001 — a settings hiccup must not stop a turn
        logger.warning("Loop guard settings unreadable; not applying it", exc_info=True)
        return False


# ─── Counting ───────────────────────────────────────────────────────────────

_lock = threading.Lock()
_by_session: Dict[str, Deque[float]] = {}
_install: Deque[float] = deque()


def _prune(times: Deque[float], now: float) -> None:
    while times and now - times[0] >= WINDOW_SECONDS:
        times.popleft()


def usage(session_id: Optional[str] = None, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        _prune(_install, now)
        result = {"install_last_hour": len(_install)}
        if session_id is not None:
            times = _by_session.get(session_id)
            if times is not None:
                _prune(times, now)
            result["session_last_hour"] = len(times or ())
        return result


def reset_counts() -> None:
    with _lock:
        _by_session.clear()
        _install.clear()


def check(session_id: str, source: str, leg: Optional[str], *, confirmed: bool = False,
          now: Optional[float] = None) -> Optional[str]:
    """Count an unattended subscription turn, or return why it must wait.

    ``None`` lets the turn start (and counts it when it is unattended). A
    string is the user-facing reason the turn is held instead.
    """
    if source not in UNATTENDED_SOURCES or leg not in (None, "delegated"):
        return None
    if not applies():
        return None
    cfg = config()
    now = time.time() if now is None else now
    with _lock:
        times = _by_session.setdefault(session_id, deque())
        _prune(times, now)
        _prune(_install, now)
        if not confirmed:
            label = SOURCE_LABELS.get(source, source)
            if len(times) >= cfg.session_turns_per_hour:
                return (
                    f"Loop guard: this {label} turn is waiting for your confirmation. "
                    f"This session already ran {len(times)} unattended turns on your Claude "
                    f"subscription in the last hour (budget {cfg.session_turns_per_hour}). "
                    "Confirm or dismiss it in Settings > Providers > Claude > Policy. Use an API key "
                    "for scheduled and unattended runs."
                )
            if len(_install) >= cfg.install_turns_per_hour:
                return (
                    f"Loop guard: this {label} turn is waiting for your confirmation. This "
                    f"worker already ran {len(_install)} unattended turns on your Claude "
                    f"subscription in the last hour (budget {cfg.install_turns_per_hour}). "
                    "Confirm or dismiss it in Settings > Providers > Claude > Policy. Use an API key "
                    "for scheduled and unattended runs."
                )
        times.append(now)
        _install.append(now)
    return None


def worker_max_seconds(provider_id: Optional[str]) -> float:
    """Default wall-clock cap for a worker on the Claude CLI in subscription
    mode (0 = none). Workers bound to another provider are not capped here."""
    if provider_id or not applies():
        return 0.0
    try:
        return float(config().worker_max_minutes) * 60.0
    except Exception:  # noqa: BLE001
        return 0.0


def scheduled_run_guidance(profile: Optional[dict] = None) -> Optional[str]:
    """The creation-time reminder, or None when it does not apply (API key
    mode, or a profile bound to a non-Claude provider)."""
    try:
        if profile and (profile.get("config") or {}).get("provider"):
            return None
        return SCHEDULED_RUN_GUIDANCE if _subscription_mode() else None
    except Exception:  # noqa: BLE001
        return None


# ─── Held turns ─────────────────────────────────────────────────────────────

def _held_key() -> str:
    return HELD_PREFIX + settings.effective_instance_id


def list_held() -> List[dict]:
    raw = database.get_system_setting(_held_key())
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _save_held(items: List[dict]) -> None:
    database.set_system_setting(_held_key(), json.dumps(items[-MAX_HELD:]))


def hold(turn: dict, reason: str) -> dict:
    """Keep a refused turn so the owner can confirm (re-submit) it later."""
    payload = dict(turn.get("payload") or {})
    payload.pop(CONFIRMED_FLAG, None)
    item = {
        "id": uuid.uuid4().hex,
        "session_id": turn.get("session_id"),
        "turn_id": turn.get("id"),
        "source": turn.get("source"),
        "prompt": turn.get("prompt"),
        "profile_id": turn.get("profile_id"),
        "project_id": turn.get("project_id"),
        "overrides": turn.get("overrides") or None,
        "payload": payload or None,
        "reason": reason,
        "held_at": time.time(),
    }
    with _lock:
        items = list_held()
        items.append(item)
        _save_held(items)
    logger.warning("Loop guard held a %s turn for session %s", item["source"], item["session_id"])
    return item


def take_held(held_id: str) -> Optional[dict]:
    with _lock:
        items = list_held()
        found = next((i for i in items if i.get("id") == held_id), None)
        if found is not None:
            _save_held([i for i in items if i.get("id") != held_id])
        return found


async def confirm_held(held_id: str) -> Optional[str]:
    """Re-submit a held turn past the budget. Returns the new turn id."""
    import asyncio

    from app.core import turn_manager

    item = await asyncio.to_thread(take_held, held_id)
    if item is None:
        return None
    payload = dict(item.get("payload") or {})
    payload[CONFIRMED_FLAG] = True
    return await turn_manager.submit_async(
        item["session_id"],
        item.get("source") or "automation",
        prompt=item.get("prompt"),
        profile_id=item.get("profile_id"),
        project_id=item.get("project_id"),
        overrides=item.get("overrides"),
        payload=payload,
    )


def status_payload() -> dict:
    cfg = config()
    return {
        **asdict(cfg),
        "defaults": dict(DEFAULTS),
        "applies": cfg.enabled and _subscription_mode(),
        "unattended_sources": list(UNATTENDED_SOURCES),
        "usage": usage(),
        "held": list_held(),
    }
