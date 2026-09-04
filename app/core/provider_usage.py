"""Provider usage capture + polling (Chunks U2/U3 of the provider-usage program,
plus the #757 follow-up: passive OpenAI/Anthropic API-key header capture).

Five data paths land in this module, per ``docs/PROVIDER_USAGE_CONTEXT_PLAN.md``
(Codex/OpenRouter) and GitHub issue #757 (OpenAI/Anthropic API-key providers):

**Codex passive capture (U2).** The fluid harness's native Codex leg receives
``x-codex-*`` plan/rate-limit headers on every response. The response event
hook installed by ``provider/harness_adapter.py::_usage_capture_http_client``
calls ``capture_codex_headers``, which normalizes those headers into the shared
``provider_usage_snapshots`` row shape and persists them fire-and-forget, off
the streaming hot path. (Before the translator gateway was retired the same
capture ran inside its Codex upstream leg.)

**OpenAI passive capture (#757).** The fluid harness's native OpenAI-platform
leg receives standard ``x-ratelimit-*`` headers; the same response hook calls
``capture_openai_headers``, which follows the exact same fire-and-forget
pattern as the Codex capture above, persisting ``provider_type="openai"``.

**Anthropic passive capture (#757).** An Anthropic leg receives
``anthropic-ratelimit-*`` headers on every successful response.
``capture_anthropic_headers`` follows the same pattern, persisting
``provider_type="anthropic"``.

**OpenRouter poller (U3).** ``ProviderUsageMonitor`` polls every enabled
``openrouter`` model-provider's key-info endpoint
(``GET {origin of base_url}/api/v1/key``) on the same 5-minute cadence as the
Claude usage-limits poller (``app.core.usage_limits``). Window/credits mapping
is governed by AMENDMENT 1 of the plan doc: when the key's ``limit`` is
non-null and positive, emit exactly ONE window —
``{type: "credit_limit", utilization: usage / limit * 100}``. Daily/weekly/
monthly spend never drives a window's utilization; it is always carried as
dollar figures in ``credits`` (populated in both branches).

**Grok SuperGrok poller.** The same monitor also polls every enabled
``provider_type='grok'`` / ``auth_mode='grok'`` subscription row against
``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` (OAuth
access token from ``codex_access_token``, never the row's API-key
``auth_token``). Settings (``subscription_tier_display``) is a separate
short-timeout fetch whose failure must not drop an already-fetched billing
snapshot. xAI API-key rows (``provider_type='xai'``) are never polled.

**OpenAI platform credits poller.** Enabled ``provider_type='openai'`` rows
whose ``base_url`` host is ``api.openai.com`` are polled against the dashboard
credit-grants endpoint the platform console uses
(``GET https://api.openai.com/v1/dashboard/billing/credit_grants``). This is
undocumented and 403s for project/service-account keys without billing access;
a 403 is logged at debug and skipped. Custom OpenAI-compatible gateways are
never sent to that host. The decrypted key is used only as a Bearer header
and is NEVER logged.

**fal.ai credits poller.** The same monitor reads the install's
``fal_api_key`` (not a model-provider row) and polls the official Platform
API (``GET https://api.fal.ai/v1/account/billing?expand=credits``). The
snapshot is stored under the synthetic id ``fal-ai``. Missing key deletes
that row so the popover does not keep a stale balance.

Security (S4 hard invariant): the allowlists below are the ONLY thing that
ever reaches ``raw`` (and, transitively, the DB and any future API response).
They never include ``Authorization``, ``chatgpt-account-id``, any cookie, or
any other credential. The decrypted OpenRouter / OpenAI / fal keys are used
only as Authorization header values and are NEVER logged, printed, or stored
(not even truncated). Failures are logged at WARNING without ever including
header or key values.
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

from app.core import encryption
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


def _decrypt_provider_auth_token(provider: Optional[Dict[str, Any]]) -> Optional[str]:
    """Decrypt a provider row's ``auth_token`` blob (or pass plaintext through).

    Relocated here from the retired translator gateway's ``upstream.py`` (it was
    that module's only remaining consumer besides this one). Never log the
    return value.
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
# OpenAI API-key passive capture (#757)
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

        await run_sync(
            db.upsert_provider_usage_snapshot,
            provider_id,
            "openai",
            u.plan_type,
            u.windows,
            u.credits,
            u.raw,
            int(time.time() * 1000),
        )
    except Exception as e:  # noqa: BLE001 — capture must never break a turn
        # Never log header values (S4) — only the exception's own message.
        logger.warning("OpenAI usage capture failed for provider %s: %s", provider_id, e)


