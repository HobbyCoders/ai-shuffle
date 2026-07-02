"""
Rewind DB sync.

Bridges the gap between Claude's JSONL transcript UUIDs and our
``session_messages`` rows during a rewind operation.

Two problems this solves:

A. **User-message UUID backfill.** User rows are persisted before the CLI
   echoes them back into the JSONL, so ``jsonl_uuid`` is NULL at insert
   time and ``set_message_jsonl_uuid`` was never called anywhere. Rewind
   needs that column populated to find the truncation anchor. We scan
   the JSONL, pair user entries with DB rows by position + content, and
   backfill the missing uuids.

B. **Content-match fallback.** For legacy sessions (rows created before
   the ``jsonl_uuid`` column existed or where backfill can't run because
   the JSONL is gone), we fall back to matching the target JSONL entry's
   text against DB rows and deleting by row id.

Entry point: :func:`sync_db_to_jsonl_rewind`. Safe to call even when the
JSONL is missing — falls back to the legacy ``delete_session_messages_after_jsonl_uuid``
path and returns whatever it finds.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.jsonl_rewind import jsonl_rewind_service
from app.db import database

logger = logging.getLogger(__name__)


def _is_user_prompt_entry(entry: Dict[str, Any]) -> bool:
    """Return True if *entry* is a real user prompt (not meta/sidechain/tool-result)."""
    if entry.get("type") != "user":
        return False
    message = entry.get("message") or {}
    if message.get("role") != "user":
        return False
    if entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = message.get("content", "")
    if isinstance(content, list):
        if any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            return False
    text = jsonl_rewind_service._extract_message_text(entry)
    if not text or text.startswith("<"):
        return False
    return True


def _collect_user_prompt_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ordered list of user prompt entries with uuid/text/timestamp."""
    out: List[Dict[str, Any]] = []
    for e in entries:
        if not _is_user_prompt_entry(e):
            continue
        uuid_ = e.get("uuid")
        if not uuid_:
            continue
        out.append({
            "uuid": uuid_,
            "text": jsonl_rewind_service._extract_message_text(e),
            "timestamp": e.get("timestamp"),
        })
    return out


def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(text.split()).strip()


def _texts_match(db_text: str, jsonl_text: str) -> bool:
    """Loose equality — identical after whitespace collapse, or one is a
    prefix of the other for at least 80 chars. Rewind pairing doesn't need
    byte-exact equality; it needs to be resilient to trim/CR normalisation
    quirks between the two storage paths."""
    a = _normalize(db_text)
    b = _normalize(jsonl_text)
    if not a or not b:
        return False
    if a == b:
        return True
    head = 80
    if len(a) >= head and len(b) >= head and a[:head] == b[:head]:
        return True
    if a.startswith(b) or b.startswith(a):
        return True
    return False


def _load_transcript_entries(
    sdk_session_id: str,
    working_dir: str,
) -> List[Dict[str, Any]]:
    """Load the session's transcript entries: JSONL when present, else the
    Postgres session store's main transcript.

    Under ``postgres_only`` the on-disk JSONL lives on tmpfs and is often gone,
    while store ``entry`` JSONB rows are the same JSONL-shaped dicts
    (``uuid``/``type``/``message``) — every matcher in this module works
    identically on either source. Without this fallback, anchor resolution
    returned None whenever the JSONL was absent and the legacy row survived
    every rewind (the e2e "rewind leftover" bug).

    Returns ``[]`` when neither source has entries. Never raises.
    """
    try:
        jsonl_path = jsonl_rewind_service._get_jsonl_path(sdk_session_id, working_dir)
        if jsonl_path:
            entries = jsonl_rewind_service._parse_jsonl_entries(jsonl_path)
            if entries:
                return entries
    except Exception as e:
        logger.debug("JSONL transcript load failed for %s: %s", sdk_session_id, e)

    # The store tables (and their %s placeholders) exist only when the
    # Postgres session store is active — never touch them otherwise.
    from app.core.config import settings

    if settings.session_store_mode not in ("mirror", "postgres_only"):
        return []
    try:
        from app.core.pg_session_store import _load_entries_by_session_id_sync

        return _load_entries_by_session_id_sync(sdk_session_id, None)
    except Exception as e:
        logger.debug("Store transcript load failed for %s: %s", sdk_session_id, e)
        return []


