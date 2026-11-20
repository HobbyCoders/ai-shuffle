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

An explicit choice from Settings follows the owner through system-settings
sync. Startup defaults and older per-worker choices remain local until the
owner makes a shared choice. Cloud credentials remain local: the shared cloud
choice only authorizes the worker that configured it.

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
    return bool(stored_api_key())


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
    return {"mode": MODE_OFF}


def stored_mode(*, cached: bool = False) -> Optional[str]:
    selection = shared_selection(cached=cached)
    if selection is not None:
        return selection["mode"]
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    value = (getter(setting_key()) or "").strip()
    return value if value in MODES else None


def cloud_selection_is_local() -> bool:
    selection = shared_selection(cached=True)
    return (selection is None or selection["mode"] != MODE_CLOUD
            or selection.get("worker") == settings.effective_instance_id)


def default_mode(key_stored: bool, cli_signed_in: Optional[bool]) -> str:
    """The mode an install without an explicit choice starts in (never ``off``)."""
    if key_stored and not cli_signed_in:
        return MODE_API_KEY
    return MODE_SUBSCRIPTION


def _cli_signed_in_peek() -> Optional[bool]:
    from app.core import claude_cli_status

    status = claude_cli_status.peek()
    return None if status is None else bool(status.signed_in)


def current_mode() -> str:
    """The active mode. Reads the setting; before startup has recorded one it
    answers with the default without writing anything."""
    try:
        selection = shared_selection(cached=True)
        if selection is not None:
            return selection["mode"]
        mode = stored_mode(cached=True)
    except Exception:  # noqa: BLE001 — never substitute a local identity on a read failure
        logger.warning("Could not read the Claude Code auth mode; refusing Claude runs", exc_info=True)
        return MODE_OFF
    if mode == MODE_OFF:
        return mode
    from app.core import claude_cloud_auth

    if claude_cloud_auth.has_environment_auth():
        return MODE_CLOUD
    if mode:
        return mode
    try:
        return default_mode(has_stored_api_key(), _cli_signed_in_peek())
    except Exception:  # noqa: BLE001
        return MODE_SUBSCRIPTION


def spawn_fingerprint() -> str:
    """Non-secret pooled-session identity, without materializing CLI credentials."""
    from app.core import claude_cloud_auth

    from app.core import claude_subscriptions

    mode = current_mode()
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
        identity.append(database.get_system_setting(API_KEY_SETTING) or "")
    selection = shared_selection(cached=True)
    if selection is not None:
        identity.append(selection)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def is_off() -> bool:
    return current_mode() == MODE_OFF


def is_explicit() -> bool:
    try:
        return stored_mode() is not None
    except Exception:  # noqa: BLE001
        return False


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown Claude Code auth mode: {mode!r}. Use one of: {', '.join(MODES)}")
    selection = {"mode": mode}
    if mode == MODE_SUBSCRIPTION or (mode == MODE_API_KEY and current_mode() == MODE_SUBSCRIPTION):
        from app.core import claude_subscriptions

        # Remember the subscription we're leaving, so switching back on a peer
        # doesn't select its unrelated default. In subscription mode a missing
        # binding is explicit and blocks launch until the owner selects one.
        claude_subscriptions.ensure_shared_selection(required=(mode == MODE_SUBSCRIPTION))
    if mode == MODE_CLOUD:
        selection["worker"] = settings.effective_instance_id
    database.set_system_setting(SELECTION_SETTING, json.dumps(selection, separators=(",", ":")))
    if mode != MODE_CLOUD:
        # A credential file written for the CLI in cloud mode does not outlive
        # it; the saved settings stay, so switching back needs no re-entry.
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
    key = stored_api_key()
    mode = current_mode()
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
