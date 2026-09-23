"""Optional account credits, using a local management key on the official API."""

import math
import time

import httpx

from app.core import usage_sources
from app.core.usage_polling import UsageDeferred, capability_fingerprint, usage_get
from app.db.async_database import run_sync

CREDITS_URL = usage_sources.OPENROUTER_CREDITS_URL
SETTING_PREFIX = "openrouter_billing_key:"
SNAPSHOT_PREFIX = "openrouter-account:"


supported = usage_sources.is_official_openrouter_row


def normalize(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    values = [data.get(name) for name in ("total_credits", "total_usage")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in values):
        return None
    total, used = values
    return {"total_credits": total, "total_usage": used,
            "balance_remaining": total - used, "currency": "USD"}


async def poll(row):
    from app.api.settings_crypto import get_decrypted_api_key
    from app.db import database as db

    if not supported(row) or not row.get("enabled", True):
        return
    provider_id = row["id"]
    setting = SETTING_PREFIX + provider_id
    snapshot = SNAPSHOT_PREFIX + provider_id
    generation = await run_sync(db.get_system_setting_revision, setting)
    if not generation:
        return
    denied = None
    try:
        key = await run_sync(get_decrypted_api_key, setting)
        if await run_sync(db.get_system_setting_revision, setting) != generation:
            return
        error = "missing_credentials"
        if key:
            headers = {"Authorization": f"Bearer {key}"}
            # A refused management key is not re-probed until it is replaced
            # or re-saved (either changes the setting revision).
            fingerprint = capability_fingerprint("openrouter_credits", headers, generation)
            raw = await run_sync(db.get_provider_usage_raw, snapshot)
            if isinstance(raw, dict) and raw.get("denied_fingerprint") == fingerprint:
                return
            response = await usage_get(CREDITS_URL, headers=headers,
                                       interval=usage_sources.interval_for("openrouter_credits", CREDITS_URL))
            status = response.status_code
            refused = status in usage_sources.DENIED_STATUSES or status in usage_sources.UNSUPPORTED_STATUSES
            denied = fingerprint if refused else None
            error = ("billing_access_denied" if status in usage_sources.DENIED_STATUSES
                     else "unsupported" if refused else "unavailable")
            if response.status_code == 200:
                credits = normalize(response.json())
                if credits is not None:
                    await run_sync(db.upsert_provider_usage_snapshot, snapshot, "openrouter_account",
                                   None, [], credits, {"source": "openrouter_management"},
                                   int(time.time() * 1000), expected_credential_generation=generation)
                    return
    except UsageDeferred:
        return
    except (httpx.TimeoutException, TimeoutError):
        error = "timeout"
    except Exception:  # noqa: BLE001 - isolate providers without logging secret-bearing errors
        # HTTP exception messages can contain request headers. Persist only a category.
        error = "unavailable"
    await run_sync(db.record_provider_usage_failure, snapshot, "openrouter_account", error,
                   expected_credential_generation=generation, denied_fingerprint=denied)
