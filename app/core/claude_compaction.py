"""Owner-wide Claude compaction preference, replicated via system_settings sync.

On: Claude sessions on the Claude Code CLI compact through AI Shuffle (the
mod's mechanical route, ``fluid.engine.claude_mod_compaction``) instead of the
CLI's own summary. Off: the CLI compacts as it does without the mod's route.
Read on the executing worker at session connect, so a change applies to the
next Claude session start or reconnect. A CLI older than the mod's minimum
version runs without the mod, which is the same as off.
"""

import logging

from app.db import database

logger = logging.getLogger(__name__)

SETTING_KEY = "claude_compaction"


def read() -> bool:
    """Enabled by default; only an explicit stored false turns it off."""
    try:
        value = database.get_system_setting(SETTING_KEY)
    except Exception as exc:
        logger.warning(
            "Could not read Claude compaction setting (%s); defaulting to enabled",
            type(exc).__name__,
        )
        return True
    if value == "false":
        return False
    if value is not None and value != "true":
        logger.warning(
            "Malformed Claude compaction setting %r%s; defaulting to enabled",
            value[:80], "..." if len(value) > 80 else "",
        )
    return True


def write(enabled: bool) -> None:
    database.set_system_setting(SETTING_KEY, "true" if enabled else "false")
