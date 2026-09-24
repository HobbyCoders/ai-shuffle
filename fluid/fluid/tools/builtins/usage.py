"""Read the host's last observed subscription/provider usage snapshot."""

from __future__ import annotations

import inspect
import json
import time
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


def _percent(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(max(0.0, min(100.0, value)), 2)


def _window(raw: Any, *, source: str, remaining_input: bool) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = _percent(raw.get("utilization"))
    if value is None:
        return None
    remaining = value if remaining_input else 100.0 - value
    return {
        "type": str(raw.get("type") or "unknown"),
        "used_percent": round(100.0 - remaining, 2),
        "remaining_percent": round(remaining, 2),
        "resets_at": raw.get("resets_at"),
        "source": source,
    }


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
            "freshness": _freshness(account_fetched_at, available=bool(account.get("available"))),
            "fetched_at": account_fetched_at,
            "windows": [
                item for value in account.get("windows") or ()
                if (item := _window(value, source="claude_subscription", remaining_input=True))
            ],
        })
    claude_available = claude_available or any(
        account.get("available") is True for account in accounts
    )
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
                for key in ("unlimited", "balance_remaining", "limit_remaining", "balance", "usage", "total_available", "spend_month")
                if key in credits
            }
        providers.append(entry)

    fetched_at = raw.get("fetched_at")
    provider_available = any(
        entry.get("status") in {"fresh", "stale"}
        or bool(entry.get("windows"))
        or bool(entry.get("credits"))
        for entry in providers
    )
    available = claude_available or provider_available
    claude_freshness = _freshness(fetched_at, available=claude_available)
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
    if not available and not windows:
        result["reason"] = "no cached subscription usage is available"
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
