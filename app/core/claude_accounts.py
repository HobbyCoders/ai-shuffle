"""Saved Claude accounts: Anthropic API keys and subscription sign-ins.

API keys keep the existing active-key scalar; both key settings follow the
encrypted shared-system-settings policy. Subscription sign-ins are per worker
and live in :mod:`app.core.claude_subscriptions` (each saved sign-in is its own
CLI config folder; the app never reads, copies or stores CLI credentials).
This module is the facade the accounts API calls.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable, Optional
from uuid import uuid4

from app.core import claude_subscriptions as subscriptions, encryption
from app.db import database

API_SETTING = "anthropic_api_key"
API_LIST_SETTING = "claude_saved_api_keys"
_lock = threading.RLock()


class AccountError(RuntimeError):
    """A safe account-management error with a stable API code and HTTP status."""

    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.code, self.status = code, status


def _error(code: str, detail: str, status: int = 409):
    raise AccountError(code, detail, status)


def _require_storage():
    if not encryption.is_encryption_ready():
        _error("storage_locked", "Unlock credential storage before managing Claude accounts.", 503)


def _read_setting(key: str):
    value = database.get_system_setting(key)
    if not value:
        return None
    if encryption.is_encrypted(value):
        _require_storage()
        try:
            return encryption.decrypt_value(value)
        except Exception:
            _error("storage_unreadable", "Saved Claude credentials could not be decrypted.", 503)
    return value


def _encrypt(value):
    _require_storage()
    try:
        return encryption.encrypt_value(value)
    except RuntimeError:
        _require_storage()
        raise


def _write_setting(key: str, value: dict):
    database.set_system_setting(key, _encrypt(json.dumps(value)))


def _keys():
    raw = _read_setting(API_LIST_SETTING)
    bundle = {"accounts": [], "active_id": None}
    if raw:
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value["accounts"], list):
                raise ValueError()
            bundle["accounts"] = value["accounts"]
        except (ValueError, KeyError, TypeError):
            _error("storage_unreadable", "Saved Anthropic API keys could not be read.", 503)
    active = active_api_key()
    if active:
        account = next((a for a in bundle["accounts"] if a["key"] == active), None)
        if account is None:
            # A legacy save or peer scalar may arrive before its list. Display
            # the credential actually used by the CLI, not a stale pointer.
            account = {
                "id": "legacy-" + hashlib.sha256(active.encode()).hexdigest()[:24],
                "label": "Anthropic API key",
                "key": active,
            }
            bundle["accounts"].append(account)
        bundle["active_id"] = account["id"]
    return bundle


def active_api_key() -> str | None:
    """Read the decrypted active key, including the legacy scalar setting."""
    return _read_setting(API_SETTING)


def _save_keys(bundle, *, update_active=False):
    # List first: a failed active-key write never loses either saved key.
    _write_setting(API_LIST_SETTING, {"accounts": bundle["accounts"]})
    if update_active:
        active = next(
            (a["key"] for a in bundle["accounts"] if a["id"] == bundle["active_id"]), None
        )
        if active:
            database.set_system_setting(API_SETTING, _encrypt(active))
        else:
            database.delete_system_setting(API_SETTING)


def _label(value, default=""):
    if not isinstance(value, str) or len(value.strip()) > 120:
        _error("invalid_label", "Use a label of at most 120 characters.", 400)
    return value.strip() or default


def add_api_key(key: str, label: str = "", activate: bool = True) -> str:
    """Save or relabel a key, optionally select it, and return its account ID."""
    if not isinstance(key, str) or not key.strip():
        _error("invalid_key", "API key cannot be empty.", 400)
    key = key.strip()
    with _lock:
        _require_storage()
        bundle = _keys()
        account = next((a for a in bundle["accounts"] if a["key"] == key), None)
        if account is None:
            account = {"id": str(uuid4()), "label": _label(label, "Anthropic API key"), "key": key}
            bundle["accounts"].append(account)
        elif label:
            account["label"] = _label(label)
        if activate:
            bundle["active_id"] = account["id"]
        _save_keys(bundle, update_active=activate)
        return account["id"]


def subscription_support() -> dict[str, Any]:
    """Whether this worker can keep several Claude subscription sign-ins."""
    return subscriptions.support()


def _subscription_call(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except subscriptions.SubscriptionError as exc:
        raise AccountError(exc.code, str(exc), exc.status) from None


def change_account(kind: str, account_id: str, action: str, label: str = "") -> None:
    """Rename, activate or delete a saved account without changing Claude auth mode."""
    if kind == "subscription":
        return _subscription_call(subscriptions.change, account_id, action, label)
    if kind != "api_key":
        _error("invalid_kind", "Use api_key or subscription.", 400)
    with _lock:
        _require_storage()
        bundle = _keys()
        account = next((a for a in bundle["accounts"] if a["id"] == account_id), None)
        if account is None:
            _error("not_found", "Saved Claude account not found.", 404)
        was_active = bundle["active_id"] == account_id
        if action == "rename":
            account["label"] = _label(label)
        elif action == "activate":
            bundle["active_id"] = account_id
        elif action == "delete":
            if was_active:
                bundle["active_id"] = None
            bundle["accounts"] = [a for a in bundle["accounts"] if a["id"] != account_id]
        else:
            _error("invalid_action", "Unknown account action.", 400)
        _save_keys(
            bundle, update_active=action == "activate" or (action == "delete" and was_active)
        )


def deactivate_api_key() -> None:
    """Clear the selected scalar key while retaining every saved account."""
    with _lock:
        bundle = _keys()
        bundle["active_id"] = None
        _save_keys(bundle, update_active=True)


def accounts_payload() -> dict[str, Any]:
    """Return masked account summaries, selection and safe storage errors."""
    from app.api.settings_crypto import mask_api_key

    with _lock:
        result = {
            "api_key": [],
            "subscription": [],
            "subscription_support": subscription_support(),
            "storage_locked": not encryption.is_encryption_ready(),
            "error": None,
            "login_pending": False,
            "login": None,
            "recovery_required": False,
        }
        if result["subscription_support"]["supported"]:
            # Plain worker-local data: listed even while key storage is locked.
            result.update(subscriptions.payload())
        try:
            bundle = _keys()
        except AccountError as exc:
            return {**result, "storage_locked": True, "error": exc.code}
        result["api_key"] = [
            {
                "id": a["id"],
                "label": a["label"],
                "active": a["id"] == bundle["active_id"],
                "masked_key": mask_api_key(a["key"]),
                "last4": a["key"][-4:] if len(a["key"]) >= 8 else None,
            }
            for a in bundle["accounts"]
        ]
        return result


def save_current_login(label: str = "") -> str:
    """Save the CLI's current default sign-in as a named subscription entry."""
    return _subscription_call(subscriptions.save_current, label)


async def mutate_subscription(function: Callable[..., Any], *args: Any) -> Any:
    """Run a subscription change off the loop, then retire idle CLIs on the old sign-in."""
    from app.db.async_database import run_sync

    result = await run_sync(function, *args)
    await subscriptions.reap_idle_pooled_clis()
    return result


async def start_login(*, external: bool = False) -> dict:
    """Start a CLI sign-in into a fresh folder (``add another``)."""
    from app.db.async_database import run_sync

    return await run_sync(_subscription_call, subscriptions.start_login)


async def cancel_login() -> Optional[str]:
    """Stop a pending ``add another`` sign-in and remove its folder.

    Returns the new entry's id when the CLI had already finished signing in,
    in which case the sign-in was kept and made active instead.
    """
    from app.db.async_database import run_sync

    return await run_sync(_subscription_call, subscriptions.cancel_login)
