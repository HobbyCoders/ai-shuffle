"""Which credential Claude Code runs on: the user's subscription, their API key,
a cloud provider or gateway, or none.

AI Shuffle runs the stock Claude Code CLI in one of three modes, or not at all,
and the user picks it:

- ``subscription``: the CLI uses its own sign-in, which the user completed in
  Anthropic's flow (``claude auth login``). Nothing is injected; any Anthropic
  key in the host or profile environment is kept away from the CLI.
- ``api_key``: the Anthropic API key the user stored in Settings is passed to
  the CLI as ``ANTHROPIC_API_KEY``, and nothing else credential-like is.
- ``cloud``: "Cloud provider or gateway". The CLI runs on the one method the
  owner set up in Settings (Amazon Bedrock, Google Vertex AI, Microsoft Foundry
  or an LLM gateway), with only that method's allowlisted values
  (:mod:`app.core.claude_cloud_auth`). An explicit cloud selector or gateway
  in the worker environment also selects this mode, unless Claude is off.
- ``off``: Claude is turned off on this worker. Claude models leave the model
  pickers, every Claude turn is refused with a setup message, and no Anthropic
  credential reaches a CLI child. Only the user turns this on; the startup
  default never picks it.

Explicit Subscription and API key choices follow the owner. Off and Cloud
are worker-local overrides. Startup defaults remain local until there is a
shared choice.

Saved API keys follow the encrypted system-settings network policy. This
module never reads the CLI's own credential files; CLI sign-in state comes from
:mod:`app.core.claude_cli_status`, which asks ``claude auth status``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

MODE_SUBSCRIPTION = "subscription"
MODE_API_KEY = "api_key"
MODE_CLOUD = "cloud"
MODE_OFF = "off"
MODES = (MODE_SUBSCRIPTION, MODE_API_KEY, MODE_CLOUD, MODE_OFF)

MODE_LABELS = {
    MODE_SUBSCRIPTION: "Claude subscription",
    MODE_API_KEY: "Anthropic API key",
    MODE_CLOUD: "Cloud provider or gateway",
    MODE_OFF: "Off",
}

SETTING_PREFIX = "claude_auth_mode:"
SELECTION_SETTING = "claude_auth_selection"
API_KEY_SETTING = "anthropic_api_key"

# Environment names the CLI reads as an Anthropic credential. In each mode the
# ones that do not belong to it are removed from what the CLI child inherits.
SUBSCRIPTION_UNSET = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
API_KEY_UNSET = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
OFF_UNSET = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def setting_key() -> str:
    """This worker's own row: the mode follows the worker's credentials."""
    return SETTING_PREFIX + settings.effective_instance_id


def stored_api_key() -> Optional[str]:
    """The decrypted Anthropic API key from Settings, or None."""
    value = database.get_system_setting(API_KEY_SETTING)
    if not value:
        return None
    from app.core import encryption

    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning("The stored Anthropic API key cannot be decrypted: credential storage is locked")
            return None
        try:
            return encryption.decrypt_value(value) or None
        except Exception as exc:  # noqa: BLE001
            logger.error("The stored Anthropic API key could not be decrypted: %s", type(exc).__name__)
            return None
    return value


def has_stored_api_key() -> bool:
    mode, choice = runtime_choice()
    return bool(selected_api_key(choice) if mode == MODE_API_KEY else stored_api_key())


def key_last4(key: Optional[str]) -> Optional[str]:
    if not key or len(key) < 8:
        return None
    return key[-4:]


def shared_selection(*, cached: bool = False) -> Optional[dict]:
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    raw = getter(SELECTION_SETTING)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("mode") in MODES:
            return value
    except (TypeError, ValueError):
        pass
    # An unreadable shared selection is not permission to use a local default.
    return {"mode": MODE_OFF, "invalid": True}


def _stored_choice(*, cached: bool) -> tuple[Optional[str], Optional[dict]]:
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    local = (getter(setting_key()) or "").strip()
    if local in (MODE_OFF, MODE_CLOUD):
        return local, None
    choice = shared_selection(cached=cached)
    if choice:
        if choice.get("invalid"):
            return MODE_OFF, choice
        if choice["mode"] in (MODE_SUBSCRIPTION, MODE_API_KEY):
            return choice["mode"], choice
        # Honor legacy Cloud only on its originating worker. Old shared Off
        # rows have no origin, so they cannot be recovered as local choices.
        if choice["mode"] == MODE_CLOUD and choice.get("worker") == settings.effective_instance_id:
            return MODE_CLOUD, None
    return (local if local in MODES else None), None


def stored_mode(*, cached: bool = False) -> Optional[str]:
    return _stored_choice(cached=cached)[0]


def default_mode(key_stored: bool, cli_signed_in: Optional[bool]) -> str:
    """The mode an install without an explicit choice starts in (never ``off``)."""
    if key_stored and not cli_signed_in:
        return MODE_API_KEY
    return MODE_SUBSCRIPTION


def _cli_signed_in_peek() -> Optional[bool]:
    from app.core import claude_cli_status

    status = claude_cli_status.peek()
    return None if status is None else bool(status.signed_in)


def runtime_choice() -> tuple[str, Optional[dict]]:
    """Resolve the mode and its shared account from one selection snapshot."""
    from app.core import claude_cloud_auth

    try:
        mode, choice = _stored_choice(cached=True)
        if choice is not None or mode in (MODE_OFF, MODE_CLOUD):
            return mode, choice
        if claude_cloud_auth.has_environment_auth():
            return MODE_CLOUD, None
        return mode or default_mode(bool(stored_api_key()), _cli_signed_in_peek()), None
    except Exception:  # noqa: BLE001 — never substitute a local identity on a read failure
        logger.warning("Could not read the Claude Code auth mode; refusing Claude runs", exc_info=True)
        return MODE_OFF, {"invalid": True}


def current_mode() -> str:
    return runtime_choice()[0]


def selected_api_key(choice: Optional[dict]) -> Optional[str]:
    if choice is None:
        return stored_api_key()
    from app.core import claude_accounts

    key_id = choice.get("key_id")
    try:
        return claude_accounts.pinned_api_key(key_id) if key_id else None
    except claude_accounts.AccountError:
        return None


def spawn_fingerprint() -> str:
    """Non-secret pooled-session identity, without materializing CLI credentials."""
    from app.core import claude_cloud_auth

    from app.core import claude_subscriptions

    mode, choice = runtime_choice()
    # Which sign-in folder the CLI runs in (a saved subscription switch must
    # reconnect pooled sessions), as an opaque id, never an email.
    identity = [mode]
    if mode == MODE_SUBSCRIPTION:
        identity.append(claude_subscriptions.fingerprint_part())
    if mode == MODE_CLOUD:
        if claude_cloud_auth._use_environment():
            identity.append(json.dumps(claude_cloud_auth.environment_env_for_cli(), sort_keys=True))
        else:
            identity.extend(database.get_system_setting(key) or "" for key in (
                claude_cloud_auth.CONFIG_SETTING, claude_cloud_auth.SECRETS_SETTING,
            ))
    elif mode == MODE_API_KEY:
        identity.append(selected_api_key(choice) or "")
    if choice is not None:
        identity.append(choice)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def is_off() -> bool:
    return current_mode() == MODE_OFF


def is_explicit() -> bool:
    try:
        return stored_mode() is not None
    except Exception:  # noqa: BLE001
        return False


def _publish(mode: str, *, subscription: Optional[str], key_id: Optional[str], defer_key_pin: bool = False) -> None:
    # One replicated decision. The encrypted key list may lag behind it; a
    # reader must either find this exact ID there or refuse, never use a scalar.
    selection = {"mode": mode, "subscription": subscription}
    if not defer_key_pin:
        selection["key_id"] = key_id
    database.set_system_setting(SELECTION_SETTING, json.dumps(selection, separators=(",", ":")))


def _key_pin(mode: str, choice: dict) -> tuple[str | None, bool]:
    """Keep any explicit pin (including no key); otherwise migrate legacy storage."""
    from app.core import claude_accounts

    if "key_id" in choice or choice.get("mode") == MODE_API_KEY:
        return choice.get("key_id"), False
    try:
        return claude_accounts.pin_legacy_key(), False
    except claude_accounts.AccountError:
        if mode == MODE_API_KEY:
            raise
        # A subscription switch does not read a key at run time. Leave the
        # pin unset so an explicit later API-mode switch can migrate after unlock.
        return None, True


def publish_subscription(target: str) -> None:
    """Explicitly select a verified subscription without changing the auth mode."""
    choice = shared_selection() or {}
    mode = choice.get("mode")
    if mode not in (MODE_SUBSCRIPTION, MODE_API_KEY):
        mode = MODE_API_KEY if current_mode() == MODE_API_KEY else MODE_SUBSCRIPTION
    key_id, deferred = _key_pin(mode, choice)
    _publish(mode, subscription=target, key_id=key_id, defer_key_pin=deferred)


def update_shared_key(account_id: Optional[str]) -> None:
    from app.core import claude_subscriptions

    choice = shared_selection()
    if choice is not None and choice["mode"] in (MODE_API_KEY, MODE_SUBSCRIPTION):
        _publish(choice["mode"], subscription=claude_subscriptions.shared_target(choice, read_choice=False),
                 key_id=account_id)


def clear_shared_key_if_selected(account_id: str) -> None:
    """Delete changes the pin only if it still names this exact key.

    Do not republish a stale mode or subscription over a concurrent choice.
    Missing list rows already fail closed, including if this update loses.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    raw = database.get_system_setting(SELECTION_SETTING)
    try:
        choice = json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        return
    if not (isinstance(choice, dict) and choice.get("mode") in (MODE_API_KEY, MODE_SUBSCRIPTION)
            and choice.get("key_id") == account_id):
        return
    choice["key_id"] = None
    with database.get_db() as conn:
        conn.cursor().execute(
            """UPDATE system_settings SET value = %s, updated_at = %s, revision = %s
               WHERE key = %s AND value = %s""",
            (json.dumps(choice, separators=(",", ":")), datetime.now(timezone.utc).isoformat(),
             str(uuid4()), SELECTION_SETTING, raw),
        )
    database.invalidate_system_setting_memo(SELECTION_SETTING)


