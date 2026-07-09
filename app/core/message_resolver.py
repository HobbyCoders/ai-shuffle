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

        # Check for ended-but-not-yet-cleaned-up session in live store.
        #
        # This tier-1 snapshot can be LESS complete than the durable store
        # transcript when the turn that produced it crashed mid-stream or was
        # seeded from empty history (the orphaned-session symptom). heal_seed_
        # against_store (below) applies the SAME "longer wins" completeness heal
        # the /live read path uses (sessions.py get_live_session), so a truncated
        # snapshot can't propagate a short seed into the new turn's buffer.
        existing = live_session_store.get_session(session_id)
        if existing and not existing.is_streaming and existing.messages:
            snapshot = [m.to_dict() for m in existing.messages]
            return heal_seed_against_store(snapshot, sdk_session_id, working_dir)

        # Fall back to the durable transcript.
        if sdk_session_id:
            from app.core.config import settings as _cfg

            # postgres_only: JSONL on disk is not the source of truth (and may be
            # absent on this machine) — the session store is. Seed from the store
            # so historical assistant turns aren't dropped while the new turn
            # streams (mirrors the run_query seed + /live read paths).
            if _cfg.session_store_mode == "postgres_only":
                # use_cache=False: the turn-start seed must NOT inherit a
                # stale _store_parse_cache. After a rewind the cache is
                # invalidated, but a poisoned seed here (from before the fix)
                # would otherwise get reseeded forever via handle_done's
                # "fresh >= seed" guard. Turn start runs once per turn, so a
                # fresh store parse here is an acceptable cost.
                messages = _parse_store_transcript(
                    sdk_session_id, working_dir, use_cache=False
                )
                if messages is not None:
                    return messages
                # Store parse FAILED (raised). Do NOT silently seed [] — a failed
                # parse here used to drop the whole transcript for the duration of
                # the turn (only the current user prompt + new output survived the
                # frontend full-replace). Fall through to the JSONL reader; if that
                # also yields nothing we return [] as before, but a transient store
                # hiccup no longer wipes visible history.

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


