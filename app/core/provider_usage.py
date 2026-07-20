"""Provider usage capture + polling (Chunks U2/U3 of the provider-usage program).

Two data paths land in this module, per ``docs/PROVIDER_USAGE_CONTEXT_PLAN.md``:

**Codex passive capture (U2).** The translator gateway's Codex upstream leg
(``upstream.py::stream_responses_events``) receives ``x-codex-*``
plan/rate-limit headers on every 200 response. ``capture_codex_headers``
normalizes those headers into the shared ``provider_usage_snapshots`` row
shape and persists them fire-and-forget, off the streaming hot path.

**OpenRouter poller (U3).** ``ProviderUsageMonitor`` polls every enabled
``openrouter`` model-provider's key-info endpoint
(``GET {origin of base_url}/api/v1/key``) on the same 5-minute cadence as the
Claude usage-limits poller (``app.core.usage_limits``). Window/credits mapping
is governed by AMENDMENT 1 of the plan doc: when the key's ``limit`` is
non-null and positive, emit exactly ONE window —
``{type: "credit_limit", utilization: usage / limit * 100}``. Daily/weekly/
monthly spend never drives a window's utilization; it is always carried as
dollar figures in ``credits`` (populated in both branches).

Security (S4 hard invariant): the allowlists below are the ONLY thing that
ever reaches ``raw`` (and, transitively, the DB and any future API response).
They never include ``Authorization``, ``chatgpt-account-id``, any cookie, or
any other credential. The decrypted OpenRouter key is used only as a Bearer
header value and is NEVER logged, printed, or stored (not even truncated).
Failures are logged at WARNING without ever including header or key values.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.translator_gateway.upstream import _decrypt_provider_auth_token
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Codex passive capture (U2)
# ---------------------------------------------------------------------------

# Header allowlist — ONLY these travel into `raw`; nothing else, never a token.
_CODEX_HEADER_KEYS = (
    "x-codex-plan-type",
    "x-codex-active-limit",
    "x-codex-primary-used-percent",
    "x-codex-primary-window-minutes",
    "x-codex-primary-reset-after-seconds",
    "x-codex-primary-reset-at",
    "x-codex-secondary-used-percent",
    "x-codex-secondary-window-minutes",
    "x-codex-secondary-reset-after-seconds",
    "x-codex-secondary-reset-at",
    "x-codex-primary-over-secondary-limit-percent",
    "x-codex-credits-has-credits",
    "x-codex-credits-balance",
    "x-codex-credits-unlimited",
)


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized result of a Codex header capture."""

    windows: List[Dict[str, Any]]
    credits: Optional[Dict[str, Any]]
    plan_type: Optional[str]
    raw: Dict[str, Any]


def _as_float(value: Any) -> Optional[float]:
    """Best-effort numeric coercion; ``None`` for missing/unparseable values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


def _window_type_for_minutes(minutes: Optional[int]) -> Optional[str]:
    """Map a rate-limit window's minutes to a stable window-type key."""
    if minutes is None:
        return None
    if minutes == 10080:
        return "seven_day"
    if minutes == 300:
        return "five_hour"
    return f"window_{minutes}m"


def _resets_at(
    reset_at: Optional[str], reset_after_s: Optional[str]
) -> Optional[str]:
    """Prefer an absolute reset-at; else derive epoch-seconds from reset-after."""
    if reset_at:
        return str(reset_at)
    if reset_after_s:
        try:
            return str(int(time.time()) + int(float(reset_after_s)))
        except (TypeError, ValueError):
            return None
    return None


def normalize_codex_headers(headers: Mapping[str, str]) -> ProviderUsage:
    """Normalize Codex ``x-codex-*`` response headers. Pure; never raises.

    ``raw`` contains only the allowlisted header names/values that were
    actually present — never a token, never an unlisted header.
    """
    h = {k: headers.get(k) for k in _CODEX_HEADER_KEYS if headers.get(k) is not None}
    windows: List[Dict[str, Any]] = []
    for prefix in ("primary", "secondary"):  # primary=weekly, secondary=5h
        pct = _as_float(h.get(f"x-codex-{prefix}-used-percent"))
        mins = _as_int(h.get(f"x-codex-{prefix}-window-minutes"))
        wtype = _window_type_for_minutes(mins)
        if wtype is None or pct is None:
            continue
        windows.append(
            {
                "type": wtype,
                "utilization": pct,  # already 0-100
                "resets_at": _resets_at(
                    h.get(f"x-codex-{prefix}-reset-at"),
                    h.get(f"x-codex-{prefix}-reset-after-seconds"),
                ),
            }
        )
    credits = {
        "has_credits": _as_bool(h.get("x-codex-credits-has-credits")),
        "balance": _as_float(h.get("x-codex-credits-balance")),
        "unlimited": _as_bool(h.get("x-codex-credits-unlimited")),
    }
    plan_type = h.get("x-codex-plan-type")
    return ProviderUsage(windows=windows, credits=credits, plan_type=plan_type, raw=h)


# Fire-and-forget capture tasks are retained here until they complete so the
# event loop doesn't garbage-collect a still-pending task ("Task was destroyed
# but it is pending!") — mirrors app.core.usage_limits._pending_refresh_tasks.
_pending_capture_tasks: "set[asyncio.Task]" = set()


