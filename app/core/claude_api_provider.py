"""The saved Anthropic API key as its own provider: "Claude API".

Claude runs two separate ways, like OpenAI's API key and ChatGPT subscription:

* the Claude subscription runs on the Claude Code CLI (the delegated leg), and
* the active saved Anthropic API key (:mod:`app.core.claude_accounts`) runs on
  fluid's native Anthropic client, through this provider.

The key is not a ``model_providers`` row: Settings > Claude manages it. This
module presents it as one built-in row (``CLAUDE_API_PROVIDER_ID``) that the
provider readers in :mod:`app.db.model_providers` add to their results, so the
picker, the leg decision (``provider_type`` ``anthropic`` with an API-key auth
mode runs natively), the credential bridge and usage accounting all treat it
like any other API-key provider. The active key is a shared setting, so a
switch of key applies on every worker.

The row is on whenever a key is selected, whatever the Claude Code mode:
turning the subscription off (the ``off`` mode) leaves Claude API running,
the way turning ChatGPT off leaves the OpenAI API key on. Turning Claude API
off clears the selected key (``claude_accounts.deactivate_api_key``) and keeps
every saved key.

The row's ``auth_mode`` is always ``api_key``: a subscription sign-in never
becomes this row, and ``fluid_credentials`` refuses subscription modes for the
native Anthropic client anyway.

Only a model bound to this row runs on the key. A Claude model chosen without a
provider is the Claude subscription on the CLI, in every sign-in mode. The old
``api_key`` sign-in mode used to hand the key to the CLI; the CLI never gets it
now, and on a worker still in that mode the default model is this row's
(:func:`app.core.credential_gate.default_run_model`), shown in the picker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CLAUDE_API_PROVIDER_ID = "claude-api"
CLAUDE_API_NAME = "Claude API"
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
AUTH_MODE = "api_key"
# Per-key cached Anthropic catalogs (shared, non-secret: ``{"by_key": {<hash>: [{"id", "label"}, ...]}}``
# — kept as a plain list per key hash for rolling-update compatibility with previous workers).
MODELS_CACHE_SETTING = "claude_api_models_cache"
# Per-key catalog refresh timestamp (separate key so the cache stays a plain list).
MODELS_FETCHED_AT_PREFIX = "claude_api_models_fetched_at:"
# Per-key curated enabled models: one row per key hash (``claude_api_models_curated:<hash>``)
# so offline edits to two different keys never overwrite each other across LWW sync.
MODELS_CURATED_PREFIX = "claude_api_models_curated:"
# Sorts before every real row, so "oldest created first" readers keep a stable
# order; the default model never comes from this row (see credential_gate).
_CREATED_AT = "1970-01-01T00:00:00+00:00"
# refresh_models_once: last try per key hash, so a missing model is not a
# catalog read on every turn.
_REFRESH_INTERVAL = 300.0
# How long a concurrent caller waits for the read already in flight (the read
# itself gives up after 12 s, claude_api_models.list_models).
_REFRESH_WAIT = 15.0
_refresh_tried: Dict[str, float] = {}
_refresh_inflight: Dict[str, threading.Event] = {}
_refresh_lock = threading.Lock()
# remember_models / save_curated_models: one read-modify-write at a time.
_cache_lock = threading.Lock()
_CATALOG_SLOTS = 32


def is_claude_api_id(provider_id: Optional[str]) -> bool:
    return provider_id == CLAUDE_API_PROVIDER_ID


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _active_key() -> Optional[str]:
    """The key Settings shows as active (the same rule as the saved-key list,
    ``claude_accounts._project_saved_keys``): the exact key the owner's synced
    choice pins, never a stale scalar, else the saved active key."""
    from app.core import claude_auth_mode

    choice = claude_auth_mode.key_pin()
    if choice is not None:
        return claude_auth_mode.selected_api_key(choice)
    return claude_auth_mode.stored_api_key()


def _base_url(key: str) -> Optional[str]:
    """Anthropic for a real key; the dev loop's fake server for its fixture key."""
    from app.core.claude_api_models import DEV_KEY, dev_base_url

    return ANTHROPIC_API_BASE_URL if key != DEV_KEY else dev_base_url()


def _default_models() -> List[Dict[str, str]]:
    """The built-in list, used until the key's own catalog is read: the
    aliases' current ids first, then the ids it offered before the 5.5
    defaults, so a pin made from the old list still validates."""
    from app.core.model_registry import _BY_VALUE, API_MODEL_IDS, FROZEN_MODEL_IDS, MODELS

    ids = [API_MODEL_IDS[m.value] for m in MODELS if m.value in API_MODEL_IDS]
    ids += [old for old in FROZEN_MODEL_IDS.values() if old not in ids]
    # Version labels ("Sonnet 5.5", "Sonnet 5"), so the two generations differ.
    return [{"id": i, "label": _BY_VALUE[i].label if i in _BY_VALUE else i} for i in ids]