def set_mode(mode: str, *, key_id: Optional[str] = None) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown Claude Code auth mode: {mode!r}. Use one of: {', '.join(MODES)}")
    if mode in (MODE_OFF, MODE_CLOUD):
        database.set_system_setting(setting_key(), mode)
    else:
        from app.core import claude_subscriptions

        target = claude_subscriptions.shared_target()
        if target is None:
            # A pending choice requires the separate, verified Use this sign-in action.
            target = claude_subscriptions.legacy_binding()
        deferred = False
        if key_id is None:
            key_id, deferred = _key_pin(mode, shared_selection() or {})
        _publish(mode, subscription=target, key_id=key_id, defer_key_pin=deferred)
        database.set_system_setting(setting_key(), mode)
        from app.core import claude_cli_status

        claude_cli_status.invalidate()
    if mode != MODE_CLOUD:
        from app.core import claude_cloud_auth

        claude_cloud_auth.remove_materialized_files()
    logger.info("Claude Code auth mode set to %s", mode)
    return mode


def ensure_mode_recorded(cli_signed_in: Optional[bool]) -> str:
    """Write the default mode once for an install that has none (startup)."""
    mode = stored_mode()
    if mode:
        return mode
    mode = default_mode(has_stored_api_key(), cli_signed_in)
    database.set_system_setting(setting_key(), mode)
    logger.info("Claude Code auth mode was not set; recorded %s for this worker", mode)
    return mode


