"""
Cost & Session Metadata Utilities

Shared helpers for attaching cost/duration metadata to session messages
and caching frequently-accessed session data to avoid redundant DB queries.

Two in-memory caches:
1. Usage cache: session_id → list of usage entries (from usage_log table)
2. Profile map cache: session_id → dict mapping user message content → ordered
   list of profile_ids (one per message with that content)

These caches are updated incrementally when new data is written (via hooks in
database.py) and serve reads without hitting the database on every poll/load.
"""

import json
import logging
import threading
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Hard ceiling on what a single session may keep in the per-content maps. The
# LRUs below bound the number of SESSIONS cached but said nothing about entry
# SIZE, so one pathological transcript could pin an unbounded dict in memory.
# Oversized sessions are still served correctly — they are just not cached.
_MAX_CACHED_MAP_VALUES = 20000


def _load_user_message_rows(session_id: str) -> List[tuple]:
    """``(content, profile_id, metadata)`` for a session's USER rows, in order.

    The two per-content caches below are built ONLY from user rows and read
    only these three columns, but each cache miss used to run
    ``get_session_messages`` — a full ``SELECT * FROM session_messages`` for
    the session (every assistant row, every tool payload), twice per session
    open. This is the narrow projection instead.

    Falls back to ``database.get_session_messages`` if the direct read fails,
    so behaviour is unchanged on any driver/schema surprise.
    """
    try:
        from app.db.base import get_cursor, get_db, rows_to_list
        from app.db.compat import ph

        with get_db() as conn:
            cursor = get_cursor(conn)
            cursor.execute(
                "SELECT content, profile_id, metadata FROM session_messages "
                f"WHERE session_id = {ph()} AND role = 'user' "
                "ORDER BY created_at ASC, id ASC",
                (session_id,),
            )
            rows = rows_to_list(cursor.fetchall())
    except Exception:  # noqa: BLE001 — never fail a transcript read on this
        logger.debug(
            "Narrow user-row read failed for %s; falling back", session_id, exc_info=True
        )
        from app.db import database

        rows = [
            m for m in database.get_session_messages(session_id)
            if m.get("role") == "user"
        ]

    resolved: List[tuple] = []
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = None
        resolved.append((
            row.get("content", ""),
            row.get("profile_id"),
            metadata if isinstance(metadata, dict) else None,
        ))
    return resolved


def _build_user_row_maps(
    session_id: str,
) -> tuple[Dict[str, List[str]], Dict[str, List[Optional[Dict[str, Any]]]]]:
    """Both per-content maps (profile ids, row metadata) from ONE read.

    They are derived from the same user rows, so a miss on either fills both —
    a session open no longer pays for two identical scans.
    """
    profile_map: Dict[str, List[str]] = {}
    meta_map: Dict[str, List[Optional[Dict[str, Any]]]] = {}
    for content, profile_id, metadata in _load_user_message_rows(session_id):
        if profile_id:
            profile_map.setdefault(content, []).append(profile_id)
        meta_map.setdefault(content, []).append(metadata)
    return profile_map, meta_map


def _map_value_count(mapping: Dict[str, list]) -> int:
    """Total number of queued values across a per-content map."""
    return sum(len(values) for values in mapping.values())


def _store_bounded(cache: OrderedDict, lock: threading.Lock, key: str, value: dict, max_size: int) -> None:
    """Cache ``value`` under ``key`` (LRU-evicting), unless it is oversized.

    Never clobbers an entry that already exists: a concurrent incremental
    append (``update_profile_map`` / ``update_user_metadata_map``) landing
    between the read and this store would otherwise be lost.
    """
    if _map_value_count(value) > _MAX_CACHED_MAP_VALUES:
        return
    with lock:
        if key in cache:
            cache.move_to_end(key)
            return
        cache[key] = value
        while len(cache) > max_size:
            cache.popitem(last=False)

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


