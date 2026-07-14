"""
Unified message resolution — single entry point for the JSONL → PostgreSQL fallback chain.

V2: live render state rides the append-only session event feed; this resolver
serves only PERSISTED transcripts (SDK JSONL/transcript store, then the message
rows in Postgres as the final fallback).

Usage:
    from app.core.message_resolver import message_resolver

    result = message_resolver.resolve(session_id, sdk_session_id, working_dir)
    result.messages  # List[Dict] in normalized camelCase format
    result.source    # "jsonl" | "database"
    result.usage     # Optional usage dict
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Canonical message fields — the output contract of the resolver
# =============================================================================

# These are always present on every resolved message
_CORE_FIELDS = ("id", "role", "type", "content", "streaming")

# These are conditionally present (only included if truthy)
_OPTIONAL_FIELDS = (
    "subtype", "systemSubtype", "systemData",
    "toolName", "toolId", "toolInput", "toolResult", "toolStatus",
    "agentId", "agentType", "agentDescription", "agentPrompt", "agentStatus", "agentChildren",
    "metadata",
    "commandId", "commandName", "commandArgs",
)


@dataclass
class ResolvedMessages:
    """Result of message resolution."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "none"  # "jsonl", "database", "none"
    usage: Optional[Dict[str, Any]] = None


