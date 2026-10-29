"""Shared, bounded model discovery snapshots; curation stays on the provider row."""
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Mapping

from app.core import encryption
from app.core.config import settings
from app.core.model_effort import sanitize_effort_metadata
from app.db import database as db

logger = logging.getLogger(__name__)
PREFIX = "model_catalog:"
MAX_BYTES = 512 * 1024
_PRICE_FIELDS = ("prompt", "completion", "input_cache_read", "input_cache_write")


def key(provider_id: str) -> str:
    """Return the shared system-setting key for a provider's discovery snapshot."""
    return PREFIX + provider_id


def binding(row: Mapping[str, Any] | None) -> str | None:
    """Fingerprint the endpoint and decrypted credential, never its ciphertext.

    Subscription account IDs survive routine token renewal. If a legacy sign-in
    has no account ID, bind conservatively to its decrypted access token instead.
    An unreadable credential has no usable binding; its snapshots cannot be read.
    """
    if row is None:
        return None
    subscription = row.get("auth_mode") in ("chatgpt", "grok") or row.get("provider_type") == "chatgpt"
    identity = row.get("codex_account_id") if subscription else None
    if not identity:
        stored = row.get("codex_access_token" if subscription else "auth_token") or ""
        if subscription and not stored:
            return None
        try:
            identity = encryption.decrypt_value(stored) if encryption.is_encrypted(stored) else stored
        except Exception:  # Never log or expose credential/decryption failures.
            return None
    credential_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    fields = [row.get(field) or "" for field in ("provider_type", "base_url", "api_dialect", "auth_mode")]
    value = json.dumps([*fields, credential_hash], separators=(",", ":"), ensure_ascii=False)
    return "v1:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _model(entry: dict[str, Any]) -> dict[str, Any]:
    # Never serialize a provider row, upstream blob, endpoint, headers or tokens.
    result = {"id": entry["id"], "label": entry.get("label") or entry["id"]}
    window = entry.get("context_window")
    if isinstance(window, int) and window > 0:
        result["context_window"] = window
    prices = entry.get("pricing")
    if isinstance(prices, dict):
        result["pricing"] = {k: prices[k] for k in _PRICE_FIELDS
                             if isinstance(prices.get(k), (str, int, float))}
    result.update(sanitize_effort_metadata(entry))
    if entry.get("media_kind") in ("audio", "voice", "image", "video"):
        result["media_kind"] = entry["media_kind"]
    return result


def _encode(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def save(provider_id: str, catalog: dict[str, Any], *, expected_binding: str | None) -> dict[str, Any] | None:
    """Save discovery only for the credential binding that actually produced it.

    Re-read the current binding immediately before writing and skip discovery
    whose binding has changed. Reads verify it too, so a concurrent or synced
    credential change cannot make an older snapshot usable.

    Above 512 KiB, omit pricing first, then context/capability metadata and
    recommendations. Never truncate model IDs or labels. If it still cannot
    fit, log one warning and return None without replacing the previous snapshot.
    The caller keeps the full live discovery result regardless of compaction.
    """
    if not expected_binding:
        return None
    device_id = settings.effective_instance_id
    worker = db.get_install(device_id) or {}
    models = [_model(m) for m in catalog.get("models", [])]
    hidden = [_model(m) for m in catalog.get("hidden", [])]
    ids = {m["id"] for m in models}
    snapshot = {
        "source": "upstream", "models": models, "hidden": hidden,
        "binding": expected_binding,
        "recommended": [m for m in catalog.get("recommended", []) if m in ids],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetched_by_device": device_id,
        "fetched_by_name": worker.get("device_name") or device_id,
    }
    for fields in ((), ("pricing",), ("context_window", "reasoning_capabilities")):
        for model in models + hidden:
            for field in fields:
                model.pop(field, None)
        value = _encode(snapshot)
        if len(value.encode("utf-8")) <= MAX_BYTES:
            break
    else:
        snapshot["recommended"] = []
        value = _encode(snapshot)
        if len(value.encode("utf-8")) > MAX_BYTES:
            logger.warning("Model catalog for provider %s is too large to share; keeping the previous snapshot", provider_id)
            return None
    if binding(db.get_model_provider(provider_id, include_token=True)) != expected_binding:
        return None
    # Use the normal memo invalidation, replication and settings resource hook.
    db.set_system_setting(key(provider_id), value)
    return snapshot


def read(provider_id: str) -> dict[str, Any] | None:
    """Return discovery only when it matches the current credential binding.

    This rejects old or delayed sync records without needing delete tombstones.
    Legacy snapshots without a binding are not evidence for the current key.
    """
    value = db.get_system_setting(key(provider_id))
    if not value:
        return None
    snapshot = json.loads(value)
    current = binding(db.get_model_provider(provider_id, include_token=True))
    return snapshot if current and snapshot.get("binding") == current else None


def delete(provider_id: str) -> None:
    """Remove discovery when its provider or credential binding changes."""
    db.delete_system_setting(key(provider_id))
