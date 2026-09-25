"""Passive inference usage capture and demand-driven account usage refreshes.

Every source read here is defined in :mod:`app.core.usage_sources`; the
poller takes its jobs from that registry and every upstream read goes through
:func:`_registered_get`, which refuses endpoints the registry does not list.
OpenAI account spend and completions usage use a separate local Admin API key.
No dashboard endpoints, browser sign-ins or ChatGPT OAuth credentials are used
for OpenAI Platform billing. Upstream reads share durable cooldowns; UI refresh
requests cannot bypass those budgets. Raw snapshots never contain credentials.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core import encryption, usage_sources
from app.core.usage_polling import UsageDeferred, capability_fingerprint, usage_get
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Poll-failure logging
# ---------------------------------------------------------------------------

#: How long a still-failing poll stays quiet (debug) before it is re-asserted
#: at ERROR. One hour: long enough that a permanently-broken key writes ~24
#: rows a day rather than ~288, short enough that a fresh look at the error log
#: still shows an outage that started yesterday.
_POLL_FAILURE_REASSERT_SECONDS = 3600.0

#: key -> (last message, monotonic timestamp of the last ERROR emission).
_poll_failure_state: Dict[str, Tuple[str, float]] = {}


def _log_poll_failure(key: str, message: str, *args: Any) -> None:
    """Log a provider-poll failure at ERROR, de-duplicated over time.

    ``key`` identifies the failing thing (``"openai-credits:<provider_id>"``),
    NOT the failure text: a key whose 403 becomes a 500 has changed state and
    is re-asserted immediately. While the same failure repeats it drops to
    debug, and it returns to ERROR once
    :data:`_POLL_FAILURE_REASSERT_SECONDS` have passed.

    Never include a credential in ``message``/``args``. Never raises.
    """
    try:
        rendered = message % args if args else message
    except Exception:  # noqa: BLE001 — bad format string must not break a poll
        rendered = message
    now = time.monotonic()
    previous = _poll_failure_state.get(key)
    repeat = (
        previous is not None
        and previous[0] == rendered
        and (now - previous[1]) < _POLL_FAILURE_REASSERT_SECONDS
    )
    if repeat:
        logger.debug("%s (unchanged since last report)", rendered)
        return
    _poll_failure_state[key] = (rendered, now)
    logger.error("%s", rendered)


def _clear_poll_failure(key: str) -> None:
    """Forget ``key``'s failure state so the next one is reported immediately."""
    _poll_failure_state.pop(key, None)


def _decrypt_provider_auth_token(provider: Optional[Dict[str, Any]]) -> Optional[str]:
    """Decrypt a provider row's ``auth_token`` blob (or pass plaintext through).

    Never log the return value.
    """
    raw = (provider or {}).get("auth_token")
    if not raw:
        return None
    if encryption.is_encrypted(raw):
        return encryption.decrypt_value(raw)
    return raw  # plaintext (migration compatibility)


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
    if minutes is None or minutes <= 0:
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

        # Poll recovered: forget the suppressed-failure state so the
        # next failure is reported at ERROR straight away.
        _clear_poll_failure(f"codex-capture:{provider_id}")
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
        # Never log header values — only the exception's own message.
        _log_poll_failure(
            f"codex-capture:{provider_id}",
            "Codex usage capture failed for provider %s: %s", provider_id, e,
        )


# ---------------------------------------------------------------------------
# OpenAI API-key passive capture
# ---------------------------------------------------------------------------

# Header allowlist — ONLY these travel into `raw`; nothing else, never a token.
_OPENAI_HEADER_KEYS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)