def summary(cli: Optional[dict] = None) -> dict:
    """What Settings shows: the mode, the CLI account, the key's last four.

    ``cli`` is the probe result as a dict (``ClaudeCliStatus.to_dict()``); when
    omitted, the last cached probe is used without running the CLI.
    """
    if cli is None:
        from app.core import claude_cli_status

        status = claude_cli_status.peek()
        cli = status.to_dict() if status is not None else {}
    mode, choice = runtime_choice()
    key = selected_api_key(choice) if mode == MODE_API_KEY else stored_api_key()
    signed_in = bool(cli.get("signed_in"))
    return {
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "explicit": is_explicit(),
        "modes": [{"value": m, "label": MODE_LABELS[m]} for m in MODES],
        "cli": {
            "installed": bool(cli.get("installed")),
            "signed_in": signed_in,
            "email": cli.get("email") if signed_in else None,
            "org_name": cli.get("org_name") if signed_in else None,
            "plan": cli.get("plan") if signed_in else None,
            "auth_method_label": cli.get("auth_method_label") if signed_in else None,
            "error": cli.get("error"),
        },
        "api_key": {"set": bool(key), "last4": key_last4(key)},
        "cloud": cloud_status(),
    }


def cloud_status() -> dict:
    """Which cloud method is saved and whether its required values are present.

    Names and presence only; never a value."""
    from app.core import claude_cloud_auth

    try:
        return claude_cloud_auth.status()
    except Exception:  # noqa: BLE001 — a status read must not break Settings
        logger.warning("Could not read the Claude cloud provider status", exc_info=True)
        return {"method": None, "method_label": None, "configured": False, "ready": False,
                "missing": [], "storage_locked": False}
