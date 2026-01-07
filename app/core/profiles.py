"""
Profile and subagent utilities

No built-in profiles or subagents are seeded automatically.
Users must create their own profiles and projects before starting chats.
"""

from typing import Dict, Any, Optional
from app.db import database


# Default profile configuration template (for reference when creating new profiles)
# Model names for Claude Code SDK: opus, sonnet, haiku
#
# NOTE on setting_sources:
# - "user": Loads user-level settings from ~/.claude/settings.json
#   This is REQUIRED for auto-compact and other Claude Code defaults to work
# - "project": Loads project-level settings from .claude/settings.json in the working directory
# - "local": Loads local settings from .claude/settings.local.json
#
# Auto-compact is enabled by default in Claude Code's user settings.
# Without "user" in setting_sources, auto-compact won't trigger automatically.
DEFAULT_PROFILE_CONFIG: Dict[str, Any] = {
    "model": "sonnet",
    "allowed_tools": [],
    "permission_mode": "bypassPermissions",
    "system_prompt": {
        "type": "preset",
        "preset": "claude_code"
    },
    "setting_sources": ["user", "project"],
    "enabled_agents": []
}


def run_migrations():
    """Run any necessary database migrations on startup.

    This function is called on app startup to ensure data consistency.
    """
    # Migration: Ensure ALL profiles have is_builtin = False
    # This fixes any profiles that were created with is_builtin = True before the policy change
    all_profiles = database.get_all_profiles()
    for profile in all_profiles:
        if profile.get("is_builtin"):
            database.set_profile_builtin(profile["id"], False)

    # Migration: Ensure ALL subagents have is_builtin = False
    all_subagents = database.get_all_subagents()
    for subagent in all_subagents:
        if subagent.get("is_builtin"):
            database.set_subagent_builtin(subagent["id"], False)


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database"""
    return database.get_profile(profile_id)