# Matches a Go-style duration token: "1h", "4m", "30s", "500ms" — "ms" is
# listed before "m"/"s" so `finditer` prefers the 2-letter unit at that
# position instead of splitting it into "m" + trailing "s".
_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def _parse_openai_duration_seconds(value: Optional[str]) -> Optional[float]:
    """Parse an OpenAI ``x-ratelimit-reset-*`` duration string to seconds.

    OpenAI reports resets as a Go-style duration (``"6m0s"``, ``"1s"``,
    ``"200ms"``, ``"1h4m0s"``), not an absolute timestamp. Returns ``None`` for
    an empty/unparseable value rather than raising.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    matches = _DURATION_TOKEN_RE.findall(text)
    if not matches:
        return None
    total = 0.0
    unit_seconds = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    for amount, unit in matches:
        try:
            total += float(amount) * unit_seconds[unit]
        except (TypeError, ValueError):
            continue
    return total


def _openai_reset_epoch(duration: Optional[str]) -> Optional[str]:
    """Convert an OpenAI reset-duration header into an epoch-seconds string."""
    secs = _parse_openai_duration_seconds(duration)
    if secs is None:
        return None
    return str(int(time.time() + secs))


def normalize_openai_headers(headers: Mapping[str, str]) -> ProviderUsage:
    """Normalize OpenAI ``x-ratelimit-*`` response headers. Pure; never raises.

    Emits up to two windows — ``requests`` and ``tokens`` — each computed as
    ``(limit - remaining) / limit * 100`` (percent of the window already used,
    the same "utilization" semantics as the Codex/Claude windows). A window is
    omitted when its ``limit``/``remaining`` pair is missing or the limit is
    non-positive. OpenAI's ``/responses`` endpoint reports no dollar credits,
    so ``credits`` and ``plan_type`` are always ``None`` here.
    """
    h = {k: headers.get(k) for k in _OPENAI_HEADER_KEYS if headers.get(k) is not None}
    windows: List[Dict[str, Any]] = []
    for prefix in ("requests", "tokens"):
        limit = _as_float(h.get(f"x-ratelimit-limit-{prefix}"))
        remaining = _as_float(h.get(f"x-ratelimit-remaining-{prefix}"))
        if limit is None or remaining is None or limit <= 0:
            continue
        used_pct = max(0.0, min(100.0, (limit - remaining) / limit * 100.0))
        windows.append(
            {
                "type": prefix,  # "requests" / "tokens"
                "utilization": used_pct,
                "resets_at": _openai_reset_epoch(h.get(f"x-ratelimit-reset-{prefix}")),
            }
        )
    return ProviderUsage(windows=windows, credits=None, plan_type=None, raw=h)


def capture_openai_headers(provider_id: str, headers: Mapping[str, str]) -> None:
    """Snapshot allowlisted OpenAI headers synchronously, then persist off the loop.

    Mirrors :func:`capture_codex_headers` exactly: never raises, never blocks
    the caller, no-op if none of the allowlisted headers are present.
    """
    try:
        snap = {k: headers[k] for k in _OPENAI_HEADER_KEYS if k in headers}
        if not snap:
            return
        task = asyncio.create_task(_persist_openai_capture(provider_id, snap))
    except RuntimeError:
        return  # no running loop (sync test context)
    except Exception:  # noqa: BLE001 — capture must never break the caller
        return
    _pending_capture_tasks.add(task)
    task.add_done_callback(_pending_capture_tasks.discard)


async def _persist_openai_capture(provider_id: str, headers: Dict[str, str]) -> None:
    """Normalize + upsert a captured OpenAI header snapshot. Best-effort."""
    try:
        u = normalize_openai_headers(headers)
        from app.db import database as db

        # The OpenAI Responses leg also serves xAI, SuperGrok and custom
        # OpenAI-compatible rows. Label the snapshot by the row's registry
        # source so their per-minute headroom is never shown as OpenAI usage
        # or written over a subscription allowance.
        try:
            row = await run_sync(db.get_model_provider, provider_id)
        except Exception:  # noqa: BLE001 — classification falls back to OpenAI
            row = None
        snapshot_type = usage_sources.passive_snapshot_type(row, codex_backend=False)
        if snapshot_type is None:
            return

        # Poll recovered: forget the suppressed-failure state so the
        # next failure is reported at ERROR straight away.
        _clear_poll_failure(f"openai-capture:{provider_id}")
        await run_sync(
            db.upsert_provider_usage_snapshot,
            provider_id,
            snapshot_type,
            u.plan_type,
            u.windows,
            u.credits,
            u.raw,
            int(time.time() * 1000),
        )
    except Exception as e:  # noqa: BLE001 — capture must never break a turn
        # Never log header values — only the exception's own message.
        _log_poll_failure(
            f"openai-capture:{provider_id}",
            "OpenAI usage capture failed for provider %s: %s", provider_id, e,
        )


# ---------------------------------------------------------------------------
# Anthropic API-key passive capture
# ---------------------------------------------------------------------------

# Header allowlist — ONLY these travel into `raw`; nothing else, never a token.
_ANTHROPIC_HEADER_KEYS = (
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-limit",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-limit",
    "anthropic-ratelimit-output-tokens-remaining",
    "anthropic-ratelimit-output-tokens-reset",
)

# window-type key (with underscores) -> header-name fragment (with hyphens).
_ANTHROPIC_WINDOW_PREFIXES = ("requests", "tokens", "input-tokens", "output-tokens")


def normalize_anthropic_headers(headers: Mapping[str, str]) -> ProviderUsage:
    """Normalize Anthropic ``anthropic-ratelimit-*`` response headers. Pure; never raises.

    Emits up to four windows — ``requests``, ``tokens``, ``input_tokens``,
    ``output_tokens`` — each computed as ``(limit - remaining) / limit * 100``,
    the same "utilization" semantics as the Codex/Claude windows. Unlike
    OpenAI's duration-based reset, Anthropic's ``*-reset`` headers are already
    absolute RFC 3339 timestamps, so they pass through verbatim as
    ``resets_at``. A window is omitted when its ``limit``/``remaining`` pair is
    missing or the limit is non-positive. The Anthropic Messages API reports no
    dollar credits, so ``credits`` and ``plan_type`` are always ``None`` here.
    """
    h = {k: headers.get(k) for k in _ANTHROPIC_HEADER_KEYS if headers.get(k) is not None}
    windows: List[Dict[str, Any]] = []
    for prefix in _ANTHROPIC_WINDOW_PREFIXES:
        limit = _as_float(h.get(f"anthropic-ratelimit-{prefix}-limit"))
        remaining = _as_float(h.get(f"anthropic-ratelimit-{prefix}-remaining"))
        if limit is None or remaining is None or limit <= 0:
            continue
        used_pct = max(0.0, min(100.0, (limit - remaining) / limit * 100.0))
        windows.append(
            {
                "type": prefix.replace("-", "_"),  # requests / tokens / input_tokens / output_tokens
                "utilization": used_pct,
                "resets_at": h.get(f"anthropic-ratelimit-{prefix}-reset"),
            }
        )
    return ProviderUsage(windows=windows, credits=None, plan_type=None, raw=h)


def capture_anthropic_headers(provider_id: str, headers: Mapping[str, str]) -> None:
    """Snapshot allowlisted Anthropic headers synchronously, then persist off the loop.

    Mirrors :func:`capture_codex_headers` exactly: never raises, never blocks
    the caller, no-op if none of the allowlisted headers are present.
    """
    try:
        snap = {k: headers[k] for k in _ANTHROPIC_HEADER_KEYS if k in headers}
        if not snap:
            return
        task = asyncio.create_task(_persist_anthropic_capture(provider_id, snap))
    except RuntimeError:
        return  # no running loop (sync test context)
    except Exception:  # noqa: BLE001 — capture must never break the caller
        return
    _pending_capture_tasks.add(task)
    task.add_done_callback(_pending_capture_tasks.discard)


async def _persist_anthropic_capture(provider_id: str, headers: Dict[str, str]) -> None:
    """Normalize + upsert a captured Anthropic header snapshot. Best-effort."""
    try:
        u = normalize_anthropic_headers(headers)
        from app.db import database as db

        # Poll recovered: forget the suppressed-failure state so the
        # next failure is reported at ERROR straight away.
        _clear_poll_failure(f"anthropic-capture:{provider_id}")
        await run_sync(
            db.upsert_provider_usage_snapshot,
            provider_id,
            "anthropic",
            u.plan_type,
            u.windows,
            u.credits,
            u.raw,
            int(time.time() * 1000),
        )
    except Exception as e:  # noqa: BLE001 — capture must never break a turn
        # Never log header values — only the exception's own message.
        _log_poll_failure(
            f"anthropic-capture:{provider_id}",
            "Anthropic usage capture failed for provider %s: %s", provider_id, e,
        )


# ---------------------------------------------------------------------------
# OpenRouter poller (U3)
# ---------------------------------------------------------------------------

_KEY_PATH = "/api/v1/key"
# Account-level credit ledger (lifetime purchased vs lifetime spent). Distinct
# from /key's `usage`/`limit`, which describe the API KEY's own spend cap.
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


# ---------------------------------------------------------------------------
# Grok SuperGrok subscription poller
# ---------------------------------------------------------------------------

# Official Grok CLI / SuperGrok OAuth usage (NOT grok.com cookies, NOT console
# billing). Host is pinned; requests never follow redirects so the bearer token
# cannot hop hosts.
GROK_BILLING_HOST = "cli-chat-proxy.grok.com"
GROK_BILLING_URL = usage_sources.GROK_BILLING_URL
GROK_SETTINGS_URL = usage_sources.GROK_SETTINGS_URL
GROK_TOKEN_AUTH = "xai-grok-cli"
_GROK_SETTINGS_TIMEOUT_SECONDS = 5.0
_GROK_PLAN_FALLBACK = "SuperGrok"

# productUsage.product → window type. Unknown products become grok_<slug>.
# "GrokBuild" is deliberately absent: that product is skipped before a window is
# built, so it is never surfaced in the usage popover.
_GROK_PRODUCT_WINDOW_TYPES: Dict[str, str] = {
    "Api": "grok_api",
    "GrokChat": "grok_chat",
}

# credits keys that may leave this module. Never tokens.
_GROK_CREDITS_FIELDS: Tuple[str, ...] = (
    "credit_usage_percent",
    "on_demand_used",
    "on_demand_cap",
    "period_type",
    "period_end",
)

# raw keys we actually read from billing. Nested currentPeriod / productUsage
# are themselves allowlisted below — never Authorization, never the access token.
_GROK_RAW_FIELDS: Tuple[str, ...] = (
    "creditUsagePercent",
    "currentPeriod",
    "billingPeriodEnd",
    "productUsage",
    "onDemandUsed",
    "onDemandCap",
)
_GROK_PERIOD_FIELDS: Tuple[str, ...] = ("type", "start", "end")
_GROK_PRODUCT_FIELDS: Tuple[str, ...] = ("product", "usagePercent")


def _unwrap_val(value: Any) -> Any:
    """Accept a bare number or ``{"val": n}`` (Grok onDemand* shape)."""
    if isinstance(value, dict) and "val" in value:
        return value.get("val")
    return value


def _grok_resets_at(value: Any) -> Optional[str]:
    """Coerce a period end (ISO, epoch, or ``{"val": ...}``) to a string."""
    if isinstance(value, dict):
        value = value.get("val", value.get("end"))
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value).strip()
    return text or None


def _grok_period_present(cfg: Mapping[str, Any]) -> bool:
    """True when billing carries a parseable current/billing period."""
    period = cfg.get("currentPeriod")
    if isinstance(period, dict) and any(period.get(k) for k in _GROK_PERIOD_FIELDS):
        return True
    return cfg.get("billingPeriodEnd") is not None


def _grok_product_window_type(product: str) -> str:
    mapped = _GROK_PRODUCT_WINDOW_TYPES.get(product)
    if mapped:
        return mapped
    slug = re.sub(r"[^a-z0-9]+", "_", product.lower()).strip("_")
    return f"grok_{slug}" if slug else "grok_unknown"


def _allowlisted_grok_raw(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only the billing fields we actually read. Never credentials."""
    raw: Dict[str, Any] = {}
    if "creditUsagePercent" in cfg:
        raw["creditUsagePercent"] = cfg.get("creditUsagePercent")
    period = cfg.get("currentPeriod")
    if isinstance(period, dict):
        raw_period = {k: period[k] for k in _GROK_PERIOD_FIELDS if k in period}
        if raw_period:
            raw["currentPeriod"] = raw_period
    if "billingPeriodEnd" in cfg:
        raw["billingPeriodEnd"] = cfg.get("billingPeriodEnd")
    products = cfg.get("productUsage")
    if isinstance(products, list):
        raw_products = []
        for item in products:
            if not isinstance(item, dict):
                continue
            entry = {k: item[k] for k in _GROK_PRODUCT_FIELDS if k in item}
            if entry:
                raw_products.append(entry)
        if raw_products:
            raw["productUsage"] = raw_products
    if "onDemandUsed" in cfg:
        raw["onDemandUsed"] = cfg.get("onDemandUsed")
    if "onDemandCap" in cfg:
        raw["onDemandCap"] = cfg.get("onDemandCap")
    return raw