def _parse_store_transcript(
    sdk_session_id: str,
    working_dir: Optional[str],
    *,
    use_cache: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """Parse the durable Postgres store transcript for a session.

    Returns the parsed messages, or ``None`` when the parse RAISES. The
    None-on-failure contract is load-bearing: callers must be able to tell a
    genuinely-empty transcript ([]) apart from a transient store hiccup (None) so
    a failure never silently produces a shorter seed than the durable transcript.

    postgres_only-only — the store is the single source of truth there.
    """
    try:
        from app.core.pg_session_store import _load_entries_by_session_id_sync
        from app.core.jsonl_parser import parse_session_history_from_store

        def _store_loader(sid: str, subpath: Optional[str] = None) -> list:
            return _load_entries_by_session_id_sync(sid, subpath)

        messages, _usage = parse_session_history_from_store(
            sdk_session_id,
            working_dir or _default_working_dir(),
            _store_loader,
            use_cache=use_cache,
        )
        return messages
    except Exception as e:
        logger.warning(f"Store-backed history load failed for live store init: {e}")
        return None


def heal_seed_against_store(
    initial_messages: List[Dict[str, Any]],
    sdk_session_id: Optional[str],
    working_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Heal a turn-start seed against the durable store transcript ("longer wins").

    This is the SEED-path analogue of the "longer wins" completeness heal on the
    non-streaming /live read path (see ``app/api/sessions.py`` get_live_session).
    Every seed source can be LESS complete than the authoritative Postgres store
    transcript the client already renders:

      * a store-parse failure that swallowed to ``[]`` (empty seed),
      * a NULL sdk_session_id turn whose later reuse propagates a short snapshot,
      * a tier-1 in-memory snapshot left crash-truncated by a mid-stream OOM/kill.

    When the durable transcript is MORE complete than ``initial_messages``, prefer
    it — exactly as the read-path heal does — so historical assistant/tool turns
    aren't dropped when the new turn's compaction poll forces a frontend full
    replace. When the seed is longer/equal (genuinely ahead of the not-yet-flushed
    mirror, or JSONL-mode) it wins, preserving tier-1 semantics.

    "Longer" is NOT sufficient on its own, though. The two sources have a
    systematic granularity mismatch: the live buffer coalesces a streamed turn's
    text into ONE message, while the store parse emits one row per text block per
    assistant message (``text-{uuid}-{n}``) plus system-ish user rows the buffer
    never held. After ``interrupt_for_query`` the durable transcript can therefore
    be numerically longer while MISSING the interrupted turn's unflushed tail that
    ``end_streaming`` deliberately preserved in memory. A bare longer-wins would
    destroy that tail (and the id-family switch would duplicate cards client-side).

    So the heal is ANCHORED: the durable transcript may only win when it is longer
    AND carries the snapshot's anchor uuid — the last message in the snapshot with
    a truthy transcript uuid (populated by ``_backfill_message_uuids`` before
    parking). Special cases:

      * empty seed → durable wins (the store-parse-swallowed / NULL-sdk case),
      * seed has NO transcript uuids at all → fall back to plain longer-wins
        (preserves the genuine crash-truncation heal this function was built for).

    No-ops (returns ``initial_messages`` unchanged) outside postgres_only, without
    a usable ``sdk_session_id``, or when the store parse fails — a heal must never
    make the seed SHORTER than what the caller already resolved.
    """
    if not sdk_session_id:
        return initial_messages

    from app.core.config import settings as _cfg

    if _cfg.session_store_mode != "postgres_only":
        return initial_messages

    # use_cache=False: mirror resolve_for_streaming_init's rationale — the seed
    # must not inherit a stale/poisoned _store_parse_cache entry.
    durable = _parse_store_transcript(sdk_session_id, working_dir, use_cache=False)
    if durable is None:
        # Parse failed — keep whatever the caller resolved (never shrink the seed).
        return initial_messages

    # Empty seed → the durable transcript is unambiguously more complete (this is
    # the store-parse-swallowed-to-[] / NULL-sdk-session case). Nothing to anchor.
    if not initial_messages:
        if durable:
            logger.debug(
                "heal_seed_against_store: empty seed — healing to durable "
                "transcript (%d msgs)",
                len(durable),
            )
            return durable
        return initial_messages

    if len(durable) <= len(initial_messages):
        # Seed is already at least as long — never shrink it.
        return initial_messages

    # Durable is numerically longer, but "longer" is NOT sufficient on its own.
    # The granularity between the two sources is systematically mismatched:
    #
    #   * the live buffer coalesces a streamed turn's text into ONE message
    #     (append_text), while
    #   * the store parse emits one row PER text block per assistant message
    #     (text-{uuid}-{n}) plus system-ish user rows the buffer never held.
    #
    # So after interrupt_for_query, the durable transcript can be longer while
    # MISSING the interrupted turn's unflushed tail that end_streaming
    # deliberately preserved in memory. A bare "longer wins" would then destroy
    # that tail and, via the id-family switch (parser text-{uuid}-{n} vs live
    # text-{12hex}), spawn duplicate cards client-side.
    #
    # Guard: durable may only win if it is longer AND is a superset of the
    # snapshot's tail — it must carry the LAST transcript uuid the snapshot holds
    # (end_streaming's _backfill_message_uuids populates these before parking).
    anchor_uuid = None
    for msg in reversed(initial_messages):
        uuid = msg.get("uuid")
        if uuid:
            anchor_uuid = uuid
            break

    if anchor_uuid is None:
        # The snapshot carries NO transcript uuids at all (e.g. a crash-truncated
        # tier-1 buffer whose backfill never ran). We cannot anchor, so fall back
        # to the original longer-wins — this preserves the genuine crash-
        # truncation heal this function was built for.
        logger.debug(
            "heal_seed_against_store: seed has no anchor uuid — falling back to "
            "longer-wins (durable %d > seed %d msgs)",
            len(durable), len(initial_messages),
        )
        return durable

    durable_has_anchor = any(m.get("uuid") == anchor_uuid for m in durable)
    if durable_has_anchor:
        logger.debug(
            "heal_seed_against_store: durable transcript (%d msgs) is longer and "
            "contains the seed's anchor %s — healing seed to durable transcript",
            len(durable), anchor_uuid,
        )
        return durable

    # Durable is longer but does NOT contain the anchor: it is a DIFFERENT (and
    # likely more coarsely/finely blocked) view that is missing the interrupted
    # tail the snapshot preserved. Keep the seed to protect that tail and avoid
    # the duplicate-card id-family switch.
    logger.debug(
        "heal_seed_against_store: durable transcript (%d msgs) is longer but "
        "MISSING the seed's anchor %s — keeping seed (%d msgs) to preserve the "
        "interrupted tail",
        len(durable), anchor_uuid, len(initial_messages),
    )
    return initial_messages


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
       an auxiliary reader (history/JSONL parsing, LiveSessionStore seeding,
       checkpoint flows), so a missing worktree dir degrades to the project
       dir instead of raising; it must never break history reads.
    3. Default workspace.
    """
    from app.db import database
    from app.core.project_utils import get_project_working_dir, resolve_existing_worktree_cwd

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
