"""Usage meters for native Messages vendors (Kimi, MiniMax).

Two documented, key-readable sources, both declared in
:mod:`app.core.usage_sources`:

* ``kimi_balance``: Moonshot's balance API for pay-as-you-go keys
  (https://platform.kimi.ai/docs/api/balance).
* ``minimax_plan``: MiniMax's M Plan remaining-quota API
  (https://platform.minimax.io/docs/m-plan/faq, "Use the API endpoint").

Kimi Code memberships, MiniMax pay-as-you-go keys and both OpenCode keys have
no documented source; they stay unavailable with their dashboard link.

Requests go through the registry guard (``_registered_get``), so an unlisted
URL is refused before the key is sent. Response bodies are parsed into a few
numbers and never logged or stored: a vendor error body may echo the key.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.usage_polling import UsageDeferred, capability_fingerprint
from app.core.usage_sources import KIMI_BALANCE_URL, MINIMAX_PLAN_URL
from app.db.async_database import run_sync

# MiniMax business errors that mean the key itself was refused
# (https://platform.minimax.io/docs/api-reference/errorcode).
_MINIMAX_AUTH_CODES = frozenset({1004, 2049})
# ``current_*_status`` in the quota response: 2 exhausted, 3 unlimited.
_EXHAUSTED, _UNLIMITED = 2, 3


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_kimi_balance(body: Any) -> dict | None:
    """``{"balance_remaining": <USD>, "currency": "USD"}`` or None when malformed.

    ``available_balance`` is cash plus vouchers; at or below zero the API
    refuses requests. Cash can be negative, so it is not the figure shown.
    """
    if not isinstance(body, Mapping) or body.get("code") != 0 or body.get("status") is False:
        return None
    data = body.get("data")
    if not isinstance(data, Mapping) or not _number(data.get("available_balance")):
        return None
    credits = {"balance_remaining": float(data["available_balance"]), "currency": "USD"}
    return credits


def _minimax_text_entry(remains: list) -> Mapping | None:
    for entry in remains:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("model_name")
        if isinstance(name, str) and (name.lower() == "general" or name.lower().startswith("minimax-m")):
            return entry
    return None


def _minimax_window(entry: Mapping, prefix: str, start_key: str, end_key: str, kind: str | None):
    """One window, or None when the response does not state it unambiguously.

    ``*_usage_count`` means "remaining" in older responses and "used" in newer
    ones, so it is never read; only the explicit remaining percentage and the
    exhausted status are trusted.
    """
    status = entry.get(f"current_{prefix}_status")
    total = entry.get(f"current_{prefix}_total_count")
    if status == _UNLIMITED or not _number(total) or total <= 0:
        return None
    pct = entry.get(f"current_{prefix}_remaining_percent")
    if status == _EXHAUSTED:
        used = 100.0
    elif _number(pct) and 0 <= pct <= 100:
        used = round(100.0 - float(pct), 2)
    else:
        return None
    start, end = entry.get(start_key), entry.get(end_key)
    if kind is None:
        if not (_number(start) and _number(end) and end > start):
            return None
        from app.core.codex_usage import window_type_for_minutes

        kind = window_type_for_minutes(round((end - start) / 60_000))
        if kind is None:
            return None
    reset = str(int(end)) if _number(end) and 0 < end < 10 ** 13 else None
    return {"type": kind, "utilization": used, "resets_at": reset}


def normalize_minimax_plan(body: Any) -> list[dict] | None:
    """The M Plan's text-model 5-hour and weekly windows, or None.

    None for a business error, a malformed body or a plan with no stated
    window (an inactive plan reports zero totals with the unlimited status).
    """
    if not isinstance(body, Mapping):
        return None
    base = body.get("base_resp")
    if isinstance(base, Mapping) and base.get("status_code") not in (0, None):
        return None
    remains = body.get("model_remains")
    entry = _minimax_text_entry(remains) if isinstance(remains, list) else None
    if entry is None:
        return None
    windows = [w for w in (
        _minimax_window(entry, "interval", "start_time", "end_time", None),
        _minimax_window(entry, "weekly", "weekly_start_time", "weekly_end_time", "seven_day"),
    ) if w is not None]
    return windows or None


def _minimax_refused(body: Any) -> bool:
    base = body.get("base_resp") if isinstance(body, Mapping) else None
    return isinstance(base, Mapping) and base.get("status_code") in _MINIMAX_AUTH_CODES


async def _poll(row: Mapping[str, Any], source_id: str, url: str, read) -> None:
    """One budgeted reading of ``source_id`` for ``row``. Never raises.

    ``read(body)`` returns ``(windows, credits)`` or None for an unusable
    body. Every write is guarded by the row revision read here, so a reading
    or failure that outlives a key replacement, disable or delete is dropped.
    A refused key is remembered by fingerprint and not re-probed until the
    key (or the row's revision) changes.
    """
    from app.core import provider_usage as pu
    from app.core.usage_sources import SOURCES
    from app.db import database as db

    provider_id = (row or {}).get("id")
    if not provider_id:
        return
    log_key = f"{source_id}:{provider_id}"
    guard = None
    fingerprint = None
    error = "unavailable"
    refused = False
    try:
        current = await run_sync(db.get_model_provider, provider_id, include_token=True)
        if (not current or not current.get("enabled", True) or not current.get("revision")
                or not SOURCES[source_id].applies(current)):
            return
        guard = (provider_id, current["revision"])
        key = pu._decrypt_provider_auth_token(current)
        if not key:
            return
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        fingerprint = capability_fingerprint(source_id, headers, current["revision"])
        if await pu._capability_denied(provider_id, fingerprint):
            return
        resp = await pu._registered_get(source_id, url, headers=headers)
        if resp.status_code != 200:
            refused = pu._is_capability_refusal(resp.status_code)
            error = pu._failure_kind(resp.status_code)
            pu._log_poll_failure(log_key, "%s poll for provider %s failed: HTTP %s",
                                 source_id, provider_id, resp.status_code)
        else:
            try:
                body: Any = resp.json()
            except ValueError:
                body = None
            reading = read(body)
            if reading is not None:
                windows, credits = reading
                pu._clear_poll_failure(log_key)
                await run_sync(db.upsert_provider_usage_snapshot, provider_id, source_id, None,
                               windows, credits, {"source": source_id}, int(time.time() * 1000),
                               expected_provider_revision=guard)
                return
            if source_id == "minimax_plan" and _minimax_refused(body):
                error, refused = "billing_access_denied", True
    except UsageDeferred:
        return
    except (httpx.TimeoutException, TimeoutError):
        error = "timeout"
    except Exception as e:  # noqa: BLE001 — a usage reading never breaks the pass
        # The exception type only: HTTP errors can carry request headers.
        pu._log_poll_failure(log_key, "%s poll for provider %s failed: %s",
                             source_id, provider_id, type(e).__name__)
    if guard is None:
        return
    try:
        await run_sync(db.record_provider_usage_failure, provider_id, source_id, error,
                       denied_fingerprint=fingerprint if refused else None,
                       expected_provider_revision=guard)
    except Exception as e:  # noqa: BLE001 — recording a failure never raises
        pu._log_poll_failure(log_key, "%s failure for provider %s not recorded: %s",
                             source_id, provider_id, type(e).__name__)


def _kimi_reading(body: Any):
    credits = normalize_kimi_balance(body)
    return ([], credits) if credits else None


def _minimax_reading(body: Any):
    windows = normalize_minimax_plan(body)
    return (windows, None) if windows else None


async def poll_kimi_balance(row: Mapping[str, Any]) -> None:
    await _poll(row, "kimi_balance", KIMI_BALANCE_URL, _kimi_reading)


async def poll_minimax_plan(row: Mapping[str, Any]) -> None:
    await _poll(row, "minimax_plan", MINIMAX_PLAN_URL, _minimax_reading)
