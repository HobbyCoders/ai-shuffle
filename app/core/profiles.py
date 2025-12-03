"""
Built-in agent profiles with subagent support
"""

from typing import Dict, Any, List, Optional
from app.db import database


# Built-in subagent templates
# These are default subagents that come with the claude-code profile
# Users can edit/customize them freely
BUILTIN_SUBAGENTS: Dict[str, Dict[str, Any]] = {
    "research-assistant": {
        "description": "Use for exploring codebases, finding patterns, or answering 'how does X work?' questions.",
        "prompt": """You are Claude Code, Anthropic's official CLI for Claude.

---

You are a file search specialist for Claude AI. You excel at thoroughly navigating and exploring codebases.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use Glob for broad file pattern matching
- Use Grep for searching file contents with regex
- Use Read when you know the specific file path you need to read
- Use Bash for file operations like copying, moving, or listing directory contents
- Adapt your search approach based on the thoroughness level specified by the caller
- Return file paths as absolute paths in your final response
- For clear communication, avoid using emojis
- Do not create any files, or run bash commands that modify the user's system state in any way

Complete the user's search request efficiently and report your findings clearly.


Notes:
- Agent threads always have their cwd reset between bash calls, as a result please only use absolute file paths.
- In your final response always share relevant file names and code snippets. Any file paths you return in your response MUST be absolute. Do NOT use relative paths.
- For clear communication with the user the assistant MUST avoid using emojis.""",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "haiku"
    },
    "code-reviewer": {
        "description": "Use PROACTIVELY when reviewing code changes. Expert at security, performance, and best practices.",
        "prompt": """You are a senior code reviewer. Analyze code for:
- Security vulnerabilities (injection, auth issues, data exposure)
- Performance bottlenecks
- Code quality and maintainability
- Testing coverage gaps

Be specific with line numbers and provide actionable fixes.
Return file paths as absolute paths.
Avoid using emojis.""",
        "tools": ["Read", "Grep", "Glob"],
        "model": "sonnet"
    },
    "test-generator": {
        "description": "Use when writing tests for functions, components, or APIs.",
        "prompt": """You are a test engineering specialist. Generate comprehensive tests that:
- Cover happy paths and edge cases
- Test error handling
- Use the project's existing test framework and patterns
- Are maintainable and well-documented

Return file paths as absolute paths.
Avoid using emojis.""",
        "tools": ["Read", "Write", "Grep", "Glob", "Bash"],
        "model": "sonnet"
    },
    "bug-investigator": {
        "description": "Use when debugging errors, crashes, or unexpected behavior.",
        "prompt": """You are a debugging specialist. When investigating bugs:
- Trace the error to its root cause
- Examine related code paths
- Check for similar issues in the codebase
- Propose minimal fixes

Focus on finding the actual cause, not just symptoms.
Return file paths as absolute paths.
Avoid using emojis.""",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "sonnet"
    }
}


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
        "description": "Full Claude Code experience with all tools and subagents",
        "is_builtin": False,  # All profiles are editable
        "config": {
            "model": "sonnet",
            "allowed_tools": [],
            "permission_mode": "bypassPermissions",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            },
            # Include "user" to enable auto-compact and other Claude Code defaults
            "setting_sources": ["user", "project"],
            # Default subagents
            "agents": BUILTIN_SUBAGENTS
        }
    }
}


def seed_builtin_profiles():
    """Seed the database with built-in profiles and update existing ones if needed"""
    for profile_id, profile_data in BUILTIN_PROFILES.items():
        existing = database.get_profile(profile_id)
        if not existing:
            # Create new profile with all default subagents
            database.create_profile(
                profile_id=profile_id,
                name=profile_data["name"],
                description=profile_data["description"],
                config=profile_data["config"],
                is_builtin=False  # All profiles are editable
            )
        else:
            # Update existing profiles if their config needs updates
            existing_config = existing.get("config", {})
            new_config = profile_data["config"]
            needs_update = False
            updated_config = {**existing_config}

            # Check if setting_sources needs to be updated
            existing_sources = existing_config.get("setting_sources", [])
            new_sources = new_config.get("setting_sources", [])
            if set(existing_sources) != set(new_sources):
                updated_config["setting_sources"] = new_sources
                needs_update = True

            # Add default subagents if none exist (migration for existing profiles)
            if not existing_config.get("agents") and new_config.get("agents"):
                updated_config["agents"] = new_config["agents"]
                needs_update = True

            # Update is_builtin to False so all profiles are editable
            if existing.get("is_builtin"):
                needs_update = True

            if needs_update:
                database.update_profile(
                    profile_id=profile_id,
                    config=updated_config,
                    allow_builtin=True  # Allow updating for migrations
                )
                # Also update is_builtin flag to False
                if existing.get("is_builtin"):
                    database.set_profile_builtin(profile_id, False)


def get_profile_or_builtin(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database, falling back to builtin definitions"""
    profile = database.get_profile(profile_id)
    if profile:
        return profile

    # Fallback to builtin (shouldn't happen after seeding, but safe)
    if profile_id in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[profile_id]

    return None
