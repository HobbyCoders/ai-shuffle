"""One-time notice: sign in to the Claude Code CLI again after the upgrade.

Earlier versions ran their own Claude sign-in and wrote the resulting token
into the CLI's config dir. That token still works for the CLI, but it did not
come from the CLI's own sign-in, so this worker asks the owner once to run
`claude auth logout` and then `claude auth login` (decision D2). The notice is
per worker, shown until dismissed, and never touches the token itself.

A worker needs the notice when the old in-app sign-in left traces in the
database: rows in the retired ``claude_accounts`` table or an active-account
pointer. ``mark_if_needed`` runs at startup and records the answer in a
local-only setting, so the notice survives the later removal of that table.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

PENDING = "pending"
DISMISSED = "dismissed"

LOGOUT_COMMAND = "claude auth logout"
LOGIN_COMMAND = "claude auth login"
MESSAGE = (
    "AI Shuffle no longer signs in to Claude for you. The Claude Code CLI now "
    "manages its own sign-in. To make sure the CLI's token comes from its own "
    "sign-in on Anthropic's page, sign out and in again once: run "
    f"`{LOGOUT_COMMAND}` and then `{LOGIN_COMMAND}` (Settings > Providers > "
    "Claude Code CLI has Sign out and Sign in)."
)


def _key() -> str:
    return f"claude_cli_relogin_notice:{settings.effective_instance_id}"


def _legacy_sign_in_used() -> bool:
    """Whether the retired in-app sign-in left rows or pointers behind."""
    from app.db.base import get_db

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS n FROM system_settings WHERE key LIKE 'claude_active_account:%'"
            )
            row = cursor.fetchone()
            if row and int(row["n"]):
                return True
    except Exception:  # noqa: BLE001 — best effort
        logger.debug("Legacy Claude pointer check skipped", exc_info=True)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM claude_accounts")
            row = cursor.fetchone()
            return bool(row and int(row["n"]))
    except Exception:  # noqa: BLE001 — the table may already be gone
        return False


def mark_if_needed() -> bool:
    """Record a pending notice for this worker when the old sign-in was used.

    Idempotent: a notice already pending or dismissed is left alone.
    Returns whether a notice is pending afterwards.
    """
    try:
        current = database.get_system_setting(_key())
        if current in (PENDING, DISMISSED):
            return current == PENDING
        if _legacy_sign_in_used():
            database.set_system_setting(_key(), PENDING)
            logger.warning("Claude Code CLI: %s", MESSAGE)
            return True
    except Exception:  # noqa: BLE001 — a notice must never block startup
        logger.debug("Claude re-login notice check skipped", exc_info=True)
    return False


def pending() -> bool:
    try:
        return database.get_system_setting(_key()) == PENDING
    except Exception:  # noqa: BLE001
        return False


def dismiss() -> None:
    database.set_system_setting(_key(), DISMISSED)


def payload() -> dict | None:
    """The notice for the status endpoints, or None."""
    if not pending():
        return None
    return {"message": MESSAGE, "logout_command": LOGOUT_COMMAND, "login_command": LOGIN_COMMAND}
