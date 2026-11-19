"""The models a saved Anthropic API key can use, from Anthropic's Models API.

Settings > Providers > Claude > Anthropic API key > Models lists what
``GET https://api.anthropic.com/v1/models`` returns for that key: the endpoint
is fixed, the request sends the key as ``x-api-key`` with ``anthropic-version``,
pages with ``limit``/``after_id`` and is bounded (pages, entries, bytes, time,
no redirects). Listing models sends no prompt and costs nothing.

Each list belongs to the key it was fetched with, not to an account row or to
whichever key is selected: it is cached under :func:`key_id`, a short one-way
hash of the key (never the key), in memory and in ``claude-api-models.json``
in the worker's data directory. A fetch whose key was deleted meanwhile is
dropped, and each write keeps only lists of keys that are still saved. So a
key switch during a refresh, or a saved inactive key, never lends its list to
another key.

Which versions the pickers offer is the owner's choice (``ENABLED_SETTING``,
a JSON list in ``system_settings``, shared by every saved key). In API-key
mode the pickers offer the four aliases plus the checked versions that the
SELECTED key's list contains; only the selected key's Models page can change
the choice. A version once picked runs as itself whatever the list says
later (``model_registry.keeps_own_id``): unchecking it, or a list without it,
only hides it from the pickers, and Anthropic answers for an id the key cannot
use. The aliases are always offered.

Only on a launcher-marked throwaway dev instance with a loopback fake provider
(``SHUFFLE_DEV_FAKE_AI_URL``) does the request go to that fake's
``/anthropic/v1/models`` instead (:func:`fake_models_url`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
ENABLED_SETTING = "claude_api_enabled_models"
"""System setting: JSON list of full model ids the API-key pickers offer."""

FAKE_PATH = "/anthropic/v1/models"
_FILE_NAME = "claude-api-models.json"
_PAGE_LIMIT = 1000  # The API's largest page.
_MAX_PAGES = 5
_MAX_MODELS = 500
_MAX_BYTES = 2_000_000
_TIMEOUT_SECONDS = 10.0
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
ALIASES = ("haiku", "sonnet", "opus", "fable")

_lock = threading.Lock()
_enabled_lock = threading.Lock()
_refresh_locks: Dict[str, threading.Lock] = {}
_cache: Dict[str, Dict[str, Any]] = {}
_loaded = False
_active_memo: Dict[str, Optional[str]] = {}


class CatalogError(RuntimeError):
    """A plain, secret-free reason the list could not be fetched."""


def key_id(key: str) -> str:
    """A non-secret id for a key: a one-way hash, never the key itself."""
    return hashlib.sha256(f"anthropic-models\n{key.strip()}".encode("utf-8")).hexdigest()[:16]


# ─── Endpoint ───────────────────────────────────────────────────────────────


def fake_models_url() -> Optional[str]:
    """The dev loop's fake catalog, only on a launcher-marked throwaway instance.

    ``SHUFFLE_DEV_FAKE_AI_URL`` must be a bare loopback ``http`` origin; any
    other value, or a real install, uses :data:`MODELS_URL`."""
    raw = (os.environ.get("SHUFFLE_DEV_FAKE_AI_URL") or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "http" or not port or parts.hostname not in ("127.0.0.1", "localhost", "::1")
            or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        return None
    try:
        from app.preview_run import disposable_preview_data

        if not disposable_preview_data():
            return None
    except Exception:  # noqa: BLE001 - an unreadable marker is a real install
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}:{port}{FAKE_PATH}"


def models_url() -> str:
    return fake_models_url() or MODELS_URL


# ─── Parsing ────────────────────────────────────────────────────────────────


def _effort_levels(capabilities: Any) -> Optional[List[str]]:
    """Effort levels ``capabilities.effort`` reports, ``[]`` for none, None when absent."""
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("effort"), dict):
        return None
    effort = capabilities["effort"]
    if effort.get("supported") is not True:
        return []
    return [level for level in _EFFORT_ORDER
            if isinstance(effort.get(level), dict) and effort[level].get("supported") is True]


def parse_page(payload: Any) -> tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """``(models, has_more, last_id)`` of one ``GET /v1/models`` page."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CatalogError("Anthropic returned an unexpected model list.")
    models = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        value = item.get("id")
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            continue
        label = item.get("display_name")
        window = item.get("max_input_tokens")
        created = item.get("created_at")
        models.append({
            "value": value.strip(),
            "label": label.strip()[:200] if isinstance(label, str) and label.strip() else value.strip(),
            "created_at": created if isinstance(created, str) else None,
            "context_window": window if isinstance(window, int) and not isinstance(window, bool)
            and window > 0 else None,
            "effort_levels": _effort_levels(item.get("capabilities")),
        })
    last = payload.get("last_id")
    return models, payload.get("has_more") is True, last if isinstance(last, str) and last else None