# ---------------------------------------------------------------------------
# Anthropic API-key passive capture (#757)
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
        # Never log header values (S4) — only the exception's own message.
        logger.warning("Anthropic usage capture failed for provider %s: %s", provider_id, e)


# ---------------------------------------------------------------------------
# OpenRouter poller (U3)
# ---------------------------------------------------------------------------

_KEY_PATH = "/api/v1/key"
# Account-level credit ledger (lifetime purchased vs lifetime spent). Distinct
# from /key's `usage`/`limit`, which describe the API KEY's own spend cap.
_CREDITS_PATH = "/api/v1/credits"
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


def _merge_openrouter_credits(
    credits: Dict[str, Any], data: Any
) -> Dict[str, Any]:
    """Fold an OpenRouter ``/credits`` payload into an existing credits dict.

    ``/credits`` reports the ACCOUNT ledger (``total_credits`` purchased,
    ``total_usage`` spent all-time); ``balance_remaining`` is their difference
    and is what the popover headlines. Pure and defensive: a non-dict payload or
    a non-numeric field leaves ``credits`` untouched (the caller keeps whatever
    ``/key`` produced), and the existing ``usage`` key — the API KEY's spend, a
    different number — is never clobbered.
    """
    if not isinstance(data, dict):
        return credits
    total_credits = _as_float(data.get("total_credits"))
    total_usage = _as_float(data.get("total_usage"))
    if total_credits is None or total_usage is None:
        return credits
    merged = dict(credits)
    merged["total_credits"] = total_credits
    merged["total_usage"] = total_usage
    merged["balance_remaining"] = total_credits - total_usage
    return merged


# ---------------------------------------------------------------------------
# Grok SuperGrok subscription poller
# ---------------------------------------------------------------------------

# Official Grok CLI / SuperGrok OAuth usage (NOT grok.com cookies, NOT console
# billing). Host is pinned; requests never follow redirects so the bearer token
# cannot hop hosts.
GROK_BILLING_HOST = "cli-chat-proxy.grok.com"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
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


def _is_grok_subscription_row(row: Optional[Dict[str, Any]]) -> bool:
    """True for SuperGrok OAuth rows. xAI API-key rows are ``provider_type='xai'``."""
    r = row or {}
    return r.get("provider_type") == "grok" or r.get("auth_mode") == "grok"


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
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False
    ) as client:
        resp = await client.get(url, headers=headers)
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

    ``xai_token_refresh`` lands with the sibling Grok-provider PR. Until then
    this is a no-op (log + skip) so the monitor never crashes.
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


# ---------------------------------------------------------------------------
# OpenAI platform credit-grants poller
# ---------------------------------------------------------------------------

# Dashboard billing endpoint the platform console uses. Undocumented; 403s for
# project keys without billing access. Host is pinned so the Bearer token never
# leaves api.openai.com (and never follows redirects).
OPENAI_CREDIT_GRANTS_HOST = "api.openai.com"
OPENAI_CREDIT_GRANTS_URL = (
    "https://api.openai.com/v1/dashboard/billing/credit_grants"
)

# credits keys that may leave this module. Never tokens.
_OPENAI_CREDITS_FIELDS: Tuple[str, ...] = (
    "balance_remaining",
    "total_available",
    "total_granted",
    "total_credits",
    "total_used",
    "total_usage",
)
_OPENAI_CREDITS_RAW_FIELDS: Tuple[str, ...] = (
    "total_granted",
    "total_used",
    "total_available",
)


def _openai_credit_grants_url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the Bearer token must never leave api.openai.com."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == OPENAI_CREDIT_GRANTS_HOST


def _is_openai_platform_row(row: Optional[Dict[str, Any]]) -> bool:
    """True for official OpenAI API-key rows (not custom OpenAI-compatible hosts)."""
    r = row or {}
    if r.get("provider_type") != "openai":
        return False
    host = (urlsplit(r.get("base_url") or "").hostname or "").lower()
    return host == OPENAI_CREDIT_GRANTS_HOST