def get_cached_profile_map(session_id: str) -> Dict[str, List[str]]:
    """
    Get the content→[profile_id, ...] map for a session, using in-memory cache.

    Values are ORDERED lists (one entry per user message with that content, in
    creation order) rather than a single id. A session whose profile changed
    mid-conversation can repeat the same prompt text under different profiles;
    a single-value map collapsed those onto the last write. Consumers pair the
    ids to messages positionally via :func:`build_profile_queues`.

    Falls back to building the map from database on cache miss.
    """
    with _profile_map_cache_lock:
        cached = _profile_map_cache.get(session_id)
        if cached is not None:
            _profile_map_cache.move_to_end(session_id)
            return cached

    # Cache miss - build from database (one narrow read fills BOTH maps)
    profile_map, meta_map = _build_user_row_maps(session_id)
    _store_bounded(
        _profile_map_cache, _profile_map_cache_lock,
        session_id, profile_map, _PROFILE_CACHE_MAX_SIZE,
    )
    _store_bounded(
        _user_meta_cache, _user_meta_cache_lock,
        session_id, meta_map, _PROFILE_CACHE_MAX_SIZE,
    )

    return profile_map


def build_profile_queues(session_id: str) -> Dict[str, "deque[str]"]:
    """
    Per-request consumable queues of profile_ids keyed by user-message content.

    JSONL/live transcripts carry no DB row id, so a message's profile is
    resolved by matching its content against the DB user rows. Popping the
    matching content's queue left-to-right as user messages are walked in order
    assigns each occurrence its OWN profile — correct even when the same prompt
    was sent twice under different profiles.
    """
    profile_map = get_cached_profile_map(session_id)
    # Snapshot under the lock: the cached map is mutated in place by
    # ``update_profile_map`` from the write path, and iterating it unguarded
    # can raise "dictionary changed size during iteration" mid-request.
    with _profile_map_cache_lock:
        items = [(content, list(ids)) for content, ids in profile_map.items()]
    return {content: deque(ids) for content, ids in items}


def attach_turn_profile_ids(
    session_id: str,
    messages: List[Dict[str, Any]],
    *,
    both_keys: bool = True,
) -> List[Dict[str, Any]]:
    """Stamp each message with the profile that produced its turn, in-place.

    Raw JSONL/store transcripts record a profile only on USER rows; the
    assistant / thinking / tool rows that follow it inherit that turn's
    profile. Walk the messages in order tracking the current turn's profile
    (popped positionally from the per-content queue on each user row) and
    label every row of that turn with it. Without this the UI falls back to
    the session's CURRENT selector and re-labels historical bubbles whenever
    the user switches profiles — the exact bug this addresses.

    A user row whose content has no matching DB profile (unresolved turn)
    leaves ``current_turn_profile`` None; those rows are left untouched rather
    than mislabelled. Returns the same list for chaining.
    """
    profile_queues = build_profile_queues(session_id)
    current_turn_profile: Optional[str] = None
    for m in messages:
        if m.get("role") == "user":
            queue = profile_queues.get(m.get("content", ""))
            current_turn_profile = queue.popleft() if queue else None
        if current_turn_profile is not None:
            m["profile_id"] = current_turn_profile
            if both_keys:
                m["profileId"] = current_turn_profile
    return messages


def update_profile_map(session_id: str, content: str, profile_id: str) -> None:
    """
    Update the profile map cache with a new user message.

    Called from database.add_session_message() when a user message with
    profile_id is saved. Appends (preserving order) rather than overwriting so
    repeated identical prompts each keep their own profile.
    """
    with _profile_map_cache_lock:
        cached = _profile_map_cache.get(session_id)
        if cached is not None:
            cached.setdefault(content, []).append(profile_id)
            _profile_map_cache.move_to_end(session_id)


def invalidate_profile_cache(session_id: Optional[str] = None) -> None:
    """Invalidate profile map cache for a session or all sessions."""
    with _profile_map_cache_lock:
        if session_id:
            _profile_map_cache.pop(session_id, None)
        else:
            _profile_map_cache.clear()
    with _user_meta_cache_lock:
        if session_id:
            _user_meta_cache.pop(session_id, None)
        else:
            _user_meta_cache.clear()


# ─── User-Message Metadata Map Cache ─────────────────────────────────────────
# session_id → dict mapping user message content → [metadata|None, ...], one
# entry per DB user row with that content, in creation order. Mirrors the
# profile map cache (kept separate — tests pin that cache's value shape).
#
# Why this exists (PR #560 follow-up): transcript reads (SDK store / JSONL)
# rebuild user messages from the SDK payload, which never carried the DB row's
# metadata — so automation-originated messages lose their
# {automation_id, automation_run_id} badge on the read path. These queues let
# the read path merge the DB row metadata back in positionally, the same way
# profile ids are resolved.
_user_meta_cache: OrderedDict = OrderedDict()
_user_meta_cache_lock = threading.Lock()


