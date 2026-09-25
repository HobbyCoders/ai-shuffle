"""
Profile and subagent utilities

The app ships no agents at all: a fresh install starts empty and the first
thing its owner does is create the agent they actually want. No agent is
built-in or protected either — the old "Default AI" agent existed to configure
internal AI generation, and that binding is now a setting (Settings ->
Providers -> Generated text).
"""

import logging
from typing import Dict, Any, Optional
from app.db import database

logger = logging.getLogger(__name__)


# The retired built-in. Nothing creates or protects it any more; the migration
# below only unlocks the row an existing install still has, so its owner can
# delete it like any other agent.
RETIRED_DEFAULT_AI_ID = "default-ai"


def run_migrations():
    """Run any necessary database migrations on startup.

    This function is called on app startup to ensure data consistency.
    """
    _migrate_instance_id()

    # Unlock the old built-in on an install that still has one, so its owner
    # can delete it like any other agent.
    _retire_default_ai_agent()

    # Seed default templates if none exist
    _seed_default_templates()


def _retire_default_ai_agent():
    """Retire the built-in "Default AI" agent on an install that still has one.

    Two one-time steps, both safe to repeat:

    1. Carry its model / effort onto the text-generation setting when that
       setting is still unset. The agent used to BE the internal-generation
       control, so without this an upgrading install would silently start
       writing its commit messages on a model nobody chose.
    2. Clear ``is_builtin``, which is what made the row undeletable. The agent
       is now ordinary — the app neither creates nor depends on it — so its
       owner can keep it as a chat agent or delete it.

    The row itself is deliberately NOT deleted: sessions reference it (deleting
    would blank their agent), and it may have been edited into something the
    user actually chats with. Its ``config.model`` is left alone too — it still
    backs the agent's legacy native subagent binding
    (``normalized_provider_models``).
    """
    from app.core.generation_options import (
        TEXT_GENERATION_EFFORT_KEY,
        TEXT_GENERATION_MODEL_KEY,
        VALID_EFFORTS,
    )

    try:
        profile = database.get_profile(RETIRED_DEFAULT_AI_ID)
        if not profile:
            return

        config = profile.get("config") or {}
        model = str(config.get("model") or "").strip()
        setting_unset = not (
            database.get_system_setting(TEXT_GENERATION_MODEL_KEY) or ""
        ).strip()
        if model and setting_unset:
            provider = str(config.get("provider") or "").strip()
            # Stored in the picker's own shape, so a provider-bound generation
            # model round-trips through the codec the composer uses.
            value = f"prov:{provider}:{model}" if provider else model
            database.set_system_setting(TEXT_GENERATION_MODEL_KEY, value)

            effort = str(config.get("effort") or "").strip()
            if effort in VALID_EFFORTS:
                database.set_system_setting(TEXT_GENERATION_EFFORT_KEY, effort)

            logger.info(
                "Carried the retired Default AI agent's model (%s) onto the "
                "text-generation setting",
                value,
            )

        if profile.get("is_builtin"):
            database.set_profile_builtin(RETIRED_DEFAULT_AI_ID, False)
            logger.info(
                "Retired the built-in Default AI agent - it is now an ordinary, "
                "deletable agent"
            )
    except Exception:  # noqa: BLE001 - a startup nicety, never a boot blocker
        logger.warning("Could not retire the Default AI agent", exc_info=True)


def _migrate_instance_id():
    """Add instance_id column to projects table for multi-instance isolation."""
    from app.db.base import column_exists, get_db
    with get_db() as conn:
        cursor = conn.cursor()
        if not column_exists(cursor, "projects", "instance_id"):
            cursor.execute("ALTER TABLE projects ADD COLUMN instance_id TEXT")
            logger.info("Added instance_id column to projects table")


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
