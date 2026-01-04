"""
Built-in Subagent Definitions

These are pre-configured subagents that ship with the application.
They can be enabled per-profile but cannot be deleted by users.
Admins can edit them but can revert to original defaults.
"""

import logging
from typing import Dict, Any, List, Optional

from app.db import database

logger = logging.getLogger(__name__)


# ============================================================================
# Built-in Subagent Definitions
# ============================================================================

CHAT_HISTORY_SUMMARIZER_PROMPT = """You are a chat history summarizer agent. Your job is to read Claude SDK JSONL chat history files and extract KEY CONTEXT for the main agent to resume work.

## Your Task
When given a path to a `.jsonl` chat history file, scan it efficiently and provide a CONCISE, ACTIONABLE summary.

## JSONL Format
One JSON object per line:
- User messages: `{"type":"user", "message":{"role":"user","content":"..."}, ...}`
- Assistant messages: `{"type":"assistant", "message":{"role":"assistant","content":[blocks]}, ...}`
- Tool results: `{"type":"user", "toolUseResult":{...}, ...}`

SKIP: `queue-operation`, `file-history-snapshot`, messages with `"isMeta": true` or `"isSidechain": true`

## Reading Strategy
1. Use Grep to find key patterns first (don't read the whole file line by line):
   - Search for "TODO", "FIXME", "decision", "chose", "implemented", "created", "fixed"
   - Search for file paths mentioned (look for common extensions: .py, .ts, .svelte, .js)
   - Search for the user's last few messages to understand current state
2. Read the LAST 50-100 lines to see where things ended
3. Read the FIRST 20-30 lines to see the initial goal
4. Only read middle sections if you need specific context

## Output Format - BE BRIEF

**Goal**: [One sentence - what was the user trying to accomplish?]

**Current State**: [What was completed vs still in progress when the conversation ended?]

**Key Files Modified**:
- [file1.py] - [what was done]
- [file2.ts] - [what was done]

**Important Decisions**:
- [Decision 1 - brief]
- [Decision 2 - brief]

**Unfinished/Next Steps**:
- [What still needs to be done, if anything]

**User Preferences** (if any noted):
- [Only include if explicitly stated preferences that affect future work]

## CRITICAL RULES
- Keep the ENTIRE summary under 300 words
- NO verbose explanations - bullet points only
- Focus on WHAT WAS DONE and WHAT'S LEFT, not the journey
- Skip sections that have nothing to report
- The main agent needs to quickly understand context to continue, not read a novel
- If the conversation was simple (single task), the summary should be 2-3 sentences
"""

BUILTIN_SUBAGENTS: List[Dict[str, Any]] = [
    {
        "id": "chat-history-summarizer",
        "name": "Chat History Summarizer",
        "description": "Use this agent when the user references a chat history file (.jsonl from ~/.claude/projects/) or asks about a previous conversation. This agent reads and summarizes chat history efficiently without burning main context.",
        "prompt": CHAT_HISTORY_SUMMARIZER_PROMPT,
        "tools": ["Read", "Grep", "Glob"],  # Limited tools for reading files
        "model": "haiku",  # Use fast/cheap model for summarization
    },
]


# ============================================================================
# Seeding Functions
# ============================================================================

def seed_builtin_subagents(force_update: bool = False) -> Dict[str, str]:
    """
    Create or update built-in subagents in the database.

    Args:
        force_update: If True, update existing built-in subagents to latest definitions
                     (useful for upgrades). This updates the defaults but preserves
                     any admin customizations.

    Returns:
        Dict mapping subagent ID to action taken ("created", "updated", "exists")
    """
    results = {}

    for definition in BUILTIN_SUBAGENTS:
        subagent_id = definition["id"]
        existing = database.get_subagent(subagent_id)

        if not existing:
            # Create new built-in subagent
            database.create_subagent(
                subagent_id=subagent_id,
                name=definition["name"],
                description=definition["description"],
                prompt=definition["prompt"],
                tools=definition.get("tools"),
                model=definition.get("model"),
                is_builtin=True,
                store_defaults=True  # Store defaults for revert functionality
            )
            logger.info(f"Created built-in subagent: {subagent_id}")
            results[subagent_id] = "created"

        elif force_update and existing.get("is_builtin"):
            # Update the default values (but not the current values - preserve admin edits)
            _update_builtin_defaults(subagent_id, definition)
            logger.info(f"Updated defaults for built-in subagent: {subagent_id}")
            results[subagent_id] = "updated"

        else:
            results[subagent_id] = "exists"

    return results


def _update_builtin_defaults(subagent_id: str, definition: Dict[str, Any]) -> None:
    """Update only the default_* columns for a built-in subagent."""
    import json
    from datetime import datetime
    from app.db.database import get_db

    tools_json = json.dumps(definition.get("tools")) if definition.get("tools") else None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE subagents SET
                default_name = ?,
                default_description = ?,
                default_prompt = ?,
                default_tools = ?,
                default_model = ?,
                updated_at = ?
               WHERE id = ? AND is_builtin = 1""",
            (
                definition["name"],
                definition["description"],
                definition["prompt"],
                tools_json,
                definition.get("model"),
                datetime.utcnow().isoformat(),
                subagent_id
            )
        )


def get_builtin_subagent_ids() -> List[str]:
    """Get list of all built-in subagent IDs."""
    return [s["id"] for s in BUILTIN_SUBAGENTS]


def get_builtin_subagent_definition(subagent_id: str) -> Optional[Dict[str, Any]]:
    """Get the original definition for a built-in subagent."""
    for s in BUILTIN_SUBAGENTS:
        if s["id"] == subagent_id:
            return s.copy()
    return None


# ============================================================================
# System Prompt Instructions (for injection when agent is enabled)
# ============================================================================

CHAT_HISTORY_AGENT_INSTRUCTIONS = """
## Chat History References

When the user references a chat history file (`.jsonl` file from `~/.claude/projects/`):

1. **Use the chat-history-summarizer agent** - Don't try to read the file directly
2. **Invoke it with the file path** - The agent will efficiently extract key context
3. **Wait for the summary** - Use the returned summary to understand the conversation
4. **Tell the user what you found** - Summarize the relevant context from that conversation

This approach saves context tokens and provides consistent, structured summaries.
"""


def get_system_prompt_instructions(enabled_agent_ids: List[str]) -> str:
    """
    Get system prompt instructions for enabled built-in agents.

    Only returns instructions for built-in agents that are actually enabled.
    """
    instructions = []

    if "chat-history-summarizer" in enabled_agent_ids:
        instructions.append(CHAT_HISTORY_AGENT_INSTRUCTIONS)

    return "\n".join(instructions)
