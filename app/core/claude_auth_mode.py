"""Which credential Claude Code runs on: the user's subscription or their API key.

AI Shuffle runs the stock Claude Code CLI in exactly one of two modes, and the
user picks it:

- ``subscription``: the CLI uses its own sign-in, which the user completed in
  Anthropic's flow (``claude auth login``). Nothing is injected; any Anthropic
  key in the host or profile environment is kept away from the CLI.
- ``api_key``: the Anthropic API key the user stored in Settings is passed to
  the CLI as ``ANTHROPIC_API_KEY``, and nothing else credential-like is.

The mode is an explicit per-worker setting (``claude_auth_mode:<instance id>``).
When both a CLI sign-in and a stored key exist, the setting decides; the app
never switches modes on its own. An install that predates the setting gets a
one-time default at startup (:func:`ensure_mode_recorded`): ``api_key`` when a
key is stored and the CLI reports no sign-in, otherwise ``subscription``. That
default is written down, so a later change in credentials cannot flip it.

The stored key stays on the worker where it was entered. This
module never reads the CLI's own credential files; CLI sign-in state comes from
:mod:`app.core.claude_cli_status`, which asks ``claude auth status``.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

MODE_SUBSCRIPTION = "subscription"
MODE_API_KEY = "api_key"
MODES = (MODE_SUBSCRIPTION, MODE_API_KEY)

MODE_LABELS = {
    MODE_SUBSCRIPTION: "Claude subscription",
    MODE_API_KEY: "Anthropic API key",
}

SETTING_PREFIX = "claude_auth_mode:"
API_KEY_SETTING = "anthropic_api_key"

# Environment names the CLI reads as an Anthropic credential. In each mode the
# ones that do not belong to it are removed from what the CLI child inherits.
SUBSCRIPTION_UNSET = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
API_KEY_UNSET = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


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


def stored_mode(*, cached: bool = False) -> Optional[str]:
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    value = (getter(setting_key()) or "").strip()
    return value if value in MODES else None


def default_mode(key_stored: bool, cli_signed_in: Optional[bool]) -> str:
    """The mode an install without an explicit choice starts in."""
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
        mode = stored_mode(cached=True)
    except Exception:  # noqa: BLE001 — a DB hiccup must not pick a mode silently
        logger.warning("Could not read the Claude Code auth mode; assuming subscription", exc_info=True)
        return MODE_SUBSCRIPTION
    if mode:
        return mode
    try:
        return default_mode(has_stored_api_key(), _cli_signed_in_peek())
    except Exception:  # noqa: BLE001
        return MODE_SUBSCRIPTION


def is_explicit() -> bool:
    try:
        return stored_mode() is not None
    except Exception:  # noqa: BLE001
        return False


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown Claude Code auth mode: {mode!r}. Use one of: {', '.join(MODES)}")
    database.set_system_setting(setting_key(), mode)
    logger.info("Claude Code auth mode set to %s", mode)
    return mode


def ensure_mode_recorded(cli_signed_in: Optional[bool]) -> str:
    """Write the default mode once for an install that has none (startup)."""
    mode = stored_mode()
    if mode:
        return mode
    mode = default_mode(has_stored_api_key(), cli_signed_in)
    set_mode(mode)
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
    }
