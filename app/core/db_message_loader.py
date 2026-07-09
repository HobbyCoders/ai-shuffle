"""
DB-first message loader for session history.

Converts session_messages DB rows into the same frontend message format
that parse_session_history() returns from JSONL files, but ~100x faster
(indexed DB query + simple transform vs. 1200-line JSONL parse).

Fallback chain: live_session_store → DB → JSONL → SDK.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.jsonl_parser import _apply_task_notification_status
from app.core.system_message_filter import is_hidden_system_subtype
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
    # Build a tool_id → subagent index so a persisted terminal
    # <task-notification> user row can repair the matching card (status + usage +
    # result) — mirrors the JSONL reload path in jsonl_parser.
    subagent_index: Dict[str, int] = {}

    for row in rows:
        msg = _transform_row(row)
        if msg is None:
            continue

        # Track tool_use messages by tool_id for result merging
        if msg.get("type") == "tool_use":
            tool_id = msg.get("toolId")
            if tool_id:
                tool_use_index[tool_id] = len(messages)

        # Track subagent cards by their launch tool_id for task-notification repair
        if msg.get("type") == "subagent":
            tool_id = msg.get("toolId")
            if tool_id:
                subagent_index[tool_id] = len(messages)

        # Merge tool_result into preceding tool_use
        if row["role"] == "tool_result":
            tool_id = (row.get("metadata") or {}).get("tool_id")
            if tool_id and tool_id in tool_use_index:
                idx = tool_use_index[tool_id]
                messages[idx]["toolResult"] = row.get("content", "")
                messages[idx]["toolStatus"] = "complete"
                continue
            # No matching tool_use — emit as standalone (shouldn't happen often)

        # A terminal <task-notification> persisted as a user row carries the
        # backgrounded sub-agent's final status/usage/result. Flip + enrich the
        # already-built card (the notification always follows its launch), then
        # let the user row itself fall through to append (filtered by the UI).
        if row["role"] == "user":
            content = row.get("content")
            if isinstance(content, str) and "<task-notification>" in content:
                _apply_task_notification_status(content, messages, subagent_index)

        messages.append(msg)

    return messages, True


def _transform_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Transform a single DB row into frontend message format."""
    role = row["role"]
    meta = row.get("metadata") or {}
    row_id = row.get("id", 0)
    created_at = row.get("created_at", "")
    # PostgreSQL returns timestamp columns as datetime objects (SQLite returned
    # ISO strings). A raw datetime here is not JSON-serializable, which breaks
    # every stdlib json.dumps() consumer of these messages — the JSONL parse
    # cache (pre_seed/store) and the SyncEngine WS broadcast — even though the
    # FastAPI HTTP response encodes it fine. Normalize to an ISO string so the
    # timestamp matches the HTTP path and stays serializable everywhere.
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()

    base: Dict[str, Any] = {
        "id": f"msg-{row_id}",
        "streaming": False,
        "metadata": {"timestamp": created_at},
    }

    # Surface the persisted JSONL entry uuid (when present) so the UI can rewind
    # by uuid directly instead of falling back to fragile text matching. User
    # rows are backfilled lazily during a rewind, so this is usually only set on
    # assistant rows and previously-rewound sessions — harmless when absent.
    jsonl_uuid = row.get("jsonl_uuid")
    if jsonl_uuid:
        base["uuid"] = jsonl_uuid

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
        # Normalize children snake_case → camelCase. The persistence layer
        # stores child entries as snake_case dicts (tool_name, tool_input, …),
        # but the frontend expects camelCase keys (toolName, toolInput, …).
        raw_children = meta.get("agent_children", []) or []
        normalized_children = []
        for c in raw_children:
            if not isinstance(c, dict):
                continue
            normalized_children.append({
                "id": c.get("id") or c.get("tool_id"),
                "type": c.get("type", "tool_use"),
                "content": c.get("content", "") or "",
                "streaming": bool(c.get("streaming", False)),
                "toolName": c.get("toolName") or c.get("tool_name"),
                "toolId": c.get("toolId") or c.get("tool_id"),
                "toolInput": c.get("toolInput") or c.get("tool_input"),
                "toolResult": c.get("toolResult") or c.get("tool_result"),
                "toolStatus": c.get("toolStatus") or c.get("tool_status") or "complete",
            })

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
            "agentChildren": normalized_children,
            "agentId": meta.get("agent_id"),
            "agentUsage": meta.get("agent_usage"),
        }

    if role == "team_member":
        raw_children = meta.get("agent_children", []) or []
        normalized_children = []
        for c in raw_children:
            if not isinstance(c, dict):
                continue
            normalized_children.append({
                "id": c.get("id") or c.get("tool_id"),
                "type": c.get("type", "tool_use"),
                "content": c.get("content", "") or "",
                "streaming": False,
                "toolName": c.get("toolName") or c.get("tool_name"),
                "toolId": c.get("toolId") or c.get("tool_id"),
                "toolInput": c.get("toolInput") or c.get("tool_input"),
                "toolResult": c.get("toolResult") or c.get("tool_result"),
                "toolStatus": c.get("toolStatus") or c.get("tool_status") or "complete",
            })
        return {
            **base,
            "role": "assistant",
            "content": row.get("content", ""),
            "type": "team_member",
            "teamName": meta.get("team_name"),
            "teammateName": meta.get("teammate_name"),
            "teammateSessionId": meta.get("teammate_session_id"),
            "teammatePid": meta.get("teammate_pid"),
            "teammateStatus": meta.get("teammate_status", "completed"),
            "agentPrompt": meta.get("agent_prompt"),
            "agentChildren": normalized_children,
            "agentUsage": meta.get("agent_usage"),
        }

    if role == "system":
        subtype = meta.get("subtype", "")
        # Drop internal/transient system subtypes that live streaming routes
        # into other UI elements (thinking counter, subagent progress) and
        # never renders as a card. Filtering on reload matches live behavior.
        if is_hidden_system_subtype(subtype):
            return None
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
