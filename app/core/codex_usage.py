"""Contract for the ChatGPT/Codex subscription usage endpoint.

This module is an isolated contract boundary, matching
``app.core.codex_rate_limit_resets``. It owns the endpoint URL, the request
headers and the response shape; the budgeted request itself is issued by
``app.core.provider_usage`` so the usage-source registry can meter it. Upstream
bodies and headers never appear in errors, and account identity is never
stored.

The endpoint reports the same quota windows the ``x-codex-*`` response headers
carry, but without needing an inference request: it is what keeps the usage
panel honest for an account that spent its allowance somewhere else (the
ChatGPT web client, the Codex CLI, another worker) or whose window rolled over
while the app sat idle. Passive header capture stays in place and refreshes the
same snapshot for free between polls.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.core.codex_rate_limit_resets import CODEX_USER_AGENT

USAGE_HOST = "chatgpt.com"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_TIMEOUT = 10.0

#: Fields kept from the upstream body. Account identity (``user_id``,
#: ``account_id``, ``email``) and marketing copy (``promo``) are never stored.
_RAW_FIELDS = (
    "source",
    "plan_type",
    "limit_reached",
    "rate_limit_reached_type",
    "spend_control_reached",
    "reset_credits_available",
)


class CodexUsageError(Exception):
    """Credential-free failure at the usage contract boundary."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


@dataclass(frozen=True)
class CodexUsage:
    """Normalized subscription usage, shaped like a header capture."""

    windows: List[Dict[str, Any]]
    credits: Optional[Dict[str, Any]]
    plan_type: Optional[str]
    raw: Dict[str, Any]


def window_type_for_minutes(minutes: Optional[int]) -> Optional[str]:
    """Map a rate-limit window's minutes to a stable window-type key."""
    if minutes is None or minutes <= 0:
        return None
    if minutes == 10080:
        return "seven_day"
    if minutes == 300:
        return "five_hour"
    return f"window_{minutes}m"


def usage_headers(access_token: str, account_id: Optional[str]) -> Dict[str, str]:
    """Request headers for one usage read. Never logged."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": CODEX_USER_AGENT,
        "Accept": "application/json",
    }
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the bearer token must never leave chatgpt.com."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == USAGE_HOST


def _number(value: Any) -> Optional[float]:
    """Finite number from an int, float or numeric string; else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> Optional[int]:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _window(payload: Any, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One normalized window, or ``None`` when the shape is not usable.

    A window needs both a used percentage and a window length; without the
    length there is no telling a weekly allowance from a five-hour one.
    """
    if not isinstance(payload, dict):
        return None
    used = _number(payload.get("used_percent"))
    seconds = _int(payload.get("limit_window_seconds"))
    if used is None or seconds is None or seconds <= 0:
        return None
    window_type = window_type_for_minutes(seconds // 60)
    if window_type is None:
        return None
    resets_at = _int(payload.get("reset_at"))
    if resets_at is None:
        after = _number(payload.get("reset_after_seconds"))
        if after is not None:
            resets_at = int((time.time() if now is None else now) + after)
    return {
        "type": window_type,
        # Clamped: the panel draws this as a bar width and subtracts it from
        # 100 for "% left".
        "utilization": max(0.0, min(100.0, used)),
        "resets_at": str(resets_at) if resets_at is not None else None,
    }


def normalize_usage(payload: Any, *, now: Optional[float] = None) -> CodexUsage:
    """Validate and normalize the usage contract. Pure.

    Raises ``CodexUsageError('invalid_contract')`` when the body is not an
    object. Individual fields degrade to ``None`` rather than failing the whole
    reading: an absent secondary window is normal on some plans, and a reading
    with no usable window at all is reported as such so the caller can keep the
    previous one instead of erasing it.
    """
    if not isinstance(payload, dict):
        raise CodexUsageError("invalid_contract")

    rate_limit = payload.get("rate_limit")
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    windows: List[Dict[str, Any]] = []
    # primary is the long window (weekly), secondary the short one (5h) —
    # the same ordering the x-codex-* headers use.
    for key in ("primary_window", "secondary_window"):
        window = _window(rate_limit.get(key), now=now)
        if window is not None:
            windows.append(window)

    raw_credits = payload.get("credits")
    raw_credits = raw_credits if isinstance(raw_credits, dict) else {}
    credits = {
        "has_credits": _bool(raw_credits.get("has_credits")),
        "balance": _number(raw_credits.get("balance")),
        "unlimited": _bool(raw_credits.get("unlimited")),
    }

    plan_type = payload.get("plan_type")
    plan_type = plan_type if isinstance(plan_type, str) and plan_type else None

    reset_credits = payload.get("rate_limit_reset_credits")
    reset_credits = reset_credits if isinstance(reset_credits, dict) else {}
    spend_control = payload.get("spend_control")
    spend_control = spend_control if isinstance(spend_control, dict) else {}
    reached = payload.get("rate_limit_reached_type")
    raw = {
        "source": "codex_usage",
        "plan_type": plan_type,
        "limit_reached": _bool(rate_limit.get("limit_reached")),
        "rate_limit_reached_type": reached if isinstance(reached, str) else None,
        "spend_control_reached": _bool(spend_control.get("reached")),
        "reset_credits_available": _int(reset_credits.get("available_count")),
    }
    return CodexUsage(
        windows=windows,
        credits=credits,
        plan_type=plan_type,
        raw={k: v for k, v in raw.items() if k in _RAW_FIELDS and v is not None},
    )
