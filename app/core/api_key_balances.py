"""Provider-reported API credit, never inferred from Admin spend or local costs."""
import hashlib
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.core import encryption
from app.core.config import settings
from app.core.usage_polling import UsageDeferred, usage_get
from app.core.usage_sources import SOURCES
from app.db import database as db, provider_balances
from app.db.async_database import run_sync

OPENAI_CREDIT_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
XAI_KEY_URL = "https://api.x.ai/v1/api-key"
XAI_BILLING_BASE = "https://management-api.x.ai/v1/billing/teams/"
INTERVAL = SOURCES["openai_platform_balance"].min_interval
FRESHNESS_SECONDS = SOURCES["openai_platform_balance"].public()["freshness_seconds"]


def credential_fingerprint(row):
    """Pure identity check, including inference credentials after network sync."""
    identity = [row.get(k) for k in ("provider_type", "base_url", "auth_mode")]
    identity.append(row.get("enabled", True))
    identity.extend(encryption.decrypt_value(row.get(k) or "") for k in ("auth_token", "balance_key"))
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def supported(row):
    try:
        url = urlsplit(row.get("base_url") or "")
        host = {"openai": "api.openai.com", "xai": "api.x.ai"}.get(row.get("provider_type"))
        return bool(host and url.scheme == "https" and url.hostname == host
                    and url.port in (None, 443) and not url.username and not url.password
                    and row.get("auth_mode") not in {"codex", "chatgpt", "grok"})
    except ValueError:
        return False


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid monetary value") from None
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("Invalid monetary value")
    return result


def usd_cents(value):
    # Documented xAI USD-cents wrapper. Never guess units from magnitude.
    # Reject unexpected currency/unit metadata instead of showing 100x credit.
    if isinstance(value, dict):
        if value.get("currency", "USD").upper() != "USD" or value.get("unit", "cents") not in {"cents", "USD_CENTS"}:
            raise ValueError("Unsupported monetary unit")
        value = value.get("val", 0)
    return float(number(value) / 100)


def parse_openai(body):
    return {"balance_remaining": float(number(body["total_available"])),
            "total_credits": float(number(body["total_granted"])),
            "total_usage": float(number(body["total_used"])), "currency": "USD"}


def parse_xai(balance, invoice=None):
    # Proto JSON omits zero values. Purchases are negative; spend is positive.
    result = {"balance_remaining": -usd_cents(balance.get("total", 0)), "currency": "USD"}
    if invoice is not None:
        core = invoice.get("coreInvoice") or {}
        # The prepaid sign is undocumented (examples use negative credits).
        # A live response still needs verifying before relying on cycle usage.
        result["spend_month"] = (usd_cents(core.get("totalWithCorr", 0))
                                 + abs(usd_cents(core.get("prepaidCreditsUsed", 0))))
        if "effectiveSpendingLimit" in invoice:
            result["spending_limit"] = usd_cents(invoice["effectiveSpendingLimit"])
    return result


def source_id(row):
    return "openai_platform_balance" if row.get("provider_type") == "openai" else "xai_management"


def status(row):
    configured = bool(row.get("balance_key") and row.get("balance_instance_id") == settings.effective_instance_id)
    try:
        snap = json.loads(row.get("balance_snapshot") or "{}") if configured else {}
    except (ValueError, TypeError):
        snap = {}
    if configured and snap.get("credential") != credential_fingerprint(row):
        snap = {}
    fetched = snap.get("fetched_at")
    error = snap.get("last_error_kind")
    fresh = bool(fetched and not error and 0 <= time.time() * 1000 - fetched <= FRESHNESS_SECONDS * 1000)
    return {"configured": configured, "source": SOURCES[source_id(row)].label,
            "source_id": source_id(row), "fetched_at": fetched,
            "fresh_until": fetched + FRESHNESS_SECONDS * 1000 if fetched else None,
            "last_error_scope": snap.get("last_error_scope"),
            "invoice_error_kind": snap.get("invoice_error_kind"),
            "spending_limit_error_kind": snap.get("spending_limit_error_kind"),
            "last_error_kind": error, "needs_reauth": error == "billing_access_denied",
            "last_deferred_reason": snap.get("last_deferred_reason"),
            "observed_at": fetched, "source_worker": snap.get("source_worker"),
            "status": ("fresh" if fresh else "stale" if fetched else "unavailable"
                       if error or not configured else "deferred" if snap.get("last_deferred_reason") else "pending"),
            # A stale or failed reading is never advertised as current credit.
            "credits": snap.get("credits") if fresh else None}


