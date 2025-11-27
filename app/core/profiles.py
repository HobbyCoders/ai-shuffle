"""
Built-in agent profiles
"""

from typing import Dict, Any, List, Optional
from app.db import database


# Built-in profile definitions
# Model names for Claude Code SDK: opus, sonnet, haiku
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
            "setting_sources": ["project"]
        }
    }
}


def seed_builtin_profiles():
    """Seed the database with built-in profiles"""
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


def get_profile_or_builtin(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database, falling back to builtin definitions"""
    profile = database.get_profile(profile_id)
    if profile:
        return profile

    # Fallback to builtin (shouldn't happen after seeding, but safe)
    if profile_id in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[profile_id]

    return None