def backfill_user_jsonl_uuids(
    session_id: str,
    sdk_session_id: Optional[str],
    working_dir: str,
) -> int:
    """Populate ``session_messages.jsonl_uuid`` for user rows that were
    persisted without it.

    Matches DB user rows to transcript user prompt entries **by position** and
    sanity-checks with a loose text compare (JSONL when present, else the
    session store — see :func:`_load_transcript_entries`). Missing matches are
    skipped silently so one drifted row doesn't poison the rest.

    Returns the number of rows updated. Safe to call repeatedly.
    """
    if not sdk_session_id:
        return 0

    try:
        entries = _load_transcript_entries(sdk_session_id, working_dir)
        if not entries:
            return 0
        jsonl_user = _collect_user_prompt_entries(entries)
        if not jsonl_user:
            return 0

        db_rows = database.get_session_messages(session_id)
        db_user_rows = [r for r in db_rows if r.get("role") == "user"]

        updated = 0
        for idx, db_row in enumerate(db_user_rows):
            if idx >= len(jsonl_user):
                break
            if db_row.get("jsonl_uuid"):
                continue
            jsonl_entry = jsonl_user[idx]
            if not _texts_match(db_row.get("content") or "", jsonl_entry["text"]):
                # Position-based alignment has drifted — stop rather than
                # write the wrong uuid onto the wrong row.
                logger.debug(
                    "Backfill aborted at idx=%d: db/jsonl text mismatch for session %s",
                    idx, session_id,
                )
                break
            try:
                if database.set_message_jsonl_uuid(db_row["id"], jsonl_entry["uuid"]):
                    updated += 1
            except Exception as e:
                logger.debug("Backfill set uuid failed for row %s: %s", db_row.get("id"), e)

        if updated:
            logger.info(
                "Backfilled jsonl_uuid for %d user rows in session %s",
                updated, session_id,
            )
        return updated
    except Exception as e:
        logger.debug("backfill_user_jsonl_uuids skipped: %s", e)
        return 0


