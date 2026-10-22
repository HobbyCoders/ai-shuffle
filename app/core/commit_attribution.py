"""Owner-wide attribution preference, replicated via system_settings sync.

Read on the executing worker at session connect, including Task/subagent
children. The snapshot stays fixed for that connection's cached prompt.
"""

import logging

from app.db import database

logger = logging.getLogger(__name__)

SETTING_KEY = "commit_attribution"


def read() -> bool:
    """Enabled by default; only an explicit stored false disables attribution."""
    try:
        value = database.get_system_setting(SETTING_KEY)
    except Exception as exc:
        logger.warning(
            "Could not read commit attribution setting (%s); defaulting to enabled",
            type(exc).__name__,
        )
        return True
    if value == "false":
        return False
    if value is not None and value != "true":
        logger.warning(
            "Malformed commit attribution setting %r%s; defaulting to enabled",
            value[:80], "..." if len(value) > 80 else "",
        )
    return True


def write(enabled: bool) -> None:
    database.set_system_setting(SETTING_KEY, "true" if enabled else "false")