def _normalize_models(models: Any) -> List[Dict[str, str]]:
    """Validate, deduplicate, and normalize ``[{"id", "label"}]`` model rows."""
    if not isinstance(models, list):
        return []
    seen: set[str] = set()
    cleaned: List[Dict[str, str]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        raw_id = m.get("id")
        if not isinstance(raw_id, str):
            continue
        model_id = raw_id.strip()
        if not model_id or len(model_id) > 300 or model_id in seen:
            continue
        seen.add(model_id)
        raw_label = m.get("label")
        label = raw_label.strip()[:300] if isinstance(raw_label, str) and raw_label.strip() else model_id
        cleaned.append({"id": model_id, "label": label})
    return cleaned


def _catalogs() -> Dict[str, Any]:
    """Saved raw catalog slots by key hash."""
    from app.db import database

    try:
        raw = database.get_system_setting(MODELS_CACHE_SETTING)
        value = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — a bad cache falls back to the defaults
        return {}
    if not isinstance(value, dict):
        return {}
    by_key = value.get("by_key")
    return dict(by_key) if isinstance(by_key, dict) else {}


def _parse_catalog_models(slot: Any) -> Optional[List[Dict[str, str]]]:
    """Parse a cached catalog slot into a normalized model list (``[]`` is valid)."""
    if isinstance(slot, list):
        return _normalize_models(slot)
    if isinstance(slot, dict) and isinstance(slot.get("models"), list):
        return _normalize_models(slot.get("models"))
    return None


def cached_catalog(key: str) -> Optional[Dict[str, Any]]:
    """Cached ``{"models": [...], "fetched_at": <iso|None>}`` for ``key``, or None on cache miss.

    An empty list ``[]`` is a valid cached catalog (distinct from ``None``).
    """
    from app.db import database

    digest = _key_hash(key)
    by_key = _catalogs()
    if digest not in by_key:
        return None
    models = _parse_catalog_models(by_key[digest])
    if models is None:
        return None
    try:
        fetched_at = database.get_system_setting(f"{MODELS_FETCHED_AT_PREFIX}{digest}")
    except Exception:  # noqa: BLE001
        fetched_at = None
    return {
        "models": models,
        "fetched_at": fetched_at if isinstance(fetched_at, str) and fetched_at else None,
    }


def _cached_models(key: str) -> Optional[List[Dict[str, str]]]:
    entry = cached_catalog(key)
    return entry["models"] if entry is not None else None


def remember_models(
    key: str,
    models: List[Dict[str, str]],
    *,
    touch_timestamp: bool = False,
) -> Optional[str]:
    """Cache the Anthropic catalog for ``key`` under its own hash slot.

    Stores ``{"by_key": {<hash>: [{"id", "label"}, ...]}}`` (plain list per key
    hash for rolling-update compatibility) and writes the ISO timestamp to
    ``claude_api_models_fetched_at:<hash>``. Caches valid empty lists ``[]`` so
    an empty refresh replaces stale cached models. Suppresses all DB writes
    when both the model list and timestamp are already present and unchanged
    (unless ``touch_timestamp=True`` for an explicit manual refresh, which
    updates only ``fetched_at`` and leaves ``claude_api_models_cache`` untouched).
    """
    if not isinstance(models, list):
        return None
    cleaned = _normalize_models(models)
    from app.db import database

    digest = _key_hash(key)
    ts_key = f"{MODELS_FETCHED_AT_PREFIX}{digest}"
    with _cache_lock:
        existing_map = _catalogs()
        has_slot = digest in existing_map
        existing_models = _parse_catalog_models(existing_map.get(digest)) if has_slot else None
        existing_is_plain_list = isinstance(existing_map.get(digest), list)
        existing_ts = database.get_system_setting(ts_key)
        cache_unchanged = has_slot and existing_is_plain_list and existing_models == cleaned
        if cache_unchanged and existing_ts and not touch_timestamp:
            return existing_ts
        if not cache_unchanged:
            catalogs = {h: slot for h, slot in existing_map.items() if h != digest}
            catalogs = dict(list(catalogs.items())[-(_CATALOG_SLOTS - 1):])
            catalogs[digest] = cleaned
            database.set_system_setting(MODELS_CACHE_SETTING, json.dumps({"by_key": catalogs}))
        now = datetime.now(timezone.utc).isoformat()
        database.set_system_setting(ts_key, now)
        return now


def curated_models(key: str) -> Optional[List[Dict[str, str]]]:
    """Explicitly curated models for ``key``, or ``None`` if never curated.

    Stored per key hash at ``claude_api_models_curated:<hash>`` so edits to
    independent keys never collide across LWW sync. ``None`` means uncurated
    (all cached/default models are offered); ``[]`` means every model unchecked.
    """
    from app.db import database

    setting_key = f"{MODELS_CURATED_PREFIX}{_key_hash(key)}"
    try:
        raw = database.get_system_setting(setting_key)
    except Exception:  # noqa: BLE001 — unreadable curation falls back to uncurated
        return None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, list):
        return None
    return _normalize_models(parsed)


