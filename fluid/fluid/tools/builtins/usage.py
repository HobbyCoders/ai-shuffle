"""Read the host's last observed subscription/provider usage snapshot."""

from __future__ import annotations

import inspect
import json
import time
from datetime import datetime, timezone
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec


_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

SPEC = ToolSpec(
    name="GetUsage",
    description=(
        "Read the latest cached subscription and provider usage observed by "
        "the host. Reports used and remaining percentages, reset timestamps, "
        "source, freshness, and unavailable data explicitly. This is read-only "
        "and never refreshes a provider or reads credentials. Session token "
        "usage, when supplied by the host, is reported separately."
    ),
    input_schema=_SCHEMA,
    risk="safe",
)


# The last provider rows this process read successfully. A host reports
# ``providers: null`` when it could not read its provider rows (as opposed to
# ``[]``, no providers), and the tool then reports these instead of "none".
_last_providers: list[dict[str, Any]] | None = None


def _percent(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(max(0.0, min(100.0, value)), 2)


def _reset_passed(resets_at: Any) -> bool:
    """True when ``resets_at`` (epoch s, epoch ms or ISO 8601) is in the past."""
    if resets_at is None or isinstance(resets_at, bool):
        return False
    try:
        stamp = float(resets_at)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(resets_at).strip().replace("Z", "+00:00"))
        except ValueError:
            return False
        # Host timestamps are UTC; a zone-less one must not read as local time.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    else:
        if stamp >= 1e12:
            stamp /= 1000.0
    return stamp <= time.time()


def _window(raw: Any, *, source: str, remaining_input: bool) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = _percent(raw.get("utilization"))
    if value is None:
        return None
    remaining = value if remaining_input else 100.0 - value
    rolled_over = _reset_passed(raw.get("resets_at"))
    if rolled_over:
        remaining = 100.0
    window = {
        "type": str(raw.get("type") or "unknown"),
        "used_percent": round(100.0 - remaining, 2),
        "remaining_percent": round(remaining, 2),
        "resets_at": raw.get("resets_at"),
        "source": source,
    }
    if rolled_over:
        window["rolled_over"] = True
    observed_at = _epoch_seconds(raw.get("observed_at"))
    if observed_at is not None:
        window["observed_at"] = raw.get("observed_at")
    return window


