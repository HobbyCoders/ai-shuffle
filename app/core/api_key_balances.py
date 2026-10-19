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
from app.db import database as db, provider_balances
from app.db.async_database import run_sync

OPENAI_CREDIT_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
XAI_KEY_URL = "https://api.x.ai/v1/api-key"
XAI_BILLING_BASE = "https://management-api.x.ai/v1/billing/teams/"
INTERVAL = 300


def credential_fingerprint(row):
    """Pure identity check, including inference credentials after network sync."""
    identity = [row.get(k) for k in ("provider_type", "base_url", "auth_mode")]
    identity.append(encryption.decrypt_value(row.get("auth_token") or ""))
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
        value = value["val"]
    return float(number(value) / 100)


def parse_openai(body):
    return {"balance_remaining": float(number(body["total_available"])),
            "total_credits": float(number(body["total_granted"])),
            "total_usage": float(number(body["total_used"])), "currency": "USD"}


def parse_xai(balance, invoice):
    core = invoice["coreInvoice"]
    # Purchases/refunds are negative in the prepaid ledger; spend is positive.
    remaining = -usd_cents(balance["total"])
    postpaid = usd_cents(core["totalWithCorr"])
    prepaid_used = usd_cents(core["prepaidCreditsUsed"])
    return {"balance_remaining": remaining, "currency": "USD",
            "spend_month": postpaid + prepaid_used,
            "spending_limit": usd_cents(invoice["effectiveSpendingLimit"])}


def source_id(row):
    return "openai_platform_balance" if row.get("provider_type") == "openai" else "xai_management"


def status(row):
    from app.core.usage_sources import SOURCES
    configured = bool(row.get("balance_key") and row.get("balance_instance_id") == settings.effective_instance_id)
    try:
        snap = json.loads(row.get("balance_snapshot") or "{}") if configured else {}
    except (ValueError, TypeError):
        snap = {}
    if configured and snap.get("credential") != credential_fingerprint(row):
        configured, snap = False, {}
    fetched = snap.get("fetched_at")
    error = snap.get("last_error_kind")
    fresh = bool(fetched and not error and time.time() * 1000 - fetched <= INTERVAL * 1000)
    return {"configured": configured, "source": SOURCES[source_id(row)].label,
            "source_id": source_id(row), "fetched_at": fetched,
            "last_error_kind": error, "needs_reauth": error == "billing_access_denied",
            "status": "fresh" if fresh else "stale" if fetched else "unavailable",
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
    try:
        cached = json.loads(row.get("balance_snapshot") or "{}")
        if cached.get("credential") != credential_fingerprint(row):
            return
        team = cached.get("team_id")
        key = encryption.decrypt_value(row["balance_key"])
        if row["provider_type"] == "openai":
            credits = parse_openai(await get(OPENAI_CREDIT_URL, key))
        else:
            # Resolve with the inference key, never send a management key here.
            if not team:
                info = await get(XAI_KEY_URL, encryption.decrypt_value(row["auth_token"]))
                team = info["team_id"]
            if not isinstance(team, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", team):
                raise ValueError("Invalid team ID")
            base = XAI_BILLING_BASE + team
            import asyncio
            balance, invoice = await asyncio.gather(
                get(base + "/prepaid/balance", key),
                get(base + "/postpaid/invoice/preview", key),
            )
            credits = parse_xai(balance, invoice)
        await run_sync(provider_balances.snapshot, row, {"credits": credits, "team_id": team, "fetched_at": int(time.time() * 1000)})
    except UsageDeferred:
        if team and not cached.get("team_id"):
            await run_sync(provider_balances.snapshot, row, {"team_id": team})
        return
    except Exception as error:
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        # No exception strings or provider response bodies (they may echo keys).
        kind = "billing_access_denied" if code in (401, 403) else "unavailable"
        await run_sync(provider_balances.snapshot, row, {"last_error_kind": kind, "team_id": team, "fetched_at": cached.get("fetched_at")})
