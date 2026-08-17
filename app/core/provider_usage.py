"""Provider usage capture + polling (Chunks U2/U3 of the provider-usage program,
plus the #757 follow-up: passive OpenAI/Anthropic API-key header capture).

Four data paths land in this module, per ``docs/PROVIDER_USAGE_CONTEXT_PLAN.md``
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

# Parity with app.core.usage_limits._periodic_usage_limits's cadence.
_POLL_INTERVAL_SECONDS = 300
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


provider_usage_monitor = ProviderUsageMonitor()