def _find_target_entry(
    entries: List[Dict[str, Any]],
    target_uuid: str,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    for i, e in enumerate(entries):
        if e.get("uuid") == target_uuid:
            return i, e
    return None


def _fallback_anchor_by_content(
    session_id: str,
    sdk_session_id: str,
    working_dir: str,
    target_uuid: str,
) -> Optional[int]:
    """Resolve a DB anchor id for *target_uuid* by matching transcript entry
    text against DB rows (JSONL when present, else the session store — see
    :func:`_load_transcript_entries`).

    Strategy: count how many **user prompt** entries precede the target in
    the transcript. Take the Nth user row in the DB as the anchor. For
    assistant targets, match by normalised content on assistant rows (they're
    stored with text already, so exact content match is usually fine).

    Returns the DB row id to use as the rewind anchor, or None if the
    target can't be resolved.
    """
    try:
        entries = _load_transcript_entries(sdk_session_id, working_dir)
        found = _find_target_entry(entries, target_uuid)
        if not found:
            return None
        target_idx, target_entry = found

        db_rows = database.get_session_messages(session_id)

        message = target_entry.get("message") or {}
        role = message.get("role")
        target_text = jsonl_rewind_service._extract_message_text(target_entry)

        if role == "user":
            # Count user prompt entries at or before target_idx (1-indexed).
            position = 0
            for i in range(target_idx + 1):
                if _is_user_prompt_entry(entries[i]):
                    position += 1
            if position == 0:
                return None
            user_rows = [r for r in db_rows if r.get("role") == "user"]
            if len(user_rows) < position:
                return None
            anchor_row = user_rows[position - 1]
            # Sanity check text — if it looks nothing like the target, bail
            # rather than truncating the wrong part of history.
            if not _texts_match(anchor_row.get("content") or "", target_text):
                logger.warning(
                    "Fallback anchor content mismatch for session %s target %s "
                    "(position=%d). Refusing to delete.",
                    session_id, target_uuid, position,
                )
                return None
            return anchor_row["id"]

        if role == "assistant":
            # Prefer exact/loose match; assistant rows store their final text.
            for r in reversed(db_rows):
                if r.get("role") != "assistant":
                    continue
                if _texts_match(r.get("content") or "", target_text):
                    return r["id"]
            return None

        return None
    except Exception as e:
        logger.debug("fallback anchor lookup failed: %s", e)
        return None


def _delete_rows_after(
    session_id: str,
    anchor_id: int,
    include_response: bool,
) -> int:
    """Delete session_messages rows to mirror a JSONL rewind.

    Semantics must match ``JSONLRewindService.truncate_to_checkpoint``:

    - ``include_response=False`` (typical for user-target rewinds):
      JSONL drops the target entry itself (``keep_entries = entries[:target_index]``).
      DB must therefore delete rows with ``id >= anchor_id`` — the anchor
      row is the target and goes too.

    - ``include_response=True`` (typical for assistant-target rewinds or
      "keep response" user rewinds): JSONL keeps the target and its
      trailing assistant reply. DB widens the anchor to the first
      following assistant row and deletes rows strictly after that,
      preserving both the target and its response.
    """
    from app.db.base import get_db, get_cursor
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)

        if include_response:
            effective_anchor = anchor_id
            cursor.execute(
                f"SELECT id FROM session_messages "
                f"WHERE session_id = {_ph} AND id > {_ph} AND role = 'assistant' "
                f"ORDER BY id ASC LIMIT 1",
                (session_id, anchor_id),
            )
            row = cursor.fetchone()
            if row is not None:
                effective_anchor = row["id"] if isinstance(row, dict) else row[0]
            cursor.execute(
                f"DELETE FROM session_messages WHERE session_id = {_ph} AND id > {_ph}",
                (session_id, effective_anchor),
            )
        else:
            # Drop anchor row and everything after it (matches JSONL's
            # entries[:target_index] slice).
            cursor.execute(
                f"DELETE FROM session_messages WHERE session_id = {_ph} AND id >= {_ph}",
                (session_id, anchor_id),
            )
        return cursor.rowcount or 0


def _resolve_anchor_by_uuid(session_id: str, target_uuid: str) -> Optional[int]:
    """Look up the DB row id whose ``jsonl_uuid`` matches *target_uuid*."""
    from app.db.base import get_db, get_cursor
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT id FROM session_messages "
            f"WHERE session_id = {_ph} AND jsonl_uuid = {_ph}",
            (session_id, target_uuid),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return row["id"] if isinstance(row, dict) else row[0]


def resolve_rewind_anchor(
    session_id: str,
    sdk_session_id: Optional[str],
    working_dir: str,
    target_uuid: str,
) -> Optional[int]:
    """Resolve the DB row id to use as the rewind anchor.

    Runs **before** the JSONL and the session store are truncated so the
    fallback content-match can still read *target_uuid*'s text from whichever
    transcript source survives.

    Steps:
      A. Backfill missing ``jsonl_uuid`` on user rows.
      1. Fast path: look up the row whose ``jsonl_uuid`` matches.
      B. Fallback: position/content match against JSONL.

    Returns the anchor row id, or None if no anchor could be resolved.
    """
    # Step A — fill missing user uuids so future rewinds hit the fast path.
    backfill_user_jsonl_uuids(session_id, sdk_session_id, working_dir)

    anchor = _resolve_anchor_by_uuid(session_id, target_uuid)
    if anchor is not None:
        return anchor

    if not sdk_session_id:
        return None

    # Step B — fallback matcher. The transcript source (JSONL or session
    # store) must still be intact here.
    return _fallback_anchor_by_content(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        working_dir=working_dir,
        target_uuid=target_uuid,
    )


def apply_rewind_delete(
    session_id: str,
    anchor_id: int,
    include_response: bool,
) -> int:
    """Delete DB rows after *anchor_id*, optionally preserving the next
    assistant row. Call after the JSONL is successfully truncated."""
    removed = _delete_rows_after(session_id, anchor_id, include_response)
    logger.info(
        "Rewind DB sync: session=%s anchor_id=%s rows_removed=%d",
        session_id, anchor_id, removed,
    )
    return removed