def normalize_grok_billing(payload: Any) -> ProviderUsage:
    """Normalize a SuperGrok ``/v1/billing?format=credits`` payload. Pure; never raises.

    Accepts the official ``{config: {...}}`` wrapper and a flat top-level shape.
    ``creditUsagePercent`` becomes the ``seven_day`` window; on-demand used/cap
    is the fallback when that percent is missing and cap > 0. A parseable
    current period with neither figure yields utilization 0 (not "no data").
    ``productUsage`` entries with a numeric ``usagePercent`` become extra
    windows; products without a percent, and the ``GrokBuild`` product (never
    surfaced), are skipped.
    """
    try:
        data = payload if isinstance(payload, dict) else {}
        cfg_obj = data.get("config")
        cfg: Mapping[str, Any] = cfg_obj if isinstance(cfg_obj, dict) else data

        credit_pct = _as_float(cfg.get("creditUsagePercent"))
        used = _as_float(_unwrap_val(cfg.get("onDemandUsed")))
        cap = _as_float(_unwrap_val(cfg.get("onDemandCap")))

        period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else None
        period_type = period.get("type") if period else None
        period_end = _grok_resets_at(period.get("end") if period else None)
        if period_end is None:
            period_end = _grok_resets_at(cfg.get("billingPeriodEnd"))

        windows: List[Dict[str, Any]] = []
        seven_day_util: Optional[float] = None
        if credit_pct is not None:
            seven_day_util = max(0.0, min(100.0, credit_pct))
        elif cap is not None and cap > 0 and used is not None:
            seven_day_util = max(0.0, min(100.0, used / cap * 100.0))
        elif _grok_period_present(cfg):
            seven_day_util = 0.0

        if seven_day_util is not None:
            windows.append(
                {
                    "type": "seven_day",
                    "utilization": seven_day_util,
                    "resets_at": period_end,
                }
            )

        products = cfg.get("productUsage")
        if isinstance(products, list):
            for item in products:
                if not isinstance(item, dict):
                    continue
                product = item.get("product")
                pct = _as_float(item.get("usagePercent"))
                if not product or pct is None:
                    continue
                # GrokBuild is deliberately not surfaced in the usage popover.
                if str(product) == "GrokBuild":
                    continue
                windows.append(
                    {
                        "type": _grok_product_window_type(str(product)),
                        "utilization": max(0.0, min(100.0, pct)),
                        "resets_at": period_end,
                    }
                )

        credits: Dict[str, Any] = {}
        if credit_pct is not None:
            credits["credit_usage_percent"] = credit_pct
        if used is not None:
            credits["on_demand_used"] = used
        if cap is not None:
            credits["on_demand_cap"] = cap
        if period_type is not None:
            credits["period_type"] = str(period_type)
        if period_end is not None:
            credits["period_end"] = period_end
        # Drop anything that somehow isn't on the allowlist.
        credits = {k: v for k, v in credits.items() if k in _GROK_CREDITS_FIELDS}

        raw = {k: v for k, v in _allowlisted_grok_raw(cfg).items() if k in _GROK_RAW_FIELDS}
        return ProviderUsage(
            windows=windows,
            credits=credits or None,
            plan_type=None,
            raw=raw,
        )
    except Exception:  # noqa: BLE001 — normalizer must never raise
        return ProviderUsage(windows=[], credits=None, plan_type=None, raw={})