async def get(url, key):
    from app.core.usage_sources import interval_for
    source = "openai_platform_balance" if url == OPENAI_CREDIT_URL else "xai_management"
    response = await usage_get(url, headers={"Authorization": "Bearer " + key}, interval=interval_for(source, url))
    response.raise_for_status()
    return response.json()


async def poll(row):
    row = await run_sync(db.get_model_provider, row["id"], include_token=True)
    if not row or not supported(row) or not row.get("enabled", True):
        return
    if not row.get("balance_key") or row.get("balance_instance_id") != settings.effective_instance_id:
        return
    team, cached = None, {}
    error_scope = "balance"
    invoice_error = limit_error = None
    try:
        cached = json.loads(row.get("balance_snapshot") or "{}")
        if cached.get("credential") != credential_fingerprint(row):
            cached = {}
        team = cached.get("team_id")
        key = encryption.decrypt_value(row["balance_key"])
        if row["provider_type"] == "openai":
            credits = parse_openai(await get(OPENAI_CREDIT_URL, key))
        else:
            # Resolve with the inference key, never send a management key here.
            if not team:
                error_scope = "inference"
                info = await get(XAI_KEY_URL, encryption.decrypt_value(row["auth_token"]))
                team = info["team_id"]
                error_scope = "balance"
            if not isinstance(team, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", team):
                raise ValueError("Invalid team ID")
            base = XAI_BILLING_BASE + team
            balance = await get(base + "/prepaid/balance", key)
            credits = parse_xai(balance)
            try:
                invoice = await get(base + "/postpaid/invoice/preview", key)
                credits.update(parse_xai(balance, {"coreInvoice": invoice.get("coreInvoice")}))
                if "effectiveSpendingLimit" in invoice:
                    try:
                        credits["spending_limit"] = usd_cents(invoice["effectiveSpendingLimit"])
                    except (ValueError, TypeError, KeyError, AttributeError):
                        limit_error = "unavailable"
            except UsageDeferred:
                # Invoice and prepaid endpoints have independent budget jitter.
                # No observation is not an error: retain the optional endpoint's
                # last values and refusal state (including a paused 401/403).
                credits.update({k: v for k, v in (cached.get("credits") or {}).items()
                                if k in {"spend_month", "spending_limit"}})
                invoice_error = cached.get("invoice_error_kind")
                limit_error = cached.get("spending_limit_error_kind")
            except Exception as error:
                code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                invoice_error = "billing_access_denied" if code in (401, 403) else "unavailable"
        await run_sync(provider_balances.snapshot, row, {"credits": credits, "team_id": team,
            "invoice_error_kind": invoice_error, "spending_limit_error_kind": limit_error,
            "fetched_at": int(time.time() * 1000)})
    except UsageDeferred as deferred:
        # Discovery may be deferred before a team exists. Always persist the
        # wait and keep both discovery progress and the last successful reading.
        await run_sync(provider_balances.snapshot, row, {
            **cached, "team_id": team,
            "last_deferred_reason": deferred.reason,
        })
        return
    except Exception as error:
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        # No exception strings or provider response bodies (they may echo keys).
        kind = "billing_access_denied" if code in (401, 403) else "unavailable"
        await run_sync(provider_balances.snapshot, row, {"last_error_kind": kind, "last_error_scope": error_scope,
            "last_attempted_at": int(time.time() * 1000), "team_id": team, "fetched_at": cached.get("fetched_at")})