def _get_page(client: Any, url: str, key: str, after: Optional[str]) -> Any:
    import httpx

    params: Dict[str, Any] = {"limit": _PAGE_LIMIT}
    if after:
        params["after_id"] = after
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "accept": "application/json"}
    try:
        with client.stream("GET", url, params=params, headers=headers) as resp:
            status = resp.status_code
            if status in (401, 403):
                raise CatalogError("Anthropic rejected this API key.")
            if status == 429:
                raise CatalogError("Anthropic rate-limited the request. Try again in a moment.")
            if status != 200:
                raise CatalogError(f"Anthropic could not list the models (HTTP {status}).")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    raise CatalogError("Anthropic's model list was too large to read.")
    except (httpx.TimeoutException, httpx.TransportError):
        raise CatalogError("Could not reach Anthropic to list the models.") from None
    try:
        return json.loads(bytes(body))
    except ValueError:
        raise CatalogError("Anthropic returned an unexpected model list.") from None


def fetch(key: str) -> List[Dict[str, Any]]:
    """Every model ``key`` may use, in the API's order. Raises :class:`CatalogError`."""
    import httpx

    if not isinstance(key, str) or not key.strip():
        raise CatalogError("This credential has no API key.")
    url = models_url()
    models: List[Dict[str, Any]] = []
    seen = set()
    after: Optional[str] = None
    with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        for _ in range(_MAX_PAGES):
            page, more, last = parse_page(_get_page(client, url, key.strip(), after))
            for model in page:
                if model["value"] not in seen and len(models) < _MAX_MODELS:
                    seen.add(model["value"])
                    models.append(model)
            if not more or not last or last == after or len(models) >= _MAX_MODELS:
                break
            after = last
    return models


# ─── Cache ──────────────────────────────────────────────────────────────────


def _path() -> Optional[Path]:
    from app.core.config import settings

    if settings.data_dir is None:
        return None
    return Path(settings.data_dir) / _FILE_NAME


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _path()
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read the saved Anthropic model lists", exc_info=True)
        return
    if isinstance(data, dict):
        _cache.update({k: v for k, v in data.items()
                       if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("models"), list)})


def cached(kid: Optional[str]) -> Optional[Dict[str, Any]]:
    """The list last fetched with the key ``kid`` names, or None."""
    if not kid:
        return None
    with _lock:
        _load()
        return _cache.get(kid)