def _decrypt_grok_access_token(blob: Any) -> Optional[str]:
    """Decrypt a SuperGrok OAuth access token. Never log the return value."""
    if not blob or not isinstance(blob, str):
        return None
    try:
        if encryption.is_encrypted(blob):
            return encryption.decrypt_value(blob)
        return blob
    except Exception:
        return None


def _grok_url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the bearer token must never leave cli-chat-proxy.grok.com."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == GROK_BILLING_HOST


async def _grok_get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> Tuple[Optional[Any], int]:
    """GET a pinned grok.com CLI-proxy URL. Never follows redirects."""
    if not _grok_url_is_pinned(url):
        raise ValueError("refusing non-pinned Grok billing host")
    resp = await _registered_get("grok_subscription", url, headers=dict(headers), timeout=timeout)
    body: Optional[Any] = None
    try:
        body = resp.json()
    except Exception:
        body = None
    return body, resp.status_code


def _grok_request_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-XAI-Token-Auth": GROK_TOKEN_AUTH,
        "Accept": "application/json",
    }


async def _maybe_refresh_grok_on_401(
    provider_id: str, stale_access: str
) -> bool:
    """Import-guarded SuperGrok token refresh after a billing 401.

    Without ``xai_token_refresh`` this is a no-op (log + skip) so the monitor
    never crashes.
    """
    try:
        from app.core import xai_token_refresh  # type: ignore[attr-defined]
    except ImportError:
        logger.warning(
            "Grok billing 401 for provider %s; xai_token_refresh not available",
            provider_id,
        )
        return False
    try:
        return bool(
            await xai_token_refresh.refresh_on_401_once(
                provider_id, stale_access_token=stale_access
            )
        )
    except Exception as e:
        logger.warning(
            "Grok token refresh after 401 for provider %s failed: %s",
            provider_id,
            e,
        )
        return False