def normalize_openai_credit_grants(payload: Any) -> Optional[Dict[str, Any]]:
    """Map ``credit_grants`` JSON to the credits blob the usage popover renders.

    ``total_available`` is the remaining prepaid/grant balance (the figure the
    platform home page shows). It is copied to ``balance_remaining`` so the
    existing OpenRouter-style headline (``$X.XX left``) applies unchanged.
    Pure and defensive: a non-dict payload or a missing available figure
    returns ``None`` rather than raising.
    """
    if not isinstance(payload, dict):
        return None
    total_granted = _as_float(payload.get("total_granted"))
    total_used = _as_float(payload.get("total_used"))
    total_available = _as_float(payload.get("total_available"))
    if total_available is None and total_granted is not None and total_used is not None:
        total_available = total_granted - total_used
    if total_available is None:
        return None
    credits: Dict[str, Any] = {
        "balance_remaining": total_available,
        "total_available": total_available,
    }
    if total_granted is not None:
        credits["total_granted"] = total_granted
        credits["total_credits"] = total_granted
    if total_used is not None:
        credits["total_used"] = total_used
        credits["total_usage"] = total_used
    return {k: v for k, v in credits.items() if k in _OPENAI_CREDITS_FIELDS}


# ---------------------------------------------------------------------------
# fal.ai account billing poller
# ---------------------------------------------------------------------------

# Official Platform API. Host is pinned; requests never follow redirects so the
# Key credential cannot hop hosts. Synthetic snapshot id is not a model-provider
# UUID — fal is an integration key, not a chat gateway row.
FAL_BILLING_HOST = "api.fal.ai"
FAL_BILLING_URL = "https://api.fal.ai/v1/account/billing?expand=credits"
FAL_USAGE_PROVIDER_ID = "fal-ai"
FAL_USAGE_PROVIDER_TYPE = "fal"
FAL_USAGE_PROVIDER_NAME = "fal.ai"

_FAL_CREDITS_FIELDS: Tuple[str, ...] = ("balance_remaining", "currency")
_FAL_RAW_FIELDS: Tuple[str, ...] = ("username", "current_balance", "currency")