def _normalize_jsonl_message(m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a JSONL-parsed message to the canonical format.

    JSONL messages from parse_session_history() are already in camelCase
    and close to the canonical format. This just ensures consistency.
    """
    msg: Dict[str, Any] = {
        "id": m.get("id", ""),
        "role": m.get("role", "user"),
        "type": m.get("type") or "text",
        "content": m.get("content", ""),
        "streaming": False,
    }
    for f in _OPTIONAL_FIELDS:
        value = m.get(f)
        if value is not None:
            msg[f] = value
    return msg


def _normalize_db_message(m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a database message (snake_case) to the canonical camelCase format.

    Database messages have fields like tool_name, tool_input, tool_id (snake_case).
    The canonical format uses toolName, toolInput, toolId (camelCase).
    """
    role = m.get("role", "user")

    # Determine message type from available data
    msg_type = None
    if m.get("tool_name"):
        msg_type = "tool_use"
    elif role in ("tool_use", "tool_result"):
        msg_type = role
        role = "assistant"  # Normalize tool roles to assistant
    elif role == "assistant":
        msg_type = "text"

    msg: Dict[str, Any] = {
        "id": f"msg-{m.get('id', 0)}",
        "role": role,
        "type": msg_type,
        "content": m.get("content", ""),
        "streaming": False,
    }

    # Map snake_case DB fields to camelCase
    if m.get("tool_name"):
        msg["toolName"] = m["tool_name"]
    if m.get("tool_id"):
        msg["toolId"] = m["tool_id"]
    if m.get("tool_input"):
        msg["toolInput"] = m["tool_input"]
    if m.get("metadata"):
        msg["metadata"] = m["metadata"]
    if m.get("profile_id"):
        msg["profile_id"] = m["profile_id"]

    return msg


class MessageResolver:
    """Unified message resolution with JSONL → PostgreSQL fallback.

    All code that needs to load session messages should use this instead of
    implementing the fallback chain directly.
    """

    @staticmethod
    def resolve(
        session_id: str,
        sdk_session_id: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> ResolvedMessages:
        """Resolve messages for a session from the best available source.

        Priority:
        1. JSONL files (Claude SDK session history / transcript store)
        2. PostgreSQL database (fallback)

        Args:
            session_id: AI Shuffle session ID
            sdk_session_id: Claude SDK session UUID (for JSONL lookup)
            working_dir: Project working directory (for JSONL file location)

        Returns:
            ResolvedMessages with normalized messages and metadata
        """
        # --- Tier 1: JSONL files (persistent, written by Claude CLI) ---
        if sdk_session_id:
            jsonl_result = MessageResolver._try_jsonl(session_id, sdk_session_id, working_dir)
            if jsonl_result is not None:
                return jsonl_result

        # --- Tier 2: PostgreSQL database (fallback) ---
        return MessageResolver._try_database(session_id)

    # -------------------------------------------------------------------------
    # Private tier methods
    # -------------------------------------------------------------------------

    @staticmethod
    def _try_jsonl(
        session_id: str,
        sdk_session_id: str,
        working_dir: Optional[str],
    ) -> Optional[ResolvedMessages]:
        """Try to load messages from JSONL session files."""
        try:
            from app.core.jsonl_parser import parse_session_history

            messages, usage_data = parse_session_history(
                sdk_session_id,
                working_dir or _default_working_dir(),
            )
            if not messages:
                return None

            normalized = [_normalize_jsonl_message(m) for m in messages]

            usage = None
            if usage_data:
                usage = {
                    "input_tokens": usage_data.get("last_input_tokens", 0),
                    "output_tokens": usage_data.get("total_tokens_out", 0),
                    "cache_creation_tokens": usage_data.get("cache_creation_tokens", 0),
                    "cache_read_tokens": usage_data.get("cache_read_tokens", 0),
                    # Also keep raw totals for callers that need them
                    "total_tokens_in": usage_data.get("total_tokens_in", 0),
                    "total_tokens_out": usage_data.get("total_tokens_out", 0),
                    "last_input_tokens": usage_data.get("last_input_tokens", 0),
                }

            return ResolvedMessages(
                messages=normalized,
                source="jsonl",
                usage=usage,
            )
        except Exception as e:
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            return None

    @staticmethod
    def _try_database(session_id: str) -> ResolvedMessages:
        """Load messages from the PostgreSQL database (final fallback)."""
        from app.db import database

        db_messages = database.get_session_messages(session_id)
        normalized = [_normalize_db_message(dict(m)) for m in db_messages]

        return ResolvedMessages(
            messages=normalized,
            source="database",
        )


def _default_working_dir() -> str:
    """Get the default working directory from settings."""
    from app.core.config import settings
    return str(settings.workspace_dir)


def resolve_working_dir(session: Dict[str, Any]) -> str:
    """Resolve the working directory for a session, checking worktree first, then project.

    Priority:
    1. Worktree path — used whenever the resolved directory EXISTS on disk,
       regardless of the worktree row's ``status`` (same rule as
       ``_resolve_session_worktree_cwd``: a per-device sync sweep can flip a
       healthy row to 'deleted' in shared Postgres without the directory
       moving, and this auxiliary reader must keep reading history/JSONL from
       where the session actually runs). The session's backup ``worktree_path``
       column is tried when the row (or its path) is missing on disk. No status
       self-heal here — that lives in query_options only.
    2. Project path — only when no worktree directory exists on disk. This is
       an auxiliary reader (history/JSONL parsing, checkpoint flows), so a
       missing worktree dir degrades to the project
       dir instead of raising; it must never break history reads.
    3. Default workspace.
    """
    from app.db import database
    from app.core.project_utils import get_project_working_dir, resolve_existing_worktree_cwd
    from app.core.config import settings

    # V2 projects: the resolver is the single cwd authority for a session bound to
    # an origin-keyed project_v2. When it yields a path, use it; when it yields
    # None (no materializable checkout on this runner) fall straight through to the
    # default workspace ONLY — NEVER the legacy project-path walk below — so an
    # unmaterialized checkout surfaces honestly instead of silently running in a
    # stale legacy path. Sessions without project_v2_id, or flag OFF, take the
    # unchanged legacy chain.
    if settings.v2_projects and session.get("project_v2_id"):
        from app.core import project_resolver
        # PURE READ: resolve_existing_cwd never materializes (no git worktree
        # add / clone) — this is an auxiliary history/JSONL reader, not the spawn
        # path. Materialization is the spawn path's job (query_options).
        v2_cwd = project_resolver.resolve_existing_cwd(session)
        if v2_cwd is not None:
            return v2_cwd
        return _default_working_dir()

    # Check for worktree first — worktree sessions should resolve to the worktree path,
    # not the main project directory. Disk existence is the only gate:
    # resolve_existing_worktree_cwd tries the row's path, then the session's
    # backup worktree_path, per-candidate, and never raises.
    worktree_id = session.get("worktree_id")
    if worktree_id:
        worktree = None
        try:
            worktree = database.get_worktree(worktree_id)
        except Exception:  # noqa: BLE001 — a row lookup failure must not skip the backup path
            logger.debug(
                "resolve_working_dir: worktree row lookup failed; trying the "
                "session's backup worktree_path",
                exc_info=True,
            )
        resolved = resolve_existing_worktree_cwd(worktree, session)
        if resolved:
            return resolved

    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            return get_project_working_dir(project)
    return _default_working_dir()


# Module-level singleton
message_resolver = MessageResolver()