def _https_host_is(url: str, host: str) -> bool:
    """True when ``url`` is https on the given host (no port tricks)."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == host


async def _registered_get(source_id: str, url: str, *, headers: Mapping[str, str], **kwargs: Any) -> httpx.Response:
    """Budgeted GET of an endpoint the registry lists for ``source_id``.

    Unlisted URLs (dashboard-internal or alpha billing endpoints, for example)
    are refused before any credential is sent.
    """
    endpoint = usage_sources.endpoint_for(source_id, url)
    return await usage_get(url, headers=dict(headers), interval=endpoint.min_interval, **kwargs)


def _failure_kind(status: Optional[int]) -> str:
    if status in usage_sources.DENIED_STATUSES:
        return "billing_access_denied"
    if status in usage_sources.UNSUPPORTED_STATUSES:
        return "unsupported"
    return "unavailable"


def _is_capability_refusal(status: Optional[int]) -> bool:
    return status in usage_sources.DENIED_STATUSES or status in usage_sources.UNSUPPORTED_STATUSES


async def _capability_denied(snapshot_id: str, fingerprint: str) -> bool:
    """True when this exact credential revision was already refused.

    A known-denied capability is not re-probed: no budget claim, no peer
    round trip and no upstream request. Any credential or revision change
    produces a different fingerprint and probes again.
    """
    from app.db import database as db

    try:
        raw = await run_sync(db.get_provider_usage_raw, snapshot_id)
    except Exception:  # noqa: BLE001 — an unreadable cache never blocks a poll
        return False
    return isinstance(raw, dict) and raw.get("denied_fingerprint") == fingerprint


# Row classification lives in the source registry; these names stay importable.
_is_openai_platform_row = usage_sources.is_openai_platform_row

# Retained only to exclude persisted snapshots from the retired OAuth fallback.
OPENAI_API_USAGE_PROVIDER_ID = "openai-api"


# ---------------------------------------------------------------------------
# fal.ai account billing poller
# ---------------------------------------------------------------------------

# The documented Platform API requires a separate *admin* key. There is no
# fallback to ordinary-key or alpha billing endpoints.
FAL_BILLING_HOST = "api.fal.ai"
FAL_BILLING_URL = usage_sources.FAL_BILLING_URL
FAL_USAGE_PROVIDER_ID = "fal-ai"
FAL_USAGE_PROVIDER_TYPE = "fal"
FAL_USAGE_PROVIDER_NAME = "fal.ai"

_FAL_CREDITS_FIELDS: Tuple[str, ...] = ("balance_remaining", "currency")
_FAL_RAW_FIELDS: Tuple[str, ...] = ("username", "current_balance", "currency", "source")


def _fal_billing_url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the Key credential must never leave api.fal.ai."""
    return _https_host_is(url, FAL_BILLING_HOST)


def _fal_auth_header(api_key: str) -> str:
    """Build fal's ``Authorization: Key …`` header. Never log the return value."""
    text = (api_key or "").strip()
    if text.lower().startswith("key "):
        return text
    return f"Key {text}"


