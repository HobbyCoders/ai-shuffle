"""
Rewind DB sync.

Bridges the gap between Claude's JSONL transcript UUIDs and our
``session_messages`` rows during a rewind operation.

Two problems this solves:

A. **User-message UUID backfill.** User rows are inserted before the
   transcript echoes them, so ``jsonl_uuid`` starts NULL. Rewind needs
   that column to find the truncation anchor, so
   :func:`backfill_user_jsonl_uuids` pairs transcript user entries with DB
   rows by position + content and fills in the missing uuids.

B. **Content-match fallback.** When no row carries the target uuid (legacy
   rows, or a backfill that stopped on drift), the target transcript
   entry's text is matched against DB rows and the anchor is taken by row id.

The transcript is a fluid session's node tree, else the JSONL when present,
else the Postgres session store (see :func:`_load_transcript_entries`).

Entry points: :func:`resolve_rewind_anchor` then :func:`apply_rewind_delete`;
:func:`delete_all_session_rows` for a full reset.
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

    A fluid-harness session has neither source: its transcript is fluid's node
    tree. The facade projects those nodes onto the same entry shape, so every
    matcher in this module keeps working unchanged. The probe is prefix-gated,
    so a legacy session pays nothing for it.
    """
    try:
        from app.core.provider.fluid_transcript import (
            is_fluid_session,
            fluid_transcript_entries,
        )

        if is_fluid_session(sdk_session_id):
            return fluid_transcript_entries(sdk_session_id)
    except Exception as e:
        logger.debug("Fluid transcript load failed for %s: %s", sdk_session_id, e)

    try:
        jsonl_path = jsonl_rewind_service._get_jsonl_path(sdk_session_id, working_dir)
        if jsonl_path:
            entries = jsonl_rewind_service._parse_jsonl_entries(jsonl_path)
            if entries:
                return entries
    except Exception as e:
        logger.debug("JSONL transcript load failed for %s: %s", sdk_session_id, e)

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
            # Assistant rows store their final text (and never carry a
            # jsonl_uuid, so every assistant-target rewind lands here). Match
            # by content, disambiguating repeated text ("Done.") by occurrence
            # counted FROM THE END: if the transcript has N matching assistant
            # entries after the target, the anchor is the (N+1)-th matching row
            # from the end. Taking simply the last match anchored a rewind to
            # an earlier "Done." on the latest one, so the rows in between
            # survived the rewind. Counting from the end keeps older DB rows
            # the transcript no longer has from shifting the pick.
            #
            # Counting from the end needs every later transcript match to have
            # a DB row. Modern chat turns write no assistant rows, so a session
            # spanning that cutover holds fewer matches in the DB than in the
            # transcript; the walk would then pass the target's own row and
            # anchor on an older identical reply, deleting turns the rewind
            # keeps. Refuse (no DB sync) when the DB has fewer matching rows.
            def _matches(e: Dict[str, Any]) -> bool:
                return (
                    e.get("type") == "assistant"
                    and (e.get("message") or {}).get("role") == "assistant"
                    and not e.get("isMeta")
                    and not e.get("isSidechain")
                    and _texts_match(
                        jsonl_rewind_service._extract_message_text(e), target_text
                    )
                )

            later_matches = sum(1 for e in entries[target_idx + 1:] if _matches(e))
            # The target itself counts once, whatever its flags.
            transcript_matches = later_matches + 1 + sum(
                1 for e in entries[:target_idx] if _matches(e)
            )
            db_matches = [
                r for r in db_rows
                if r.get("role") == "assistant"
                and _texts_match(r.get("content") or "", target_text)
            ]
            if len(db_matches) < transcript_matches:
                logger.warning(
                    "Fallback anchor for session %s target %s: the DB has %d "
                    "matching assistant rows, the transcript %d. Refusing to "
                    "delete.",
                    session_id, target_uuid, len(db_matches), transcript_matches,
                )
                return None
            anchor = db_matches[-(later_matches + 1)]
            # Totals alone can balance out: one older identical reply the
            # transcript no longer holds offsets one modern turn with no row,
            # and the pick lands a turn early. The anchor must sit in the
            # target's own turn, i.e. be followed by exactly as many prompts
            # as the target is in the transcript; otherwise refuse.
            later_prompts = sum(1 for e in entries[target_idx + 1:] if _is_user_prompt_entry(e))
            later_prompt_rows = sum(
                1 for r in db_rows if r.get("role") == "user" and r["id"] > anchor["id"]
            )
            if later_prompt_rows != later_prompts:
                logger.warning(
                    "Fallback anchor for session %s target %s: %d prompt rows "
                    "follow the candidate row, %d prompts follow the target. "
                    "Refusing to delete.",
                    session_id, target_uuid, later_prompt_rows, later_prompts,
                )
                return None
            return anchor["id"]

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

    - ``include_response=True`` (what the UI sends for EVERY assistant-target
      rewind, and for "keep response" user rewinds): JSONL keeps the target
      and everything up to the next real user prompt
      (``compute_rewind_keep_count``). DB deletes from the next prompt row
      (``user``/``command``) after the anchor onward; with no later prompt
      nothing is deleted.

      This used to widen the anchor to the first ``assistant`` row after it
      and delete strictly after that. For an ASSISTANT anchor that row is the
      NEXT turn's reply, so the next turn's prompt and reply survived the
      rewind; for a user anchor it cut the turn's own later rows (tool rows,
      a second assistant text) that JSONL keeps.
    """
    from app.db.base import get_db, get_cursor
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)

        if include_response:
            cursor.execute(
                f"SELECT id FROM session_messages "
                f"WHERE session_id = {_ph} AND id > {_ph} "
                f"AND role IN ('user', 'command') "
                f"ORDER BY id ASC LIMIT 1",
                (session_id, anchor_id),
            )
            row = cursor.fetchone()
            if row is None:
                return 0  # target is the tail turn — nothing after it
            next_prompt_id = row["id"] if isinstance(row, dict) else row[0]
            cursor.execute(
                f"DELETE FROM session_messages WHERE session_id = {_ph} AND id >= {_ph}",
                (session_id, next_prompt_id),
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


def delete_all_session_rows(session_id: str) -> int:
    """Delete EVERY ``session_messages`` row for *session_id*.

    For the full-rewind edge case: the session store was truncated to empty
    and no legacy anchor row resolved. The empty-store read fallback would
    otherwise resurrect these rows wholesale on the next reload."""
    from app.db.base import get_db, get_cursor
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"DELETE FROM session_messages WHERE session_id = {_ph}",
            (session_id,),
        )
        removed = cursor.rowcount or 0
    logger.info(
        "Rewind DB purge (full rewind, no anchor): session=%s rows_removed=%d",
        session_id, removed,
    )
    return removed