def _fal_billing_url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the Key credential must never leave api.fal.ai."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == FAL_BILLING_HOST


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
    """Polls OpenRouter, SuperGrok, OpenAI credit-grants, and fal.ai billing.

    Best-effort, module-singleton, lifecycle mirrors
    ``app.core.usage_limits.UsageLimitsMonitor``: ``poll_once`` never raises,
    and one provider's failure (decrypt error, timeout, non-200) never blocks
    another provider's poll or breaks the loop. xAI API-key rows are skipped.
    """

    async def poll_once(self) -> None:
        """Poll enabled OpenRouter, Grok, OpenAI-platform, and fal rows. Never raises."""
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
            rows = []

        for row in rows or []:
            r = row or {}
            if r.get("provider_type") == "openrouter":
                await self._poll_provider(r)
            elif _is_grok_subscription_row(r):
                await self._poll_grok_provider(r)
            elif _is_openai_platform_row(r):
                await self._poll_openai_provider(r)

        await self._poll_fal()

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
            credits = await self._merge_account_credits(
                provider_id, _origin(base_url), key, credits
            )
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

    async def _merge_account_credits(
        self,
        provider_id: Optional[str],
        origin: str,
        key: str,
        credits: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add the account credit balance from ``/api/v1/credits``, if reachable.

        Strictly additive and non-fatal: /credits is a separate endpoint from
        /key, so a non-200, a timeout or a malformed body must leave the /key
        snapshot exactly as it was rather than failing the whole poll. Logged at
        debug for that reason (a warning would be noise on key-only setups).
        """
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    origin + _CREDITS_PATH,
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code != 200:
                logger.debug(
                    "OpenRouter /credits for provider %s returned HTTP %s; "
                    "keeping /key credits only",
                    provider_id,
                    resp.status_code,
                )
                return credits
            body = resp.json() or {}
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                data = body if isinstance(body, dict) else {}
            return _merge_openrouter_credits(credits, data)
        except Exception as e:
            # Never include `key` / the Authorization header value here.
            logger.debug(
                "OpenRouter /credits fetch for provider %s failed: %s", provider_id, e
            )
            return credits

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
            body, status = await _grok_get(
                GROK_BILLING_URL, headers, _REQUEST_TIMEOUT_SECONDS
            )

            if status == 401:
                refreshed = await _maybe_refresh_grok_on_401(provider_id, access)
                if not refreshed:
                    return
                stored = await run_sync(db.get_codex_tokens, provider_id)
                access = _decrypt_grok_access_token(
                    (stored or {}).get("codex_access_token")
                )
                if not access:
                    return
                headers = _grok_request_headers(access)
                body, status = await _grok_get(
                    GROK_BILLING_URL, headers, _REQUEST_TIMEOUT_SECONDS
                )

            if status != 200 or not isinstance(body, dict):
                logger.warning(
                    "Grok billing fetch for provider %s failed: HTTP %s",
                    provider_id,
                    status,
                )
                return

            u = normalize_grok_billing(body)
            plan_type = _GROK_PLAN_FALLBACK

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

            await run_sync(
                db.upsert_provider_usage_snapshot,
                provider_id,
                "grok",
                plan_type,
                u.windows,
                u.credits,
                u.raw,
                int(time.time() * 1000),
            )
        except Exception as e:
            # Never include the bearer token here.
            logger.warning(
                "Grok usage poll for provider %s failed: %s", provider_id, e
            )

    async def _poll_openai_provider(self, row: Dict[str, Any]) -> None:
        """Fetch OpenAI credit-grants and upsert a credits-only snapshot. Never raises."""
        from app.db import database as db

        provider_id = row.get("id")
        try:
            if not provider_id:
                return
            key = _decrypt_provider_auth_token(row)
            if not key:
                return
            if not _openai_credit_grants_url_is_pinned(OPENAI_CREDIT_GRANTS_URL):
                return

            headers = {
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                resp = await client.get(OPENAI_CREDIT_GRANTS_URL, headers=headers)

            if resp.status_code in (401, 403):
                # Project / service-account keys cannot read billing. Expected.
                logger.debug(
                    "OpenAI credit_grants for provider %s returned HTTP %s",
                    provider_id,
                    resp.status_code,
                )
                return
            if resp.status_code != 200:
                logger.warning(
                    "OpenAI credit_grants for provider %s failed: HTTP %s",
                    provider_id,
                    resp.status_code,
                )
                return

            try:
                body = resp.json()
            except Exception:
                body = None
            credits = normalize_openai_credit_grants(body)
            if not credits:
                return
            raw = {
                k: body.get(k)
                for k in _OPENAI_CREDITS_RAW_FIELDS
                if isinstance(body, dict) and k in body
            }
            await run_sync(
                db.upsert_provider_usage_snapshot,
                provider_id,
                "openai",
                None,
                [],
                credits,
                raw,
                int(time.time() * 1000),
            )
        except Exception as e:
            # Never include the Bearer key here.
            logger.warning(
                "OpenAI credits poll for provider %s failed: %s", provider_id, e
            )

    async def _poll_fal(self) -> None:
        """Fetch fal.ai account credits and upsert the synthetic snapshot. Never raises.

        Fal is an integration key (``fal_api_key``), not a model-provider row.
        A missing key deletes any previous ``fal-ai`` snapshot so the popover
        cannot keep a stale balance after the user removes the key.
        """
        from app.db import database as db

        try:
            from app.api.settings_crypto import get_decrypted_api_key

            key = await run_sync(get_decrypted_api_key, "fal_api_key")
            if not key:
                await run_sync(db.delete_provider_usage_snapshot, FAL_USAGE_PROVIDER_ID)
                return
            if not _fal_billing_url_is_pinned(FAL_BILLING_URL):
                return

            headers = {
                "Authorization": _fal_auth_header(key),
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                resp = await client.get(FAL_BILLING_URL, headers=headers)

            if resp.status_code in (401, 403):
                logger.debug("fal.ai billing returned HTTP %s", resp.status_code)
                return
            if resp.status_code != 200:
                logger.warning("fal.ai billing fetch failed: HTTP %s", resp.status_code)
                return

            try:
                body = resp.json()
            except Exception:
                body = None
            credits = normalize_fal_billing(body)
            if not credits:
                return
            raw = _allowlisted_fal_raw(body if isinstance(body, dict) else {})
            await run_sync(
                db.upsert_provider_usage_snapshot,
                FAL_USAGE_PROVIDER_ID,
                FAL_USAGE_PROVIDER_TYPE,
                None,
                [],
                credits,
                raw,
                int(time.time() * 1000),
            )
        except Exception as e:
            # Never include the Key credential here.
            logger.warning("fal.ai credits poll failed: %s", e)


provider_usage_monitor = ProviderUsageMonitor()
