"""Claude plan usage reported by AI Shuffle's Claude Code mod.

The mod (``claude_mod/hooks/usage.ts``) hooks ``session.measure``, which the
CLI fires after each main-thread turn and when a rate-limit window moves a
whole point. It posts the rate-limit windows it was handed, and nothing else,
to the session's loopback bridge (``serve_registry_socket``'s HTTP route)
with a per-session token. This module is the contract for that post: the
path, the size cap and ``parse_plan_usage_report``, which turns a body into a
bounded :class:`PlanUsageReport` or raises ``ValueError``.

Body, version 1::

    {"v": 1, "measuredAt": <epoch ms>,
     "rateLimits": [{"kind": "five_hour", "percentUsed": 23.5,
                     "resetsAt": "2026-10-03T18:00:00Z"}, ...]}

``percentUsed`` is quota used, 0 to 100 (past 100 on an exceeded spend
limit), as the CLI's own ``SessionRateLimit`` has it. Unknown keys are
ignored and never kept.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: The bridge route the mod posts to.
USAGE_PATH = "/claude-mod/usage"
#: Largest body the route reads. A report of eight windows is under 1 KiB.
MAX_BODY_BYTES = 8 * 1024
#: Most windows one report may carry (the CLI has three kinds today).
MAX_WINDOWS = 8
#: A reading measured longer ago than this is not news any more.
MAX_AGE_SECONDS = 10 * 60
#: Clock skew allowed for a reading stamped ahead of this process's clock.
MAX_FUTURE_SECONDS = 60
#: Highest ``percentUsed`` accepted (a spend limit can pass 100).
MAX_PERCENT_USED = 1000.0
#: A reset further out than this (or before the epoch) is not a reset time.
MAX_RESET_AHEAD_SECONDS = 400 * 24 * 3600

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass(frozen=True)
class PlanWindow:
    """One rate-limit window: its kind, the share used and when it resets."""

    kind: str
    #: Quota used, 0 to ``MAX_PERCENT_USED``, rounded to one decimal.
    percent_used: float
    #: When the window resets, epoch seconds; None when the CLI gave none.
    resets_at: int | None


@dataclass(frozen=True)
class PlanUsageReport:
    """A validated report: when the CLI measured it and the windows it had."""

    #: Epoch seconds, never ahead of the receiving process's clock.
    measured_at: float
    windows: tuple[PlanWindow, ...]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _reset_seconds(value: Any, now: float) -> int | None:
    """``resetsAt`` (ISO 8601, or epoch seconds or ms) as epoch seconds."""
    if value is None:
        return None
    number = _number(value)
    if number is not None:
        stamp = number / 1000.0 if number >= 1e12 else number
    elif isinstance(value, str) and 0 < len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("resetsAt is not a timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    else:
        raise ValueError("resetsAt is not a timestamp")
    if stamp <= 0 or stamp > now + MAX_RESET_AHEAD_SECONDS:
        raise ValueError("resetsAt is out of range")
    return int(round(stamp))


def parse_plan_usage_report(body: Any, *, now: float) -> PlanUsageReport:
    """Validate a decoded report body against ``now`` (epoch seconds).

    Raises ``ValueError`` for anything that is not a version 1 report with
    one to ``MAX_WINDOWS`` well-formed windows, distinct kinds, and a
    ``measuredAt`` within ``MAX_AGE_SECONDS`` of now.
    """
    if not isinstance(body, dict):
        raise ValueError("report is not an object")
    if body.get("v") != 1:
        raise ValueError("unknown report version")
    measured_ms = _number(body.get("measuredAt"))
    if measured_ms is None:
        raise ValueError("measuredAt is missing")
    measured_at = measured_ms / 1000.0
    if measured_at > now + MAX_FUTURE_SECONDS:
        raise ValueError("measuredAt is in the future")
    if measured_at < now - MAX_AGE_SECONDS:
        raise ValueError("measuredAt is too old")
    raw = body.get("rateLimits")
    if not isinstance(raw, list) or not raw:
        raise ValueError("rateLimits is missing or empty")
    if len(raw) > MAX_WINDOWS:
        raise ValueError("too many windows")
    windows: list[PlanWindow] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("a window is not an object")
        kind = item.get("kind")
        if not isinstance(kind, str) or not _KIND_RE.match(kind):
            raise ValueError("a window has no valid kind")
        if any(w.kind == kind for w in windows):
            raise ValueError("a window kind repeats")
        used = _number(item.get("percentUsed"))
        if used is None or used < 0 or used > MAX_PERCENT_USED:
            raise ValueError("a window has no valid percentUsed")
        windows.append(PlanWindow(
            kind=kind,
            percent_used=round(used, 1),
            resets_at=_reset_seconds(item.get("resetsAt"), now),
        ))
    return PlanUsageReport(measured_at=min(measured_at, now), windows=tuple(windows))
