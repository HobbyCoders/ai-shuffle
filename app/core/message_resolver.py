"""
Unified message resolution — single entry point for the LiveSessionStore → JSONL → PostgreSQL fallback chain.

This eliminates the 4 duplicate fallback implementations that previously existed in:
- api/sessions.py get_session (REST)
- api/sessions.py get_live_session (polling)
- api/websocket.py load_session handler
- api/websocket.py run_query initialization

Usage:
    from app.core.message_resolver import message_resolver

    result = message_resolver.resolve(session_id, sdk_session_id, working_dir)
    result.messages  # List[Dict] in normalized camelCase format
    result.source    # "live_store" | "jsonl" | "database"
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
    source: str = "none"  # "live_store", "jsonl", "database", "none"
    is_streaming: bool = False
    last_update_id: int = 0
    initial_message_count: int = 0
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
    """Unified message resolution with LiveSessionStore → JSONL → PostgreSQL fallback.

    All code that needs to load session messages should use this instead of
    implementing the fallback chain directly.
    """

    @staticmethod
    def resolve(
        session_id: str,
        sdk_session_id: Optional[str] = None,
        working_dir: Optional[str] = None,
        *,
        check_live_store: bool = True,
        force_jsonl: bool = False,
    ) -> ResolvedMessages:
        """Resolve messages for a session from the best available source.

        Priority:
        1. LiveSessionStore (if check_live_store=True and not force_jsonl)
        2. JSONL files (Claude SDK session history)
        3. PostgreSQL database (fallback)

        Args:
            session_id: AI Shuffle session ID
            sdk_session_id: Claude SDK session UUID (for JSONL lookup)
            working_dir: Project working directory (for JSONL file location)
            check_live_store: Whether to check LiveSessionStore first
            force_jsonl: Skip LiveSessionStore even if check_live_store=True

        Returns:
            ResolvedMessages with normalized messages and metadata
        """
        result = ResolvedMessages()

        # --- Tier 1: LiveSessionStore (in-memory, during/after streaming) ---
        if check_live_store and not force_jsonl:
            live_result = MessageResolver._try_live_store(session_id)
            if live_result is not None:
                return live_result

        # --- Tier 2: JSONL files (persistent, written by Claude CLI) ---
        if sdk_session_id:
            jsonl_result = MessageResolver._try_jsonl(session_id, sdk_session_id, working_dir)
            if jsonl_result is not None:
                return jsonl_result

        # --- Tier 3: PostgreSQL database (fallback) ---
        return MessageResolver._try_database(session_id)

    @staticmethod
    def resolve_for_streaming_init(
        session_id: str,
        sdk_session_id: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve messages specifically for seeding LiveSessionStore.start_streaming().

        This is a lighter version used by run_query initialization.
        Checks ended (non-streaming) LiveSessionStore first, then JSONL.
        Returns raw messages suitable for start_streaming() — no DB fallback
        (DB messages may have different IDs that conflict with JSONL-based sessions).
        """
        from app.core.live_session_store import live_session_store

        # Check for ended-but-not-yet-cleaned-up session in live store
        existing = live_session_store.get_session(session_id)
        if existing and not existing.is_streaming and existing.messages:
            return [m.to_dict() for m in existing.messages]

        # Fall back to the durable transcript.
        if sdk_session_id:
            from app.core.config import settings as _cfg

            # postgres_only: JSONL on disk is not the source of truth (and may be
            # absent on this machine) — the session store is. Seed from the store
            # so historical assistant turns aren't dropped while the new turn
            # streams (mirrors the run_query seed + /live read paths).
            if _cfg.session_store_mode == "postgres_only":
                try:
                    from app.core.pg_session_store import _load_entries_by_session_id_sync
                    from app.core.jsonl_parser import parse_session_history_from_store

                    def _store_loader(sid: str, subpath: Optional[str] = None) -> list:
                        return _load_entries_by_session_id_sync(sid, subpath)

                    messages, _usage = parse_session_history_from_store(
                        sdk_session_id,
                        working_dir or _default_working_dir(),
                        _store_loader,
                    )
                    return messages
                except Exception as e:
                    logger.warning(f"Store-backed history load failed for live store init: {e}")

            try:
                from app.core.jsonl_parser import parse_session_history
                messages, _usage = parse_session_history(
                    sdk_session_id,
                    working_dir or _default_working_dir(),
                )
                return messages
            except Exception as e:
                logger.warning(f"Failed to load history for live store init: {e}")

        return []

    @staticmethod
    def resolve_delta(
        session_id: str,
        since_update_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Resolve delta updates from LiveSessionStore for efficient polling.

        Returns None if there are no updates since the given update_id,
        or the delta dict from LiveSessionStore.get_session_since().
        """
        from app.core.live_session_store import live_session_store

        if not live_session_store.has_session(session_id):
            return None

        if since_update_id > 0:
            return live_session_store.get_session_since(session_id, since_update_id)
        else:
            return live_session_store.get_session_dict(session_id)

    # -------------------------------------------------------------------------
    # Private tier methods
    # -------------------------------------------------------------------------

    @staticmethod
    def _try_live_store(session_id: str) -> Optional[ResolvedMessages]:
        """Try to load messages from the LiveSessionStore."""
        from app.core.live_session_store import live_session_store

        if not live_session_store.has_session(session_id):
            return None

        live_data = live_session_store.get_session_dict(session_id)
        if not live_data or not live_data.get("messages"):
            return None

        return ResolvedMessages(
            messages=live_data["messages"],
            source="live_store",
            is_streaming=live_data.get("is_streaming", False),
            last_update_id=live_data.get("last_update_id", 0),
            initial_message_count=live_data.get("initial_message_count", 0),
            usage=live_data.get("usage"),
        )

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
    1. Worktree path (if session is linked to an active worktree)
    2. Project path
    3. Default workspace
    """
    from app.db import database
    from app.core.project_utils import get_project_working_dir, get_worktree_working_dir

    # Check for worktree first — worktree sessions should resolve to the worktree path,
    # not the main project directory.
    worktree_id = session.get("worktree_id")
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if worktree and worktree.get("status") == "active":
            return get_worktree_working_dir(worktree)

    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            return get_project_working_dir(project)
    return _default_working_dir()


# Module-level singleton
message_resolver = MessageResolver()
