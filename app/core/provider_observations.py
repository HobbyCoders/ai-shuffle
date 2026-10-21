"""Share normalized observations, not provider responses or local trust markers.

The request budget remains network-wide. A peer's reading travels through the
existing authenticated/encrypted settings sync, keyed by source/provider identity
with a network-keyed credential revision. Importing it never authorizes
billing access: local bindings and credential/revision guards still apply.
"""
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime

from app.core import encryption
from app.core.config import settings
from app.db import database as db, provider_balances
from app.db.async_database import run_sync

PREFIX = "provider_observation:"
BALANCES = {"openai_platform_balance", "xai_management"}
CREDIT_NUMBERS = frozenset({
    "balance_remaining", "total_credits", "total_usage", "spend_month", "spending_limit",
    "usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining",
    "balance", "period_start", "period_end", "input_tokens", "output_tokens", "num_model_requests",
    "credit_usage_percent", "on_demand_used", "on_demand_cap",
})
PLANS = frozenset({"free", "plus", "pro", "team", "business", "enterprise", "edu",
                   "SuperGrok", "SuperGrok Heavy"})
WINDOW_TYPES = frozenset({"five_hour", "seven_day", "primary", "secondary", "credit_limit",
                          "requests", "tokens", "input_tokens", "output_tokens", "grok", "supergrok",
                          "grok_api", "grok_chat", "grok_unknown"})


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _credits(value):
    if not isinstance(value, dict):
        return None
    result = {k: v for k, v in value.items() if k in CREDIT_NUMBERS and (v is None or _number(v))}
    if value.get("currency") in {"USD", "EUR", "GBP"}:
        result["currency"] = value["currency"]
    for name in ("has_credits", "unlimited", "is_free_tier"):
        if isinstance(value.get(name), bool):
            result[name] = value[name]
    period = value.get("period_type")
    if period in {"USAGE_PERIOD_TYPE_WEEKLY", "USAGE_PERIOD_TYPE_MONTHLY", "USAGE_PERIOD_TYPE_DAILY"}:
        result["period_type"] = period
    end = value.get("period_end")
    if isinstance(end, str):
        if re.fullmatch(r"[0-9]{1,13}", end):
            result["period_end"] = end
        else:
            try:
                datetime.fromisoformat(end.replace("Z", "+00:00"))
                result["period_end"] = end
            except ValueError:
                pass
    return result or None


def _windows(value):
    result = []
    for window in value if isinstance(value, list) else []:
        if not isinstance(window, dict):
            continue
        kind = window.get("type")
        if kind not in WINDOW_TYPES and not (isinstance(kind, str) and re.fullmatch(r"window_[0-9]{1,6}m", kind)):
            continue
        if not _number(window.get("utilization")):
            continue
        item = {"type": window["type"], "utilization": window["utilization"], "resets_at": None}
        reset = window.get("resets_at")
        if isinstance(reset, str) and re.fullmatch(r"[0-9]{1,13}", reset):
            item["resets_at"] = reset
        elif _number(reset):
            item["resets_at"] = reset
        elif isinstance(reset, str):
            try:
                datetime.fromisoformat(reset.replace("Z", "+00:00"))
                item["resets_at"] = reset
            except ValueError:
                pass
        result.append(item)
    return result


def _binding(source, row):
    """Capture local authorization and a cross-installation credential identity."""
    from app.api.settings_crypto import get_decrypted_api_key
    from app.core.provider_usage import _snapshot_id, fal_env_api_key, fal_env_key_generation
    from app.core.openrouter_usage import model_key_generation

    row = db.get_model_provider(row["id"], include_token=True) if row else None
    if source.id != "fal_billing" and (not row or not row.get("enabled", True)):
        return None
    parts = [source.id]
    guards = {}
    if row:
        parts.extend(row.get(k) for k in ("id", "provider_type", "base_url", "auth_mode",
                                         "openai_billing_org_id", "codex_account_id"))
        parts.extend(encryption.decrypt_value(row.get(k) or "") for k in
                     ("auth_token", "balance_key", "openai_admin_key", "codex_access_token"))
        guards["expected_provider_revision"] = (row["id"], row.get("revision"))
    if source.id in BALANCES:
        if not row.get("balance_key") or row.get("balance_instance_id") != settings.effective_instance_id:
            return None
    if source.id == "openai_admin":
        if not row.get("openai_admin_key") or row.get("openai_billing_instance_id") != settings.effective_instance_id:
            return None
        guards.update(expected_binding=(row.get("openai_billing_org_id"), settings.effective_instance_id),
                      expected_admin_key=row["openai_admin_key"])
    if source.id in {"fal_billing", "openrouter_credits"}:
        setting = "fal_api_key" if source.id == "fal_billing" else "openrouter_billing_key:" + row["id"]
        generation = db.get_system_setting_revision(setting)
        key = get_decrypted_api_key(setting) if generation else None
        if source.id == "fal_billing":
            key = key or fal_env_api_key()
            generation = generation or (fal_env_key_generation() if key else None)
            if not key:
                return None
        else:
            generation = generation or model_key_generation(row)
        parts.append(key)
        guards["expected_credential_generation"] = generation
    from app.network import crypto, engine
    ctx = engine.cached_context()
    if ctx is None:
        return None
    hmac_key = crypto._hkdf(ctx.network_key, b"shuffle-provider-observation-hmac-v1")
    revision = hmac.new(hmac_key, b"provider-observation-v1\0" + json.dumps(parts).encode(),
                        hashlib.sha256).hexdigest()
    provider_id = row["id"] if row else _snapshot_id(source, row)
    return PREFIX + source.id + ":" + provider_id, row, _snapshot_id(source, row), guards, revision


