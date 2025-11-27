"""
Built-in agent profiles
"""

from typing import Dict, Any, List, Optional
from app.db import database


# Built-in profile definitions
# Model names for Claude Code SDK: opus, sonnet, haiku
BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "simple-chat": {
        "id": "simple-chat",
        "name": "Simple Chat",
        "description": "Text-only responses, no tool access",
        "is_builtin": True,
        "config": {
            "model": "sonnet",
            "allowed_tools": [],
            "permission_mode": "default",
            "max_turns": 10,
            "system_prompt": None
        }
    },

    "code-reader": {
        "id": "code-reader",
        "name": "Code Reader",
        "description": "Read-only code analysis, no modifications",
        "is_builtin": True,
        "config": {
            "model": "sonnet",
            "allowed_tools": ["Read", "Glob", "Grep"],
            "permission_mode": "default",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            }
        }
    },

    "code-writer": {
        "id": "code-writer",
        "name": "Code Writer",
        "description": "Can read and write files, auto-approves edits",
        "is_builtin": True,
        "config": {
            "model": "sonnet",
            "allowed_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
            "permission_mode": "acceptEdits",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            }
        }
    },

    "full-claude": {
        "id": "full-claude",
        "name": "Full Claude Code",
        "description": "Complete Claude Code experience with all tools",
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
    },

    "data-extractor": {
        "id": "data-extractor",
        "name": "Data Extractor",
        "description": "Optimized for structured data extraction (JSON output)",
        "is_builtin": True,
        "config": {
            "model": "haiku",
            "allowed_tools": ["WebFetch"],
            "permission_mode": "bypassPermissions",
            "max_turns": 3,
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": "Return ONLY valid JSON. No markdown, no explanations, no code blocks. Pure JSON output."
            }
        }
    },

    "researcher": {
        "id": "researcher",
        "name": "Researcher",
        "description": "Web search and content analysis",
        "is_builtin": True,
        "config": {
            "model": "sonnet",
            "allowed_tools": ["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
            "permission_mode": "default",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": "Always cite sources and provide references."
            }
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