def capture_codex_headers(provider_id: str, headers: Mapping[str, str]) -> None:
    """Snapshot allowlisted headers synchronously, then persist off the loop.

    Never raises, never blocks the caller. No-op if none of the allowlisted
    ``x-codex-*`` headers are present on this response.
    """
    try:
        snap = {k: headers[k] for k in _CODEX_HEADER_KEYS if k in headers}
        if not snap:
            return
        task = asyncio.create_task(_persist_codex_capture(provider_id, snap))
    except RuntimeError:
        return  # no running loop (sync test context)
    except Exception:  # noqa: BLE001 — capture must never break the caller
        return
    _pending_capture_tasks.add(task)
    task.add_done_callback(_pending_capture_tasks.discard)


async def _persist_codex_capture(provider_id: str, headers: Dict[str, str]) -> None:
    """Normalize + upsert a captured Codex header snapshot. Best-effort."""
    try:
        u = normalize_codex_headers(headers)
        from app.db import database as db

        await run_sync(
            db.upsert_provider_usage_snapshot,
            provider_id,
            "chatgpt",
            u.plan_type,
            u.windows,
            u.credits,
            u.raw,
            int(time.time() * 1000),
        )
    except Exception as e:  # noqa: BLE001 — capture must never break a turn
        # Never log header values (S4) — only the exception's own message.
        logger.warning("Codex usage capture failed for provider %s: %s", provider_id, e)


# ---------------------------------------------------------------------------
# OpenRouter poller (U3)
# ---------------------------------------------------------------------------

# Parity with app.core.usage_limits._periodic_usage_limits's cadence.
_POLL_INTERVAL_SECONDS = 300
_KEY_PATH = "/api/v1/key"
_REQUEST_TIMEOUT_SECONDS = 20.0

# Allowlist for `raw` / `credits` sourced from the OpenRouter /key response.
# Deliberately excludes anything credential-shaped (there is no such field in
# this response, but the allowlist is the enforcement point regardless).
_OPENROUTER_KEY_FIELDS: Tuple[str, ...] = (
    "label",
    "limit",
    "limit_remaining",
    "limit_reset",
    "usage",
    "usage_daily",
    "usage_weekly",
    "usage_monthly",
    "is_free_tier",
)


def _origin(base_url: str) -> str:
    """Return the scheme+netloc of a provider's configured ``base_url``."""
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _openrouter_windows_and_credits(
    data: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Normalise an OpenRouter ``/key`` payload into (windows, credits).

    AMENDMENT 1 (binding): exactly one ``credit_limit`` window when ``limit``
    is non-null and positive; empty windows when it is null. Dollar figures
    always populate ``credits`` in both branches.
    """
    limit = _as_float(data.get("limit"))
    credits: Dict[str, Any] = {
        "usage": _as_float(data.get("usage")),
        "usage_daily": _as_float(data.get("usage_daily")),
        "usage_weekly": _as_float(data.get("usage_weekly")),
        "usage_monthly": _as_float(data.get("usage_monthly")),
        "limit": limit,
        "limit_remaining": _as_float(data.get("limit_remaining")),
        "is_free_tier": bool(data.get("is_free_tier")),
    }

    windows: List[Dict[str, Any]] = []
    if limit is not None and limit > 0:
        used = _as_float(data.get("usage"))
        if used is not None:
            windows.append(
                {
                    "type": "credit_limit",
                    "utilization": max(0.0, min(100.0, used / limit * 100.0)),
                    # /key's `limit_reset` is a cadence descriptor ("monthly"),
                    # not a timestamp, so there is no `resets_at` to surface.
                    "resets_at": None,
                }
            )
    return windows, credits


class ProviderUsageMonitor:
    """Polls every enabled OpenRouter provider's ``/api/v1/key`` endpoint.

    Best-effort, module-singleton, lifecycle mirrors
    ``app.core.usage_limits.UsageLimitsMonitor``: ``poll_once`` never raises,
    and one provider's failure (decrypt error, timeout, non-200) never blocks
    another provider's poll or breaks the loop.
    """

    async def poll_once(self) -> None:
        """Poll every enabled ``openrouter`` provider row. Never raises."""
        from app.db import database as db

        try:
            # AMENDMENT 2 (binding): include_token passed as an explicit
            # keyword via functools.partial, never positional, so a future
            # signature change on get_enabled_model_providers can't silently
            # flip a token-bearing read.
            rows = await run_sync(
                functools.partial(db.get_enabled_model_providers, include_token=True)
            )
        except Exception as e:
            logger.debug("Provider usage poll: provider read skipped: %s", e)
            return

        for row in rows or []:
            if (row or {}).get("provider_type") != "openrouter":
                continue
            await self._poll_provider(row)

    async def _poll_provider(self, row: Dict[str, Any]) -> None:
        from app.db import database as db

        provider_id = row.get("id")
        try:
            key = _decrypt_provider_auth_token(row)
            base_url = row.get("base_url") or ""
            if not key or not base_url:
                return

            url = _origin(base_url) + _KEY_PATH
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})

            if resp.status_code != 200:
                logger.warning(
                    "OpenRouter /key fetch for provider %s failed: HTTP %s",
                    provider_id,
                    resp.status_code,
                )
                return

            body = resp.json() or {}
            # The /key response wraps its fields under a top-level `data`
            # object; tolerate both wrapped and flat shapes.
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                data = body if isinstance(body, dict) else {}

            windows, credits = _openrouter_windows_and_credits(data)
            raw = {k: data.get(k) for k in _OPENROUTER_KEY_FIELDS if k in data}

            await run_sync(
                db.upsert_provider_usage_snapshot,
                provider_id,
                "openrouter",
                None,
                windows,
                credits,
                raw,
                int(time.time() * 1000),
            )
        except Exception as e:
            # Never include `key` / the Authorization header value here.
            logger.warning(
                "OpenRouter usage poll for provider %s failed: %s", provider_id, e
            )


provider_usage_monitor = ProviderUsageMonitor()
