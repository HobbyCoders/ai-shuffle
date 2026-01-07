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

    # Seed default templates if none exist
    _seed_default_templates()


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


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database"""
    return database.get_profile(profile_id)