def save_curated_models(key: str, models: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Persist the curated enabled models for ``key`` (empty list allowed).

    Suppresses DB writes when the key already stores the exact same list.
    """
    cleaned = _normalize_models(models)
    from app.db import database

    setting_key = f"{MODELS_CURATED_PREFIX}{_key_hash(key)}"
    with _cache_lock:
        raw = database.get_system_setting(setting_key)
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, list) and _normalize_models(parsed) == cleaned:
                return cleaned
        database.set_system_setting(setting_key, json.dumps(cleaned))
    return cleaned


def virtual_row(include_token: bool = False) -> Optional[Dict[str, Any]]:
    """The Claude API provider row, or None without an active key.

    ``include_token`` adds the encrypted key as ``auth_token``, exactly like a
    stored row's blob; it is never present on a default read.
    """
    try:
        key = _active_key()
    except Exception:  # noqa: BLE001 — an unreadable key is no row
        logger.warning("Could not read the saved Anthropic API key", exc_info=True)
        return None
    if not key:
        return None
    base_url = _base_url(key)
    if base_url is None:
        return None
    curated = curated_models(key)
    if curated is not None:
        models = curated
    else:
        cached = _cached_models(key)
        models = cached if cached is not None else _default_models()
    ids = [m["id"] for m in models]
    from app.core.model_registry import API_MODEL_IDS, DEFAULT_MODEL

    preferred = API_MODEL_IDS.get(DEFAULT_MODEL)
    row: Dict[str, Any] = {
        "id": CLAUDE_API_PROVIDER_ID,
        "name": CLAUDE_API_NAME,
        "provider_type": "anthropic",
        "base_url": base_url,
        "api_dialect": None,
        "models_json": models,
        "default_model": preferred if preferred in ids else (ids[0] if ids else None),
        "small_fast_model": None,
        "extra_env_json": {},
        "context_window": None,
        "max_output_tokens": None,
        "enabled": True,
        "auth_mode": AUTH_MODE,
        "codex_account_id": None,
        "codex_id_token_expires_at": None,
        "codex_last_refresh": None,
        "needs_reauth": False,
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        # Changes with the key and its model list, so capability caches refresh.
        "revision": _key_hash(key + json.dumps(ids)),
    }
    if include_token:
        from app.core import encryption

        row["auth_token"] = encryption.encrypt_value(key)
    return row


def api_model_id(model: Optional[str]) -> Optional[str]:
    """The Messages API id for a Claude pick (``opus`` → ``claude-opus-5-5``;
    a full id such as ``claude-opus-5`` stays that version)."""
    from app.core.model_registry import resolve_api_model_id

    return resolve_api_model_id(model)


def refresh_models_once() -> bool:
    """Read the active key's catalog when the saved list belongs to another key.

    For a bound model the row does not list, right after a switch of key: the
    row then shows the built-in list until someone opens the key's Models tab.
    Skipped when the key already has an explicit curation (never bypasses the
    owner's checkboxes) or already has a cached catalog. One read per key per
    few minutes, and never on an event loop (it blocks).
    """
    import asyncio
    import time

    import httpx

    from app.core import claude_api_models

    try:
        key = _active_key()
    except Exception:  # noqa: BLE001 — an unreadable key is no refresh
        return False
    if not key:
        return False
    if curated_models(key) is not None:
        return False
    if _cached_models(key) is not None:
        return True  # another caller already saved it: check again
    try:
        asyncio.get_running_loop()
        return False
    except RuntimeError:
        pass
    digest = _key_hash(key)
    with _refresh_lock:
        flight = _refresh_inflight.get(digest)
        owner = flight is None
        if owner:
            last = _refresh_tried.get(digest)
            if last is not None and time.monotonic() - last < _REFRESH_INTERVAL:
                return False
            _refresh_tried[digest] = time.monotonic()
            flight = _refresh_inflight[digest] = threading.Event()
    if not owner:
        flight.wait(_REFRESH_WAIT)
        return curated_models(key) is None and _cached_models(key) is not None

    async def read() -> List[Dict[str, str]]:
        async with httpx.AsyncClient() as client:
            return await claude_api_models.list_models(key, client=client)

    try:
        remember_models(key, asyncio.run(read()))
    except Exception as exc:  # noqa: BLE001 — the caller rejects the model as before
        # CatalogError text is display-safe; never log the request (it has the key).
        logger.warning("Could not refresh the Claude API model list: %s", exc)
    finally:
        with _refresh_lock:
            _refresh_inflight.pop(digest, None)
        flight.set()
    return curated_models(key) is None and _cached_models(key) is not None
