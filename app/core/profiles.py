"""
Built-in agent profiles
"""

from typing import Dict, Any, List, Optional
from app.db import database


# Built-in profile definitions
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
BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "claude-code": {
        "id": "claude-code",
        "name": "Claude Code",
        "description": "Full Claude Code experience with all tools",
        "is_builtin": True,
        "config": {
            "model": "sonnet",
            "allowed_tools": [],
            "permission_mode": "bypassPermissions",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            },
            # Include "user" to enable auto-compact and other Claude Code defaults
            "setting_sources": ["user", "project"]
        }
    }
}


def seed_builtin_profiles():
    """Seed the database with built-in profiles and update existing ones if needed"""
    for profile_id, profile_data in BUILTIN_PROFILES.items():
        existing = database.get_profile(profile_id)
        if not existing:
            database.create_profile(
                profile_id=profile_id,
                name=profile_data["name"],
                description=profile_data["description"],
                config=profile_data["config"],
                is_builtin=True
            )
        else:
            # Update existing builtin profiles if their config has changed
            # This ensures auto-compact fix propagates to existing installations
            existing_config = existing.get("config", {})
            new_config = profile_data["config"]

            # Check if setting_sources needs to be updated
            existing_sources = existing_config.get("setting_sources", [])
            new_sources = new_config.get("setting_sources", [])

            if set(existing_sources) != set(new_sources):
                # Update the profile config to include new setting_sources
                updated_config = {**existing_config, "setting_sources": new_sources}
                database.update_profile(
                    profile_id=profile_id,
                    config=updated_config,
                    allow_builtin=True  # Allow updating builtin profiles for migrations
                )


def get_profile_or_builtin(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database, falling back to builtin definitions"""
    profile = database.get_profile(profile_id)
    if profile:
        return profile

    # Fallback to builtin (shouldn't happen after seeding, but safe)
    if profile_id in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[profile_id]

    return None
