"""One-time notice: sign in to the Claude Code CLI again after the upgrade.

Earlier versions ran their own Claude sign-in and wrote the resulting token
into the CLI's config dir. That token still works for the CLI, but it did not
come from the CLI's own sign-in, so this worker asks the owner once to run
`claude auth logout` and then `claude auth login`. The notice is
per worker, shown until dismissed, and never touches the token itself.

A worker needs the notice when the old in-app sign-in was used against its
database. The schema migration that removes that sign-in's storage (v89,
``app.db.database._retire_claude_accounts``) records the pending notice for
every worker that had an active-account pointer, before deleting anything,
and sets a database-wide flag. ``mark_if_needed`` runs at startup and turns
that flag into this worker's own pending notice, so a second worker sharing
the database gets it too. Nothing here reads the retired table.

The notice is about the Claude provider only: it is shown and logged only
while Claude is turned on for this worker (mode not ``off``). An owner who
turned Claude off, or uses other providers only, never sees it.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

PENDING = database.CLAUDE_RELOGIN_PENDING
DISMISSED = "dismissed"

LOGOUT_COMMAND = "claude auth logout"
LOGIN_COMMAND = "claude auth login"
MESSAGE = (
    "Claude: sign in to the Claude Code CLI again once. Claude's sign-in is now "
    "managed by the Claude Code CLI itself, not by AI Shuffle. To make sure its "
    "token comes from the CLI's own sign-in on Anthropic's page, run "
    f"`{LOGOUT_COMMAND}` and then `{LOGIN_COMMAND}` (Settings > Providers > "
    "Claude has Sign out and Sign in)."
)


def _key() -> str:
    return f"{database.CLAUDE_RELOGIN_NOTICE_PREFIX}{settings.effective_instance_id}"


def _legacy_sign_in_used() -> bool:
    """Whether the migration found and removed the old sign-in's storage."""
    return database.get_system_setting(database.CLAUDE_RELOGIN_NEEDED_MARKER) == "1"


def _claude_in_use() -> bool:
    """Whether Claude is turned on for this worker (any mode but ``off``)."""
    try:
        from app.core import claude_auth_mode

        return not claude_auth_mode.is_off()
    except Exception:  # noqa: BLE001 — unknown reads as "in use"
        return True


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
            if _claude_in_use():
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
    if not pending() or not _claude_in_use():
        return None
    return {"message": MESSAGE, "logout_command": LOGOUT_COMMAND, "login_command": LOGIN_COMMAND}
