"""
Cost & Session Metadata Utilities

Shared helpers for attaching cost/duration metadata to session messages
and caching frequently-accessed session data to avoid redundant DB queries.

Two in-memory caches:
1. Usage cache: session_id → list of usage entries (from usage_log table)
2. Profile map cache: session_id → dict mapping user message content → profile_id

These caches are updated incrementally when new data is written (via hooks in
database.py) and serve reads without hitting the database on every poll/load.
"""

import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Usage Cache ─────────────────────────────────────────────────────────────
# session_id → list of usage entry dicts (same format as get_all_session_usage)
_USAGE_CACHE_MAX_SIZE = 100
_usage_cache: OrderedDict = OrderedDict()
_usage_cache_lock = threading.Lock()


def get_cached_usage(session_id: str) -> List[Dict[str, Any]]:
    """
    Get usage entries for a session, using in-memory cache when available.

    Falls back to database on cache miss and populates the cache.
    Returns list of dicts with: cost_usd, cumulative_cost_usd, duration_ms,
    tokens_in, tokens_out, model, turn_index.
    """
    with _usage_cache_lock:
        cached = _usage_cache.get(session_id)
        if cached is not None:
            _usage_cache.move_to_end(session_id)
            return cached

    # Cache miss - load from database
    from app.db import database
    entries = database.get_all_session_usage(session_id)

    with _usage_cache_lock:
        _usage_cache[session_id] = entries
        while len(_usage_cache) > _USAGE_CACHE_MAX_SIZE:
            _usage_cache.popitem(last=False)

    return entries


def append_usage_entry(
    session_id: str,
    cost_usd: float,
    duration_ms: int,
    tokens_in: int,
    tokens_out: int,
    model: Optional[str]
) -> None:
    """
    Append a new usage entry to the in-memory cache.

    Called from database.log_usage() after the DB insert succeeds.
    This keeps the cache in sync without needing a full DB re-read.
    """
    with _usage_cache_lock:
        cached = _usage_cache.get(session_id)
        if cached is None:
            # Not cached yet - don't populate (will load on next read)
            return

        # Compute cumulative cost
        prev_cumulative = cached[-1]["cumulative_cost_usd"] if cached else 0.0
        cumulative_cost = prev_cumulative + (cost_usd or 0.0)

        cached.append({
            "cost_usd": cost_usd or 0.0,
            "cumulative_cost_usd": cumulative_cost,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": model,
            "turn_index": len(cached)
        })
        _usage_cache.move_to_end(session_id)


def invalidate_usage_cache(session_id: Optional[str] = None) -> None:
    """Invalidate usage cache for a session or all sessions."""
    with _usage_cache_lock:
        if session_id:
            _usage_cache.pop(session_id, None)
        else:
            _usage_cache.clear()


# ─── Profile Map Cache ───────────────────────────────────────────────────────
# session_id → dict mapping user message content → profile_id
_PROFILE_CACHE_MAX_SIZE = 100
_profile_map_cache: OrderedDict = OrderedDict()
_profile_map_cache_lock = threading.Lock()


def get_cached_profile_map(session_id: str) -> Dict[str, str]:
    """
    Get the content→profile_id map for a session, using in-memory cache.

    Falls back to building the map from database on cache miss.
    """
    with _profile_map_cache_lock:
        cached = _profile_map_cache.get(session_id)
        if cached is not None:
            _profile_map_cache.move_to_end(session_id)
            return cached

    # Cache miss - build from database
    from app.db import database
    db_messages = database.get_session_messages(session_id)
    profile_map: Dict[str, str] = {}
    for msg in db_messages:
        if msg.get("role") == "user" and msg.get("profile_id"):
            content = msg.get("content", "")
            profile_map[content] = msg["profile_id"]

    with _profile_map_cache_lock:
        _profile_map_cache[session_id] = profile_map
        while len(_profile_map_cache) > _PROFILE_CACHE_MAX_SIZE:
            _profile_map_cache.popitem(last=False)

    return profile_map


def update_profile_map(session_id: str, content: str, profile_id: str) -> None:
    """
    Update the profile map cache with a new user message.

    Called from database.add_session_message() when a user message with
    profile_id is saved.
    """
    with _profile_map_cache_lock:
        cached = _profile_map_cache.get(session_id)
        if cached is not None:
            cached[content] = profile_id
            _profile_map_cache.move_to_end(session_id)


def invalidate_profile_cache(session_id: Optional[str] = None) -> None:
    """Invalidate profile map cache for a session or all sessions."""
    with _profile_map_cache_lock:
        if session_id:
            _profile_map_cache.pop(session_id, None)
        else:
            _profile_map_cache.clear()


# ─── Cost Attachment Helper ──────────────────────────────────────────────────

def attach_cost_metadata(messages: List[Dict[str, Any]], session_id: str) -> None:
    """
    Attach cost/duration metadata to final assistant messages in-place.

    Finds the last non-empty assistant text message in each user→assistant turn
    using an O(N) reverse scan, then matches usage_log entries by turn index.

    This replaces the duplicated inline cost-attachment logic in:
    - sessions.py GET /sessions/{id}
    - sessions.py GET /sessions/{id}/live
    - websocket.py load_session handler

    Args:
        messages: List of message dicts (modified in-place)
        session_id: Session ID for looking up usage entries
    """
    if not messages:
        return

    # Find final assistant text messages per turn using a single reverse pass O(N)
    # A "final" message is the last assistant text before a user message or end of list
    final_indices: List[int] = []
    last_assistant_text_idx: Optional[int] = None

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        role = msg.get("role")

        if role == "user":
            if last_assistant_text_idx is not None:
                final_indices.append(last_assistant_text_idx)
                last_assistant_text_idx = None
        elif role == "assistant" and msg.get("type") in ("text", None):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip() and last_assistant_text_idx is None:
                last_assistant_text_idx = i

    # Handle the first turn (no user message before it)
    if last_assistant_text_idx is not None:
        final_indices.append(last_assistant_text_idx)
    final_indices.reverse()

    # Get usage entries (cached) and match to final messages
    usage_entries = get_cached_usage(session_id)
    for turn_idx, msg_idx in enumerate(final_indices):
        if turn_idx < len(usage_entries):
            usage = usage_entries[turn_idx]
            is_absolute_final = (turn_idx == len(final_indices) - 1)
            existing_metadata = messages[msg_idx].get("metadata") or {}

            metadata = {
                **existing_metadata,
                "cost_usd": usage["cost_usd"],
                "duration_ms": usage["duration_ms"],
                "tokens_out": usage.get("tokens_out", 0),
                "is_final_message": is_absolute_final
            }
            # Only add cumulative cost to the absolute final message
            if is_absolute_final:
                metadata["total_cost_usd"] = usage["cumulative_cost_usd"]

            messages[msg_idx]["metadata"] = metadata