def store(kid: str, models: List[Dict[str, Any]], saved: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Keep ``kid``'s list while that key is still saved (``saved`` key ids);
    lists of keys no longer saved are dropped. None when ``kid`` is gone."""
    saved_ids = set(saved)
    snapshot = {"fetched_at": datetime.now(timezone.utc).isoformat(), "models": models}
    with _lock:
        _load()
        if kid not in saved_ids:
            logger.info("Anthropic model list for a key that is no longer saved ignored")
            return None
        for stale in [k for k in _cache if k not in saved_ids]:
            del _cache[stale]
        _cache[kid] = snapshot
        path = _path()
        if path is not None:
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(_cache, indent=1), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                logger.warning("Could not save the Anthropic model lists", exc_info=True)
    return snapshot


def _saved_key_ids() -> List[str]:
    from app.core import claude_accounts

    return [key_id(key) for _, key, _ in claude_accounts.saved_api_keys()]


def refresh_key(key: str) -> Dict[str, Any]:
    """Fetch and keep ``key``'s list; concurrent refreshes of one key share a
    fetch. Raises :class:`CatalogError`."""
    kid = key_id(key)
    with _lock:
        lock = _refresh_locks.setdefault(kid, threading.Lock())
    with lock:
        models = fetch(key)
        snapshot = store(kid, models, _saved_key_ids())
        if snapshot is None:
            raise CatalogError("This API key was removed while its models were listed.")
    logger.info("Anthropic lists %d models for a saved API key", len(models))
    if kid == active_key_id():
        _announce()
    return snapshot


def refresh_active_soon() -> None:
    """Fetch the selected key's list off the caller's thread when none is
    cached (a key was added or selected). Never raises."""
    def run() -> None:
        try:
            from app.core import claude_accounts

            key = claude_accounts.active_api_key()
            if key and cached(key_id(key)) is None:
                refresh_key(key)
        except Exception as exc:  # noqa: BLE001 - Settings' Refresh retries
            logger.info("Anthropic model list not fetched for the selected key: %s", exc)

    threading.Thread(target=run, name="claude-api-models", daemon=True).start()


# ─── The selected key's list in the pickers ─────────────────────────────────


def api_key_mode() -> bool:
    from app.core import claude_auth_mode

    return claude_auth_mode.current_mode() == claude_auth_mode.MODE_API_KEY


def active_key_id() -> Optional[str]:
    """``key_id`` of the selected key, None without one. The decrypted key is
    hashed once per stored ciphertext, not on every model lookup."""
    from app.core import claude_auth_mode
    from app.db.settings import get_system_setting_cached

    try:
        stored = get_system_setting_cached(claude_auth_mode.API_KEY_SETTING)
    except Exception:  # noqa: BLE001 - an unreadable setting has no key
        return None
    if not stored:
        return None
    if stored not in _active_memo:
        key = claude_auth_mode.stored_api_key()
        if not key:
            return None  # Locked storage: ask again once it opens.
        _active_memo.clear()
        _active_memo[stored] = key_id(key)
    return _active_memo[stored]


def catalog() -> Optional[Dict[str, Any]]:
    """The selected key's list in API-key mode, else None."""
    try:
        return cached(active_key_id()) if api_key_mode() else None
    except Exception:  # noqa: BLE001 - the static registry is the fallback
        return None


def entry(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """The selected key's list entry for ``value`` (API-key mode), or None."""
    snap = catalog() if value else None
    if not snap:
        return None
    return next((m for m in snap["models"] if m.get("value") == value), None)


def offered_entries() -> List[Dict[str, Any]]:
    """Checked versions the selected key lists, in the list's order (API-key mode)."""
    snap = catalog()
    if not snap:
        return []
    enabled = set(enabled_models())
    return [m for m in snap["models"] if m.get("value") in enabled and m.get("value") not in ALIASES]


# ─── Owner's choice ─────────────────────────────────────────────────────────


def enabled_models() -> List[str]:
    """Full ids the owner checked (default: none)."""
    from app.db.settings import get_system_setting_cached

    try:
        raw = get_system_setting_cached(ENABLED_SETTING)
        values = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001 - unreadable means nothing extra is offered
        logger.warning("Could not read the enabled Anthropic API models", exc_info=True)
        return []
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def set_model_enabled(value: str, enabled: bool) -> List[str]:
    """Check or uncheck one full id; returns the saved list. Other ids stay
    as they are, including ones the selected key does not list."""
    from app.db.settings import get_system_setting, set_system_setting

    value = (value or "").strip()
    if not value or value in ALIASES or not value.lower().startswith("claude-") or value.endswith("[1m]"):
        raise ValueError("Only a full Claude model id can be turned on or off.")
    with _enabled_lock:
        try:
            current = json.loads(get_system_setting(ENABLED_SETTING) or "[]")
        except ValueError:
            current = []
        clean = [v for v in current if isinstance(v, str) and v != value] if isinstance(current, list) else []
        if enabled:
            clean.append(value)
        set_system_setting(ENABLED_SETTING, json.dumps(clean))
    _announce()
    return clean


def _announce() -> None:
    """Open pickers re-read the registry."""
    from app.core import claude_cli_status

    claude_cli_status.announce_account_change()


# ─── Settings payload ───────────────────────────────────────────────────────


def account_payload(account_id: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What one saved key's Models page shows. Raises ``KeyError`` for an
    unknown account. Never includes the key or its hash."""
    from app.core import claude_accounts
    from app.core.model_registry import API_MODEL_IDS, MODELS

    key, active = claude_accounts.api_key_for(account_id)
    snap = snapshot if snapshot is not None else cached(key_id(key))
    return {
        "account_id": account_id,
        "active": active,
        "aliases": [{"value": m.value, "label": m.label, "runs": API_MODEL_IDS.get(m.value)} for m in MODELS],
        "catalog": ({"fetched_at": snap.get("fetched_at"),
                     "models": [{"value": m["value"], "label": m.get("label") or m["value"],
                                 "created_at": m.get("created_at")} for m in snap["models"]]}
                    if snap else None),
        "enabled": enabled_models(),
    }


def refresh_account(account_id: str) -> Dict[str, Any]:
    """Fetch one saved key's list now. ``KeyError`` for an unknown account,
    :class:`CatalogError` when Anthropic cannot answer."""
    from app.core import claude_accounts

    key, _ = claude_accounts.api_key_for(account_id)
    return account_payload(account_id, refresh_key(key))