def get_cached_user_metadata_map(session_id: str) -> Dict[str, List[Optional[Dict[str, Any]]]]:
    """content → ordered [metadata|None] for the session's DB user rows."""
    with _user_meta_cache_lock:
        cached = _user_meta_cache.get(session_id)
        if cached is not None:
            _user_meta_cache.move_to_end(session_id)
            return cached

    # One narrow read fills BOTH maps (see get_cached_profile_map).
    profile_map, meta_map = _build_user_row_maps(session_id)
    _store_bounded(
        _user_meta_cache, _user_meta_cache_lock,
        session_id, meta_map, _PROFILE_CACHE_MAX_SIZE,
    )
    _store_bounded(
        _profile_map_cache, _profile_map_cache_lock,
        session_id, profile_map, _PROFILE_CACHE_MAX_SIZE,
    )

    return meta_map


def update_user_metadata_map(
    session_id: str, content: str, metadata: Optional[Dict[str, Any]]
) -> None:
    """Append a new user row's metadata (or None) if the cache is primed.

    Must be called for EVERY user row (even metadata-less ones) so the
    per-content queues stay positionally aligned with the DB rows.
    """
    with _user_meta_cache_lock:
        cached = _user_meta_cache.get(session_id)
        if cached is not None:
            cached.setdefault(content, []).append(
                metadata if isinstance(metadata, dict) else None
            )
            _user_meta_cache.move_to_end(session_id)


def attach_user_message_metadata(
    messages: List[Dict[str, Any]], session_id: str
) -> List[Dict[str, Any]]:
    """Merge DB user-row metadata into transcript user messages, in-place.

    Store/JSONL transcripts rebuild user messages from the SDK payload, which
    lacks the metadata written on the session_messages row (e.g. an automation
    run's {automation_id, automation_run_id}). Pair each user message with its
    DB row positionally via per-content queues (same pattern as profile ids)
    and merge — transcript-side keys win on conflict. Messages that already
    came from the DB merge their own metadata back: a no-op. Returns the same
    list for chaining.
    """
    meta_map = get_cached_user_metadata_map(session_id)
    # Snapshot under the lock (see build_profile_queues).
    with _user_meta_cache_lock:
        items = [(content, list(metas)) for content, metas in meta_map.items()]
    meta_queues = {content: deque(metas) for content, metas in items}
    if not meta_queues:
        return messages
    for m in messages:
        if m.get("role") != "user":
            continue
        queue = meta_queues.get(m.get("content", ""))
        db_meta = queue.popleft() if queue else None
        if not db_meta:
            continue
        existing = m.get("metadata")
        m["metadata"] = {**db_meta, **existing} if isinstance(existing, dict) else dict(db_meta)
    return messages


# ─── Cost Attachment Helper ──────────────────────────────────────────────────

def attach_cost_metadata(
    messages: List[Dict[str, Any]],
    session_id: str,
    *,
    turn_offset: int = 0,
    total_turns: Optional[int] = None,
) -> None:
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
        turn_offset: Absolute index of the first turn in ``messages``. Usage
            entries are matched positionally against the WHOLE session, so a
            windowed/paginated slice must say where it starts or every message
            gets the cost of a different turn. 0 for a full transcript.
        total_turns: Total turns in the session, when known. Only the session's
            genuinely last turn carries ``is_final_message`` and the cumulative
            total; without this a window's last turn would falsely claim to be
            the end of the conversation. Defaults to treating the supplied list
            as the whole session.
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
    # Absolute turn count for deciding which message is the session's last.
    effective_total = (
        total_turns if total_turns is not None else turn_offset + len(final_indices)
    )
    for local_idx, msg_idx in enumerate(final_indices):
        turn_idx = turn_offset + local_idx
        if turn_idx < len(usage_entries):
            usage = usage_entries[turn_idx]
            is_absolute_final = (turn_idx == effective_total - 1)
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
