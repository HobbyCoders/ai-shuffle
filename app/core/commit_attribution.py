"""Owner-wide attribution preference, replicated via system_settings sync.

Read on the executing worker at session connect, including Task/subagent
children. The snapshot stays fixed for that connection's cached prompt.
"""

from app.db import database

SETTING_KEY = "commit_attribution"


def read() -> bool:
    """Enabled by default; only an explicit stored false disables attribution."""
    return database.get_system_setting(SETTING_KEY) != "false"


def write(enabled: bool) -> None:
    database.set_system_setting(SETTING_KEY, "true" if enabled else "false")
