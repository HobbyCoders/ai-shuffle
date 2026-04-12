"""
Profile and subagent utilities

Seeds a built-in "Default AI" profile on first run. This profile is used
for all internal AI generation tasks (commit messages, PR descriptions,
profile/subagent generation, etc.) when no user profile is selected.
Built-in profiles can be edited but not deleted.
"""

import logging
from typing import Dict, Any, Optional
from app.db import database

logger = logging.getLogger(__name__)


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
    "permission_mode": "default",
    "system_prompt": {
        "type": "preset",
        "preset": "claude_code"
    },
    "setting_sources": ["user", "project"],
    "enabled_agents": []
}


# ============================================================================
# Built-in Default AI Profile
# ============================================================================
# This profile controls the model and settings used for all internal AI
# generation tasks: commit messages, PR descriptions, profile/subagent
# generation, and any other AI-powered feature that doesn't use a user profile.
# Users can edit these settings but cannot delete the profile.

DEFAULT_AI_PROFILE_ID = "default-ai"

DEFAULT_AI_PROFILE: Dict[str, Any] = {
    "id": DEFAULT_AI_PROFILE_ID,
    "name": "Default AI",
    "description": "Built-in profile used for AI-powered features like commit message generation, "
                   "PR descriptions, profile/subagent generation, and other internal AI tasks. "
                   "Edit this profile to change the model and settings used by these features.",
    "config": {
        "model": "sonnet",
        "permission_mode": "bypassPermissions",
        "thinking_mode": "default",
        "system_prompt": {
            "type": "custom",
            "preset": "claude_code",
            "content": "",
        },
        "allowed_tools": [],
        "include_partial_messages": False,
        "continue_conversation": False,
        "fork_session": False,
        "enable_file_checkpointing": False,
        "enabled_agents": [
            "chat-history-summarizer",
            "code-simplifier",
            "Explore",
            "frontend-designer",
            "general-purpose",
            "Plan",
        ],
        "ai_tools": {
            "image_generation": True,
            "image_editing": True,
            "image_reference": True,
            "video_generation": True,
            "image_to_video": True,
            "video_extend": True,
            "text_to_3d": True,
            "image_to_3d": True,
            "tts": True,
            "stt": True,
        },
    },
}


def get_default_ai_profile() -> Optional[Dict[str, Any]]:
    """Get the default AI profile from the database.

    Returns the profile dict or None if it hasn't been seeded yet.
    """
    return database.get_profile(DEFAULT_AI_PROFILE_ID)


def get_default_ai_model() -> str:
    """Get the model configured in the default AI profile.

    Falls back to 'sonnet' if the profile doesn't exist or has no model set.
    """
    profile = get_default_ai_profile()
    if profile:
        return profile.get("config", {}).get("model", "sonnet") or "sonnet"
    return "sonnet"


def run_migrations():
    """Run any necessary database migrations on startup.

    This function is called on app startup to ensure data consistency.
    """
    _migrate_instance_id()

    # Seed the built-in default AI profile
    _seed_default_ai_profile()

    # Seed default templates if none exist
    _seed_default_templates()


def _migrate_instance_id():
    """Add instance_id column to projects table for multi-instance isolation."""
    from app.db.base import column_exists, get_db
    with get_db() as conn:
        cursor = conn.cursor()
        if not column_exists(cursor, "projects", "instance_id"):
            cursor.execute("ALTER TABLE projects ADD COLUMN instance_id TEXT")
            logger.info("Added instance_id column to projects table")


def _seed_default_ai_profile():
    """Ensure the built-in Default AI profile exists.

    Creates it on first run. If it already exists, ensures it's marked as built-in.
    Does NOT overwrite user edits to the profile config.
    """
    existing = database.get_profile(DEFAULT_AI_PROFILE_ID)
    if existing:
        # Ensure it's marked as built-in (in case of upgrade from older version)
        if not existing.get("is_builtin"):
            database.set_profile_builtin(DEFAULT_AI_PROFILE_ID, True)
            logger.info("Marked existing default-ai profile as built-in")
        return

    # Create the default AI profile
    database.create_profile(
        profile_id=DEFAULT_AI_PROFILE["id"],
        name=DEFAULT_AI_PROFILE["name"],
        description=DEFAULT_AI_PROFILE["description"],
        config=DEFAULT_AI_PROFILE["config"],
        is_builtin=True,
    )
    logger.info("Seeded built-in Default AI profile")


# Default templates to seed on first run
DEFAULT_TEMPLATES = [
    {
        "id": "tmpl-code-review",
        "name": "Code Review",
        "description": "Review code for bugs, security issues, and improvements",
        "prompt": "Please review this code for bugs, security issues, and improvements:\n\n",
        "icon": "magnifying_glass_tilted_right",
        "category": "coding"
    },
    {
        "id": "tmpl-explain-code",
        "name": "Explain Code",
        "description": "Get a step-by-step explanation of code",
        "prompt": "Please explain what this code does step by step:\n\n",
        "icon": "books",
        "category": "coding"
    },
    {
        "id": "tmpl-debug-help",
        "name": "Debug Help",
        "description": "Get help debugging an error",
        "prompt": "I'm getting this error. Help me debug:\n\n",
        "icon": "bug",
        "category": "debugging"
    },
    {
        "id": "tmpl-write-tests",
        "name": "Write Tests",
        "description": "Generate unit tests for code",
        "prompt": "Please write unit tests for this code:\n\n",
        "icon": "test_tube",
        "category": "coding"
    },
    {
        "id": "tmpl-refactor",
        "name": "Refactor Code",
        "description": "Improve code structure and readability",
        "prompt": "Please refactor this code to improve its structure, readability, and maintainability:\n\n",
        "icon": "hammer_and_wrench",
        "category": "coding"
    },
    {
        "id": "tmpl-documentation",
        "name": "Write Documentation",
        "description": "Generate documentation for code",
        "prompt": "Please write clear documentation for this code including function descriptions, parameters, and usage examples:\n\n",
        "icon": "memo",
        "category": "documentation"
    }
]


def _seed_default_templates():
    """Seed default templates if none exist."""
    existing = database.get_all_templates()
    if len(existing) > 0:
        # Templates already exist, don't seed
        return

    for tmpl in DEFAULT_TEMPLATES:
        database.create_template(
            template_id=tmpl["id"],
            name=tmpl["name"],
            description=tmpl.get("description"),
            prompt=tmpl["prompt"],
            icon=tmpl.get("icon"),
            category=tmpl.get("category"),
            is_builtin=True
        )


def get_builtin_profile_defaults(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get the original seed defaults for a built-in profile.

    Returns the default name, description, and config for a built-in profile,
    or None if the profile_id doesn't correspond to a known built-in profile.
    """
    if profile_id == DEFAULT_AI_PROFILE_ID:
        return {
            "name": DEFAULT_AI_PROFILE["name"],
            "description": DEFAULT_AI_PROFILE["description"],
            "config": DEFAULT_AI_PROFILE["config"],
        }
    return None


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database"""
    return database.get_profile(profile_id)
