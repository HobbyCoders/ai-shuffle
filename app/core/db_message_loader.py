"""
DB-first message loader for session history.

Converts session_messages DB rows into the same frontend message format
that parse_session_history() returns from JSONL files, but ~100x faster
(indexed DB query + simple transform vs. 1200-line JSONL parse).

Fallback chain: live_session_store → DB → JSONL → SDK.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.db import database

logger = logging.getLogger(__name__)


def load_messages_from_db(
    session_id: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Load messages from DB and transform to frontend format.

    Returns:
        (messages, has_db_messages) — messages list and whether DB had data.
        If has_db_messages is False, caller should fall back to JSONL.
    """
    rows = database.get_session_messages(session_id)
    if not rows:
        return [], False

    messages = []
    # Build a tool_id → tool_use index for merging tool_results
    tool_use_index: Dict[str, int] = {}

    for row in rows:
        msg = _transform_row(row)
        if msg is None:
            continue

        # Track tool_use messages by tool_id for result merging
        if msg.get("type") == "tool_use":
            tool_id = msg.get("toolId")
            if tool_id:
                tool_use_index[tool_id] = len(messages)

        # Merge tool_result into preceding tool_use
        if row["role"] == "tool_result":
            tool_id = (row.get("metadata") or {}).get("tool_id")
            if tool_id and tool_id in tool_use_index:
                idx = tool_use_index[tool_id]
                messages[idx]["toolResult"] = row.get("content", "")
                messages[idx]["toolStatus"] = "complete"
                continue
            # No matching tool_use — emit as standalone (shouldn't happen often)

        messages.append(msg)

    return messages, True


def _transform_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Transform a single DB row into frontend message format."""
    role = row["role"]
    meta = row.get("metadata") or {}
    row_id = row.get("id", 0)
    created_at = row.get("created_at", "")

    base: Dict[str, Any] = {
        "id": f"msg-{row_id}",
        "streaming": False,
        "metadata": {"timestamp": created_at},
    }

    if role == "user":
        return {
            **base,
            "role": "user",
            "content": row["content"],
            "type": None,
            "profile_id": row.get("profile_id"),
        }

    if role == "command":
        return {
            **base,
            "role": "user",
            "content": row["content"],
            "type": "command",
            "commandName": meta.get("command_name"),
            "commandArgs": meta.get("command_args"),
            "profile_id": row.get("profile_id"),
        }

    if role == "assistant":
        return {
            **base,
            "role": "assistant",
            "content": row["content"],
            "type": "text",
            "metadata": {
                **base["metadata"],
                "model": meta.get("model"),
            },
        }

    if role == "thinking":
        return {
            **base,
            "role": "assistant",
            "content": row["content"],
            "type": "thinking",
            "metadata": {
                **base["metadata"],
                "model": meta.get("model"),
            },
        }

    if role == "tool_use":
        return {
            **base,
            "role": "assistant",
            "content": "",
            "type": "tool_use",
            "toolName": row.get("tool_name"),
            "toolId": meta.get("tool_id"),
            "toolInput": row.get("tool_input"),
            "toolStatus": "running",  # Will be updated if tool_result exists
        }

    if role == "tool_result":
        # Normally merged into tool_use by caller; standalone fallback
        return {
            **base,
            "role": "assistant",
            "content": row.get("content", ""),
            "type": "tool_result",
            "toolName": row.get("tool_name"),
            "toolId": meta.get("tool_id"),
        }

    if role == "subagent":
        return {
            **base,
            "role": "assistant",
            "content": row["content"],
            "type": "subagent",
            "toolName": row.get("tool_name"),
            "toolId": meta.get("tool_id"),
            "agentType": meta.get("agent_type"),
            "agentDescription": meta.get("agent_description"),
            "agentPrompt": meta.get("agent_prompt"),
            "agentStatus": meta.get("agent_status", "completed"),
            "agentChildren": meta.get("agent_children", []),
            "agentId": meta.get("agent_id"),
        }

    if role == "system":
        subtype = meta.get("subtype", "")
        return {
            **base,
            "role": "system",
            "content": row["content"],
            "type": "system",
            "subtype": subtype,
            "systemSubtype": subtype,
            "systemData": meta.get("system_data", {}),
        }

    # Unknown role — pass through
    return {
        **base,
        "role": role,
        "content": row.get("content", ""),
        "type": None,
    }
