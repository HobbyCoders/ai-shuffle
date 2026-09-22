"""Optional account credits, using a local management key on the official API."""

import math
import time
from urllib.parse import urlsplit

import httpx

from app.core.usage_polling import UsageDeferred, usage_get
from app.db.async_database import run_sync

CREDITS_URL = "https://openrouter.ai/api/v1/credits"
SETTING_PREFIX = "openrouter_billing_key:"
SNAPSHOT_PREFIX = "openrouter-account:"


def supported(row):
    try:
        url = urlsplit(row.get("base_url") or "")
        return (row.get("provider_type") == "openrouter" and url.scheme == "https"
                and url.hostname == "openrouter.ai" and url.port in (None, 443)
                and not url.username and not url.password)
    except ValueError:
        return False


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
    try:
        key = await run_sync(get_decrypted_api_key, setting)
        if await run_sync(db.get_system_setting_revision, setting) != generation:
            return
        error = "missing_credentials"
        if key:
            response = await usage_get(CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
            error = "billing_access_denied" if response.status_code in (401, 403) else "unavailable"
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
                   expected_credential_generation=generation)