def _local(source, binding):
    if source.id in BALANCES:
        return json.loads(binding[1].get("balance_snapshot") or "{}")
    return next((s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == binding[2]), {})


def _read(source, row):
    binding = _binding(source, row)
    if not binding:
        return None
    local = _local(source, binding)
    try:
        shared = json.loads(db.get_system_setting(binding[0]) or "{}")
    except (ValueError, TypeError):
        shared = {}
    observed = shared.get("observed_at")
    attempted = max(local.get("last_attempted_at") or 0,
                    (local.get("raw") or {}).get("usage_attempted_at") or 0)
    matching = shared.get("credential_revision") == binding[4]
    if (matching and _number(observed) and observed <= time.time() * 1000
            and observed > max(local.get("fetched_at") or 0, attempted)):
        credits, windows = _credits(shared.get("credits")), _windows(shared.get("windows"))
        if credits or windows:
            provenance = {"observed_at": observed, "source_worker": shared.get("source_worker")}
            if source.id in BALANCES:
                provider_balances.snapshot(binding[1], {**local, "credits": credits, "fetched_at": observed,
                                                        "last_error_kind": None, "last_error_scope": None,
                                                        "last_deferred_reason": None,
                                                        **provenance})
            else:
                raw = {"source": source.id, **provenance}
                if source.id == "openai_admin":
                    raw["organization_id"] = binding[1].get("openai_billing_org_id")
                plan = shared.get("plan_type")
                db.upsert_provider_usage_snapshot(binding[2], source.snapshot_type,
                                                  plan if plan in PLANS else None, windows,
                                                  credits, raw, observed, **binding[3])
            local = _local(source, _binding(source, row) or binding)
    # Do not turn an old peer reading into a fresh one on every import. Local
    # observations still go through the existing budget (and its clock).
    from app.network import engine
    ctx = engine.cached_context()
    peer_reading = bool(matching and ctx and shared.get("source_worker")
                        and shared.get("source_worker") != ctx.device_id
                        and observed == local.get("fetched_at"))
    return binding, local, peer_reading


def import_keys(keys):
    """Import only matching arrivals: never start a full poll or claim a budget."""
    from app.core import usage_sources

    for key in keys:
        if not key.startswith(PREFIX):
            continue
        source_id, _, provider_id = key[len(PREFIX):].partition(":")
        source = usage_sources.SOURCES.get(source_id)
        if source is None or not source.poller:
            continue
        row = None if source.global_source else db.get_model_provider(provider_id, include_token=True)
        if not source.global_source and (not row or source not in usage_sources.polled_sources(row)):
            continue
        binding = _binding(source, row)
        if binding and binding[0] == key:
            _read(source, row)


def _publish(source, row, before):
    from app.network import engine

    binding = _binding(source, row)
    if not binding or binding[0] != before[0][0] or binding[4] != before[0][4]:
        return
    local = _local(source, binding)
    fetched = local.get("fetched_at")
    if not _number(fetched) or fetched <= (before[1].get("fetched_at") or 0):
        return
    if local.get("last_error_kind") or (local.get("raw") or {}).get("usage_error"):
        return
    credits, windows = _credits(local.get("credits")), _windows(local.get("windows"))
    if not credits and not windows:
        return
    ctx = engine.cached_context()
    if ctx is None:
        return
    source_worker = local.get("source_worker") or (local.get("raw") or {}).get("source_worker")
    if source_worker and source_worker != ctx.device_id:
        return  # A deferred poll may have imported a peer's reading mid-flight.
    # Explicit projection: raw, denial fingerprints, binding IDs, secrets and
    # provider-controlled text never enter the shared record.
    plan = local.get("plan_type")
    value = {"credits": credits, "windows": windows, "observed_at": fetched,
             "source_worker": ctx.device_id, "credential_revision": binding[4]}
    if plan in PLANS:
        value["plan_type"] = plan
    db.set_system_setting(binding[0], json.dumps(value))
    engine.record_after_commit("system_settings", binding[0])


async def poll_shared(source, row, poll):
    """Read a peer observation first, otherwise poll and publish a new reading."""
    before = None
    try:
        before = await run_sync(_read, source, row)
    except Exception:
        pass  # Sharing must not prevent the existing local poll path.
    if (before and before[2] and not before[1].get("last_error_kind")
            and not (before[1].get("raw") or {}).get("usage_error")
            and 0 <= time.time() * 1000 - (before[1].get("fetched_at") or 0) < source.min_interval * 1000):
        return
    result = await poll(row) if row is not None else await poll()
    if before:
        try:
            await run_sync(_publish, source, row, before)
        except Exception:
            pass  # The local reading remains usable when publication is offline.
    return result
