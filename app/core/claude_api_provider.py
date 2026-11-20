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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CLAUDE_API_PROVIDER_ID = "claude-api"
CLAUDE_API_NAME = "Claude API"
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
AUTH_MODE = "api_key"
# The last model list the saved key's Models tab read from Anthropic, for the
# active key only (shared, non-secret: model ids and labels plus a key hash).
MODELS_CACHE_SETTING = "claude_api_models_cache"
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
# remember_models: one read-modify-write of the per-key catalogs at a time.
_cache_lock = threading.Lock()
_CATALOG_SLOTS = 8


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


def _claude_off() -> bool:
    from app.core import claude_auth_mode

    return claude_auth_mode.current_mode() == claude_auth_mode.MODE_OFF


def _base_url(key: str) -> Optional[str]:
    """Anthropic for a real key; the dev loop's fake server for its fixture key."""
    from app.core.claude_api_models import DEV_KEY, dev_base_url

    return ANTHROPIC_API_BASE_URL if key != DEV_KEY else dev_base_url()


def _default_models() -> List[Dict[str, str]]:
    """The built-in list, used until the key's own catalog is read: the
    aliases' current ids first, then the ids it offered before the 5.5
    defaults, so a pin made from the old list still validates."""
    from app.core.model_registry import _BY_VALUE, API_MODEL_IDS, FROZEN_MODEL_IDS, MODELS

    models = [{"id": API_MODEL_IDS[m.value], "label": m.label} for m in MODELS if m.value in API_MODEL_IDS]
    for old in FROZEN_MODEL_IDS.values():
        if all(m["id"] != old for m in models):
            option = _BY_VALUE.get(old)
            models.append({"id": old, "label": option.label if option else old})
    return models


def _catalogs() -> Dict[str, Any]:
    """Saved catalogs by key hash: ``{hash: [model, ...]}``."""
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


def _cached_models(key: str) -> Optional[List[Dict[str, str]]]:
    models = [m for m in _catalogs().get(_key_hash(key)) or []
              if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
    return [{"id": m["id"], "label": str(m.get("label") or m["id"])} for m in models] or None


def remember_models(key: str, models: List[Dict[str, str]]) -> None:
    """Keep the list Anthropic returned for the ACTIVE key as this row's models.

    Each key has its own slot, so a slow read for a key that is no longer
    active can never overwrite the new key's list.
    """
    if not models or key != _active_key():
        return
    from app.db import database

    digest = _key_hash(key)
    with _cache_lock:
        catalogs = {h: m for h, m in _catalogs().items() if h != digest}
        # The newest last; a handful of saved keys is the most anyone keeps.
        catalogs = dict(list(catalogs.items())[-(_CATALOG_SLOTS - 1):])
        catalogs[digest] = [{"id": m["id"], "label": m.get("label") or m["id"]} for m in models]
        database.set_system_setting(MODELS_CACHE_SETTING, json.dumps({"by_key": catalogs}))


def virtual_row(include_token: bool = False) -> Optional[Dict[str, Any]]:
    """The Claude API provider row, or None without an active key (or Claude off).

    ``include_token`` adds the encrypted key as ``auth_token``, exactly like a
    stored row's blob; it is never present on a default read.
    """
    try:
        if _claude_off():
            return None
        key = _active_key()
    except Exception:  # noqa: BLE001 — an unreadable key is no row
        logger.warning("Could not read the saved Anthropic API key", exc_info=True)
        return None
    if not key:
        return None
    base_url = _base_url(key)
    if base_url is None:
        return None
    models = _cached_models(key) or _default_models()
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
    One read per key per few minutes, and never on an event loop (it blocks).
    Single-flight: a caller that arrives while the read runs waits for it
    (bounded) instead of finding the cooldown and refusing. True when the
    active key now has a saved list, so the caller checks the model again.
    """
    import asyncio
    import time

    import httpx

    from app.core import claude_api_models

    try:
        if _claude_off():
            return False
        key = _active_key()
    except Exception:  # noqa: BLE001 — an unreadable key is no refresh
        return False
    if not key:
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
        return _cached_models(key) is not None

    async def read() -> List[Dict[str, str]]:
        async with httpx.AsyncClient() as client:
            return await claude_api_models.list_models(key, client=client)

    try:
        # Saved only while this key is still the active one (remember_models).
        remember_models(key, asyncio.run(read()))
    except Exception as exc:  # noqa: BLE001 — the caller rejects the model as before
        # CatalogError text is display-safe; never log the request (it has the key).
        logger.warning("Could not refresh the Claude API model list: %s", exc)
    finally:
        with _refresh_lock:
            _refresh_inflight.pop(digest, None)
        flight.set()
    return _cached_models(key) is not None