def _epoch_seconds(value: Any) -> float | None:
    """Epoch seconds from an epoch-seconds or epoch-ms stamp, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp != stamp or stamp <= 0 or stamp in (float("inf"), float("-inf")):
        return None
    # App snapshots use seconds for Claude and milliseconds for provider rows
    # and per-window observations.
    return stamp / 1000.0 if stamp > 10_000_000_000 else stamp


def _oldest_observation(fetched_at: Any, windows: Any) -> Any:
    """When the least recently reported Claude window was observed.

    One CLI event dates one window, so a snapshot's ``fetched_at`` (moved by
    any window) overstates the others' freshness. The usage panel dates the
    section by its oldest window; the tool does the same. Windows without
    their own ``observed_at`` fall back to ``fetched_at``.
    """
    fallback = _epoch_seconds(fetched_at)
    stamps = [
        observed if (observed := _epoch_seconds(w.get("observed_at"))) is not None else fallback
        for w in windows or () if isinstance(w, dict)
    ]
    stamps = [s for s in stamps if s is not None]
    return min(stamps) if stamps else fetched_at


def _freshness(fetched_at: Any, *, available: bool = True) -> str:
    if not available:
        return "unavailable"
    if not fetched_at:
        return "unknown"
    try:
        stamp = float(fetched_at)
        # App snapshots use seconds for Claude and milliseconds for provider
        # rows. Normalize only for this status calculation.
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return "stale" if time.time() - stamp > 300 else "fresh"
    except (TypeError, ValueError):
        return "unknown"


def _payload(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, dict):
        raise TypeError("usage snapshot callback returned an invalid value")

    claude_available = bool(raw.get("available"))
    windows = [
        item for value in raw.get("windows") or ()
        if (item := _window(value, source="claude_subscription", remaining_input=True))
    ]
    accounts: list[dict[str, Any]] = []
    for account in raw.get("snapshots") or ():
        if not isinstance(account, dict):
            continue
        account_fetched_at = account.get("fetched_at")
        accounts.append({
            "instance_id": account.get("instance_id"),
            "device_name": account.get("device_name"),
            "available": bool(account.get("available")),
            "source": "cached_host_snapshot",
            "freshness": _freshness(
                _oldest_observation(account_fetched_at, account.get("windows")),
                available=bool(account.get("available")),
            ),
            "fetched_at": account_fetched_at,
            "windows": [
                item for value in account.get("windows") or ()
                if (item := _window(value, source="claude_subscription", remaining_input=True))
            ],
        })
    claude_available = claude_available or any(
        account.get("available") is True for account in accounts
    )
    global _last_providers
    providers_unread = "providers" in raw and raw["providers"] is None
    providers: list[dict[str, Any]] = []
    for provider in raw.get("providers") or ():
        if not isinstance(provider, dict):
            continue
        fetched_at = provider.get("fetched_at")
        status = provider.get("status") or _freshness(fetched_at)
        entry: dict[str, Any] = {
            "provider_id": provider.get("provider_id"),
            "provider_name": provider.get("provider_name"),
            "provider_type": provider.get("provider_type"),
            "plan_type": provider.get("plan_type"),
            "source": "cached_provider_snapshot",
            "status": status,
            "freshness": status if status in {"fresh", "stale", "unavailable", "unknown"}
            else _freshness(fetched_at),
            "fetched_at": fetched_at,
            "windows": [
                item for value in provider.get("windows") or ()
                if (item := _window(value, source="provider_snapshot", remaining_input=False))
            ],
        }
        # Credits are already a redacted API model; do not expose provider raw
        # blobs or any credential-shaped data if a host callback is extended.
        credits = provider.get("credits")
        if isinstance(credits, dict):
            entry["credits"] = {
                key: credits[key]
                for key in ("unlimited", "balance_remaining", "limit_remaining", "limit", "balance", "usage",
                            "total_available", "total_credits", "total_usage", "spend_month", "currency")
                if key in credits
            }
        providers.append(entry)
    if providers_unread:
        # Replayed rows are what was read last time, not what the host holds
        # now: never label them fresh (their fetched_at still says when).
        providers = []
        for cached in _last_providers or ():
            entry = dict(cached)
            entry["source"] = "last_known_provider_snapshot"
            entry["last_known"] = True
            if entry.get("freshness") == "fresh":
                entry["freshness"] = "stale"
            if entry.get("status") == "fresh":
                entry["status"] = "stale"
            providers.append(entry)
    else:
        _last_providers = [dict(entry) for entry in providers]

    fetched_at = raw.get("fetched_at")
    provider_available = any(
        entry.get("status") in {"fresh", "stale"}
        or bool(entry.get("windows"))
        or bool(entry.get("credits"))
        for entry in providers
    )
    available = claude_available or provider_available
    claude_freshness = _freshness(
        _oldest_observation(fetched_at, raw.get("windows")), available=claude_available,
    )
    provider_freshness = {entry["freshness"] for entry in providers}
    aggregate_freshness = (
        claude_freshness
        if claude_available and claude_freshness != "unavailable"
        else "fresh" if "fresh" in provider_freshness
        else "stale" if "stale" in provider_freshness
        else "unknown" if available
        else "unavailable"
    )
    result: dict[str, Any] = {
        "available": available,
        "claude_available": claude_available,
        "source": "cached_host_snapshot",
        "freshness": aggregate_freshness,
        "claude_freshness": claude_freshness,
        "fetched_at": fetched_at,
        "provider_refreshing": bool(raw.get("provider_refreshing")),
        "windows": windows,
        "accounts": accounts,
        "providers": providers,
        "session_usage": raw.get("session_usage") if isinstance(raw.get("session_usage"), dict) else {
            "available": False,
            "reason": "host did not provide session usage in this snapshot",
        },
    }
    if providers_unread:
        result["providers_unavailable"] = True
        result["providers_reason"] = (
            "provider usage rows are temporarily unavailable (the host could "
            "not read them); "
            + ("these are the last values read" if providers
               else "this does not mean no providers are configured")
        )
    if not available and not windows:
        result["reason"] = (
            "provider usage rows are temporarily unavailable and no cached "
            "subscription usage is available" if providers_unread
            else "no cached subscription usage is available"
        )
    return result


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    callback = (ctx.extra or {}).get("usage_snapshot")
    if callback is None:
        return ToolOutcome(text=json.dumps({
            "available": False,
            "claude_available": False,
            "source": "standalone",
            "freshness": "unavailable",
            "windows": [],
            "accounts": [],
            "providers": [],
            "session_usage": {"available": False},
            "reason": "no usage snapshot host is configured",
        }, separators=(",", ":")))
    try:
        raw = callback()
        if inspect.isawaitable(raw):
            raw = await raw
        return ToolOutcome(text=json.dumps(_payload(raw), separators=(",", ":")))
    except Exception:
        return ToolOutcome(text=json.dumps({
            "available": False,
            "source": "host_snapshot",
            "freshness": "unavailable",
            "windows": [],
            "accounts": [],
            "providers": [],
            "session_usage": {"available": False},
            "reason": "usage snapshot is unavailable",
        }, separators=(",", ":")), is_error=True)