def normalize_fal_billing(payload: Any) -> Optional[Dict[str, Any]]:
    """Map fal ``/v1/account/billing?expand=credits`` JSON to a credits blob.

    ``credits.current_balance`` becomes ``balance_remaining`` so the popover
    headlines ``$X.XX left``. Pure and defensive: a missing/non-numeric
    balance returns ``None``.
    """
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("data"), dict) and "credits" not in payload:
        payload = payload["data"]
    credits_obj = payload.get("credits")
    if not isinstance(credits_obj, dict):
        credits_obj = payload if "current_balance" in payload else None
    if not isinstance(credits_obj, dict):
        return None
    balance = _as_float(credits_obj.get("current_balance"))
    if balance is None:
        return None
    credits: Dict[str, Any] = {"balance_remaining": balance}
    currency = credits_obj.get("currency")
    if isinstance(currency, str) and currency.strip():
        credits["currency"] = currency.strip()
    return {k: v for k, v in credits.items() if k in _FAL_CREDITS_FIELDS}


def _allowlisted_fal_raw(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only billing fields we actually read. Never credentials."""
    raw: Dict[str, Any] = {}
    username = payload.get("username")
    if isinstance(username, str) and username:
        raw["username"] = username
    credits_obj = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    if "current_balance" in credits_obj:
        raw["current_balance"] = credits_obj.get("current_balance")
    if "currency" in credits_obj:
        raw["currency"] = credits_obj.get("currency")
    return {k: v for k, v in raw.items() if k in _FAL_RAW_FIELDS}


class ProviderUsageMonitor:
    """Fetches provider usage through credential-scoped, persistent request budgets.

    Best-effort, module-singleton, lifecycle mirrors
    ``app.core.usage_limits.UsageLimitsMonitor``: ``poll_once`` never raises,
    and one provider's failure (decrypt error, timeout, non-200) never blocks
    another provider's poll or breaks the loop. xAI API-key rows are skipped.
    """

    def __init__(self) -> None:
        self._poll_task: Optional[asyncio.Task] = None
        self._last_poll_finished: Optional[float] = None

    def start(self) -> None:
        # Account refreshes are demand-driven. Never launch billing browser tabs.
        pass

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self._poll_task = None

    @property
    def refreshing(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    def request_refresh(self, force: bool = False) -> Optional[asyncio.Task]:
        if self.refreshing:
            return self._poll_task
        if not force and self._last_poll_finished is not None:
            if time.monotonic() - self._last_poll_finished < 300:
                return None
        self._poll_task = asyncio.create_task(self._run_poll())
        return self._poll_task

    async def poll_once(self) -> None:
        task = self.request_refresh(force=True)
        if task is not None:
            await asyncio.shield(task)

    async def _run_poll(self) -> None:
        try:
            await asyncio.wait_for(self._poll_providers(), timeout=90)
        except Exception:
            logger.warning("Provider usage refresh failed")
        finally:
            self._last_poll_finished = time.monotonic()

    async def _poll_providers(self) -> None:
        from app.db import database as db

        try:
            rows = await run_sync(
                functools.partial(db.get_enabled_model_providers, include_token=True)
            )
        except Exception:
            logger.debug("Provider usage poll: provider read skipped")
            rows = []

        semaphore = asyncio.Semaphore(4)

        async def bounded(fn, *args):
            async with semaphore:
                try:
                    return await fn(*args)
                except Exception:
                    logger.warning("Provider usage job failed")
                    return False

        # The registry decides which sources apply to which rows; this loop
        # only schedules them. Rows no source applies to (xAI API keys, custom
        # hosts, Claude API keys) have no polled usage.
        jobs = [bounded(getattr(self, source.poller)) for source in usage_sources.global_sources()]
        for row in rows or []:
            for source in usage_sources.polled_sources(row or {}):
                jobs.append(bounded(getattr(self, source.poller), row))
        await asyncio.gather(*jobs, return_exceptions=True)

    async def _poll_openrouter_account(self, row: Dict[str, Any]) -> None:
        from app.core.openrouter_usage import poll

        await poll(row)

    async def _poll_provider(self, row: Dict[str, Any]) -> None:
        from app.db import database as db

        provider_id = row.get("id")
        try:
            key = _decrypt_provider_auth_token(row)
            base_url = row.get("base_url") or ""
            if not key or not base_url or not provider_id:
                return

            url = _origin(base_url) + _KEY_PATH
            headers = {"Authorization": f"Bearer {key}"}
            fingerprint = capability_fingerprint("openrouter_key", headers)
            if await _capability_denied(provider_id, fingerprint):
                return
            resp = await _registered_get("openrouter_key", url, headers=headers)

            if resp.status_code != 200:
                _log_poll_failure(
                    f"openrouter-key:{provider_id}",
                    "OpenRouter /key fetch for provider %s failed: HTTP %s",
                    provider_id,
                    resp.status_code,
                )
                await run_sync(
                    db.record_provider_usage_failure, provider_id, "openrouter",
                    _failure_kind(resp.status_code),
                    denied_fingerprint=fingerprint if _is_capability_refusal(resp.status_code) else None,
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

            # Poll recovered: forget the suppressed-failure state so the
            # next failure is reported at ERROR straight away.
            _clear_poll_failure(f"openrouter-poll:{provider_id}")
            _clear_poll_failure(f"openrouter-key:{provider_id}")
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
        except UsageDeferred:
            return
        except Exception as e:
            # Never include `key` / the Authorization header value here.
            _log_poll_failure(
                f"openrouter-poll:{provider_id}",
                "OpenRouter usage poll for provider %s failed: %s", provider_id, e,
            )

    async def _poll_grok_provider(self, row: Dict[str, Any]) -> None:
        """Fetch SuperGrok billing + settings and upsert a snapshot. Never raises.

        Uses the OAuth access token in ``codex_access_token`` only. The row's
        ``auth_token`` / ``XAI_API_KEY`` is never sent to cli-chat-proxy.
        """
        from app.db import database as db

        provider_id = row.get("id")
        try:
            if not provider_id or row.get("needs_reauth"):
                return

            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not stored or stored.get("needs_reauth"):
                return
            access = _decrypt_grok_access_token(stored.get("codex_access_token"))
            if not access:
                return

            headers = _grok_request_headers(access)
            fingerprint = capability_fingerprint("grok_subscription", headers)
            if await _capability_denied(provider_id, fingerprint):
                return
            body, status = await _grok_get(
                GROK_BILLING_URL, headers, _REQUEST_TIMEOUT_SECONDS
            )

            if status == 401:
                refreshed = await _maybe_refresh_grok_on_401(provider_id, access)
                if not refreshed:
                    await run_sync(db.record_provider_usage_failure, provider_id, "grok",
                                   "billing_access_denied", denied_fingerprint=fingerprint)
                    return
                stored = await run_sync(db.get_codex_tokens, provider_id)
                access = _decrypt_grok_access_token(
                    (stored or {}).get("codex_access_token")
                )
                if not access:
                    return
                headers = _grok_request_headers(access)
                fingerprint = capability_fingerprint("grok_subscription", headers)
                body, status = await _grok_get(
                    GROK_BILLING_URL, headers, _REQUEST_TIMEOUT_SECONDS
                )

            if status != 200 or not isinstance(body, dict):
                _log_poll_failure(
                    f"grok-billing:{provider_id}",
                    "Grok billing fetch for provider %s failed: HTTP %s",
                    provider_id,
                    status,
                )
                await run_sync(
                    db.record_provider_usage_failure, provider_id, "grok", _failure_kind(status),
                    denied_fingerprint=fingerprint if _is_capability_refusal(status) else None,
                )
                return

            u = normalize_grok_billing(body)
            plan_type = _GROK_PLAN_FALLBACK
            try:
                previous = await run_sync(db.get_all_provider_usage_snapshots)
            except Exception:
                previous = []
            for snapshot in previous or []:
                if snapshot.get("provider_id") == provider_id:
                    plan_type = snapshot.get("plan_type") or plan_type
                    break
            initial_plan = plan_type
            fetched_at = int(time.time() * 1000)
            await run_sync(
                db.upsert_provider_usage_snapshot,
                provider_id, "grok", plan_type, u.windows, u.credits, u.raw, fetched_at,
            )

            try:
                settings_body, settings_status = await _grok_get(
                    GROK_SETTINGS_URL, headers, _GROK_SETTINGS_TIMEOUT_SECONDS
                )
                if settings_status == 200 and isinstance(settings_body, dict):
                    display = settings_body.get("subscription_tier_display")
                    if isinstance(display, str) and display.strip():
                        plan_type = display.strip()
            except Exception as e:
                # Settings is additive; keep the billing snapshot.
                logger.debug(
                    "Grok settings fetch for provider %s failed: %s", provider_id, e
                )

            # Poll recovered: forget the suppressed-failure state so the
            # next failure is reported at ERROR straight away.
            _clear_poll_failure(f"grok-poll:{provider_id}")
            _clear_poll_failure(f"grok-billing:{provider_id}")

            if plan_type == initial_plan:
                return
            await run_sync(
                db.upsert_provider_usage_snapshot,
                provider_id,
                "grok",
                plan_type,
                u.windows,
                u.credits,
                u.raw,
                fetched_at,
            )
        except UsageDeferred:
            return
        except Exception as e:
            # Never include the bearer token here.
            _log_poll_failure(
                f"grok-poll:{provider_id}",
                "Grok usage poll for provider %s failed: %s", provider_id, e,
            )

    async def _poll_openai_provider(self, row: Dict[str, Any]) -> bool:
        from app.core.config import settings
        from app.core.openai_admin_usage import fetch
        from app.core.usage_polling import UsageDeferred
        from app.db import database as db

        provider_id = row.get("id")
        if not provider_id:
            return False
        # Browser bindings from older versions never authorize Admin API access.
        current = await run_sync(db.get_model_provider, provider_id, include_token=True)
        if not current or not current.get("enabled", True) or not _is_openai_platform_row(current):
            return False
        encrypted = current.get("openai_admin_key")
        org = current.get("openai_billing_org_id")
        if not encrypted or not org or current.get("openai_billing_instance_id") != settings.effective_instance_id:
            return False
        binding = (org, settings.effective_instance_id)
        fingerprint = None
        try:
            key = encryption.decrypt_value(encrypted)
            # Re-saving the key re-encrypts it, so a deliberate re-save retries.
            fingerprint = capability_fingerprint(
                "openai_admin", {"Authorization": f"Bearer {key}", "OpenAI-Organization": org}, encrypted,
            )
            if await _capability_denied(provider_id, fingerprint):
                return False
            results = await asyncio.gather(fetch(key, org, costs=True), fetch(key, org), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException) and not isinstance(r, UsageDeferred)]
            if failures:
                raise failures[0]
            fetched = [r for r in results if isinstance(r, dict)]
            if not fetched:
                return False
            credits = {**fetched[0], **fetched[-1]}
            if len(fetched) == 1:
                # The two endpoints' budgets drift out of phase; merge the half
                # that was fetched into the stored reading instead of discarding it.
                stored = next((r for r in await run_sync(db.get_all_provider_usage_snapshots)
                               if r.get("provider_id") == provider_id), None) or {}
                previous = stored.get("credits") or {}
                if ((stored.get("raw") or {}).get("organization_id") != org
                        or previous.get("period_start") != credits.get("period_start")):
                    previous = {}
                credits = {**previous, **credits}
                if "spend_month" not in credits:
                    return False
            return await run_sync(
                db.upsert_provider_usage_snapshot, provider_id, "openai", None, [], credits,
                {"source": "openai_admin", "organization_id": org}, int(time.time() * 1000),
                expected_binding=binding, expected_admin_key=encrypted,
            )
        except Exception as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            kind = _failure_kind(status)
            # Never log an exception with provider response text or credentials.
            _log_poll_failure(f"openai-admin:{provider_id}", "OpenAI Admin usage failed for %s (%s)", provider_id, kind)
            await run_sync(db.record_provider_usage_failure, provider_id, "openai", kind,
                           expected_binding=binding, expected_admin_key=encrypted,
                           denied_fingerprint=fingerprint if _is_capability_refusal(status) else None)
            return False

    async def _poll_fal(self) -> None:
        """Fetch fal.ai account credits and upsert the synthetic snapshot. Never raises.

        Only the optional, separate local ``fal_billing_api_key`` is used.
        Credential mutations invalidate ``fal-ai`` inside their transaction;
        every result is conditionally committed against the captured revision.
        No ordinary-key probes or undocumented fallback endpoints are allowed.
        """
        from app.db import database as db

        generation = None
        try:
            from app.api.settings_crypto import get_decrypted_api_key

            generation = await run_sync(db.get_system_setting_revision, "fal_billing_api_key")
            key = await run_sync(get_decrypted_api_key, "fal_billing_api_key")
            if generation != await run_sync(db.get_system_setting_revision, "fal_billing_api_key"):
                return
            if not key:
                return

            async def failed(kind="unavailable", denied=None):
                await run_sync(db.record_provider_usage_failure, FAL_USAGE_PROVIDER_ID,
                               FAL_USAGE_PROVIDER_TYPE, kind,
                               expected_credential_generation=generation,
                               denied_fingerprint=denied)

            headers = {
                "Authorization": _fal_auth_header(key),
                "Accept": "application/json",
            }
            credits: Optional[Dict[str, Any]] = None
            raw: Dict[str, Any] = {}

            if not _fal_billing_url_is_pinned(FAL_BILLING_URL):
                return
            # Re-saving the Admin key bumps its revision and probes again.
            fingerprint = capability_fingerprint("fal_billing", headers, generation)
            if await _capability_denied(FAL_USAGE_PROVIDER_ID, fingerprint):
                return
            resp = await _registered_get("fal_billing", FAL_BILLING_URL, headers=headers)
            if resp.status_code != 200:
                refused = _is_capability_refusal(resp.status_code)
                await failed(_failure_kind(resp.status_code), fingerprint if refused else None)
                return
            body = resp.json()
            credits = normalize_fal_billing(body)
            raw = {**_allowlisted_fal_raw(body), "source": "fal_admin"}

            if not credits:
                await failed()
                return
            # Poll recovered: forget the suppressed-failure state so the
            # next failure is reported at ERROR straight away.
            _clear_poll_failure("fal-credits-poll")
            await run_sync(
                db.upsert_provider_usage_snapshot,
                FAL_USAGE_PROVIDER_ID,
                FAL_USAGE_PROVIDER_TYPE,
                None,
                [],
                credits,
                raw,
                int(time.time() * 1000),
                expected_credential_generation=generation,
            )
        except UsageDeferred:
            return
        except Exception as e:
            # Never include the Key credential here.
            _log_poll_failure("fal-credits-poll", "fal.ai credits poll failed: %s", e)
            if generation:
                try:
                    await run_sync(db.record_provider_usage_failure, FAL_USAGE_PROVIDER_ID,
                                   FAL_USAGE_PROVIDER_TYPE, "unavailable",
                                   expected_credential_generation=generation)
                except Exception:
                    logger.debug("Could not persist fal.ai billing failure")


provider_usage_monitor = ProviderUsageMonitor()
