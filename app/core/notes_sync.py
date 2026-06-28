"""
Notes disk-sync worker.

Notes live in a shared database (the source of truth) but are also materialized
as per-install ``.md`` files on local disk, so the Claude Agent SDK — and the
user, over SSH/SMB/any text editor — can edit them as plain files. The problem:
nothing actively pushes a direct file edit back into the database, and nothing
pulls a database change (made on another install) down to this install's files.
The only existing reconcile lives in ``GET /notes/{id}`` and fires lazily, only
when a human opens that exact note in the UI. In a multi-install (shared-DB)
deployment that means an agent's edit silently fails to propagate until someone
happens to open the note in the originating install's browser.

This module is the missing worker: a periodic pass that reconciles each note's
on-disk file with its database row in BOTH directions, so an edit made anywhere
converges everywhere — no UI interaction required.

Design — a three-way merge against a per-install in-memory baseline:

    baseline = the (title, content) this install last knew to be in sync between
               its local file and the database, stored as a content hash.

    * file changed, db unchanged    -> PUSH  file -> db   (adopt the local edit)
    * db changed,   file unchanged  -> PULL  db   -> file (refresh stale file)
    * neither changed               -> nothing (fast-path skip)
    * both changed (true conflict)  -> newest wins (file mtime vs db updated_at)

Because the baseline is per-install, a file that is merely *stale* relative to
the database (older content this install never touched) is recognised as such
and PULLED down — rather than mistakenly PUSHED up, which is what would let one
install revert another install's edit. That distinction is the whole point of
tracking a baseline instead of comparing mtime alone.

The worker never deletes a database note from a missing local file: a missing
file is treated as an absent local view and re-materialized from the DB.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.note_files import (
    get_note_file_mtime,
    read_note_file,
    write_note_file,
)
from app.db import database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-install, in-memory sync state (keyed by note id). Rebuilt from scratch on
# process start; the first pass establishes baselines using a recency tiebreak.
# ---------------------------------------------------------------------------
_baseline_hash: Dict[str, str] = {}
_last_file_mtime: Dict[str, Optional[float]] = {}
_last_db_updated: Dict[str, Optional[str]] = {}


def reset_state() -> None:
    """Clear all in-memory sync state. Primarily for tests."""
    _baseline_hash.clear()
    _last_file_mtime.clear()
    _last_db_updated.clear()


def _content_hash(title: str, content: str) -> str:
    """Stable hash of a note's (title, content) used to detect real changes."""
    h = hashlib.sha1()
    h.update((title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((content or "").encode("utf-8"))
    return h.hexdigest()


def _updated_to_epoch(value: Any) -> Optional[float]:
    """Parse a stored ISO ``updated_at`` into epoch seconds (assume UTC)."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _remember(
    note_id: str,
    content_hash: str,
    file_mtime: Optional[float],
    db_updated: Optional[str],
) -> None:
    """Record the in-sync baseline + the inputs that produced it (fast-path)."""
    _baseline_hash[note_id] = content_hash
    _last_file_mtime[note_id] = file_mtime
    _last_db_updated[note_id] = db_updated


def _reconcile_one(note: Dict[str, Any]) -> None:
    """Reconcile a single note's on-disk file with its database row."""
    note_id = note["id"]
    file_path = note.get("file_path")
    if not file_path:
        return  # Not materialized yet (handled by startup materialization).

    filename = file_path.split("/")[-1]
    db_title = note.get("title") or ""
    db_content = note.get("content") or ""
    db_hash = _content_hash(db_title, db_content)
    db_updated = note.get("updated_at")

    mtime = get_note_file_mtime(filename)

    # Fast path: neither the file nor the DB row moved since we last looked, and
    # we already hold a baseline — nothing to do, and crucially no file read.
    if (
        note_id in _baseline_hash
        and _last_file_mtime.get(note_id) == mtime
        and _last_db_updated.get(note_id) == db_updated
    ):
        return

    baseline = _baseline_hash.get(note_id)

    # File absent locally: never delete the shared note off a missing file — a
    # peer install simply may not have materialized it yet. Restore the local
    # view from the DB (source of truth).
    if mtime is None:
        try:
            write_note_file(filename, db_title, db_content)
        except OSError:
            logger.warning("[NotesSync] could not materialize %s", filename)
            return
        _remember(note_id, db_hash, get_note_file_mtime(filename), db_updated)
        return

    parsed = read_note_file(filename)
    if parsed is None:
        # Exists but unreadable this cycle; skip the heavy work, remember inputs.
        _last_file_mtime[note_id] = mtime
        _last_db_updated[note_id] = db_updated
        return

    file_title, file_content = parsed
    # A heading-less external edit keeps the DB title (mirrors the GET reconcile).
    eff_title = file_title or db_title
    file_hash = _content_hash(eff_title, file_content)

    if file_hash == db_hash:
        # Already converged; just refresh the baseline + fast-path inputs.
        _remember(note_id, db_hash, mtime, db_updated)
        return

    file_changed = baseline is None or file_hash != baseline
    db_changed = baseline is None or db_hash != baseline

    push = pull = False
    if baseline is None:
        # Cold start (or note we've never synced here): no baseline to tell an
        # edit from staleness, so fall back to recency — newer side wins. This
        # matches the existing GET reconcile heuristic.
        db_epoch = _updated_to_epoch(db_updated)
        push = db_epoch is None or mtime > db_epoch
        pull = not push
    elif file_changed and not db_changed:
        push = True
    elif db_changed and not file_changed:
        pull = True
    else:
        # Both sides changed since the baseline — a genuine conflict. Newest
        # wins; log it so a real collision is visible.
        db_epoch = _updated_to_epoch(db_updated)
        push = db_epoch is None or mtime > db_epoch
        pull = not push
        logger.warning(
            "[NotesSync] conflict on note %s (%s) — %s wins",
            note_id, filename, "file" if push else "db",
        )

    if push:
        updated = database.update_note(
            note_id=note_id, title=eff_title, content=file_content
        )
        new_updated = (updated or {}).get("updated_at", db_updated) if isinstance(updated, dict) else db_updated
        _remember(note_id, file_hash, get_note_file_mtime(filename), new_updated)
        logger.info(
            "[NotesSync] pushed disk edit -> DB for note %s (%s)", note_id, filename
        )
    elif pull:
        try:
            write_note_file(filename, db_title, db_content)
        except OSError:
            logger.warning("[NotesSync] could not refresh %s from DB", filename)
            return
        _remember(note_id, db_hash, get_note_file_mtime(filename), db_updated)
        logger.info(
            "[NotesSync] pulled DB update -> disk for note %s (%s)", note_id, filename
        )


def _fetch_all_notes() -> List[Dict[str, Any]]:
    """Page through every note (the per-call limit is bounded)."""
    out: List[Any] = []
    offset = 0
    page = 500
    while True:
        batch = database.get_all_notes(limit=page, offset=offset)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def sync_notes_once() -> Dict[str, int]:
    """Run one full reconcile pass over all notes. Best-effort per note.

    Synchronous (blocking DB + file I/O); call via ``asyncio.to_thread`` from
    the async worker. Returns simple stats for logging/tests.
    """
    notes = _fetch_all_notes()
    seen: set = set()
    for note in notes:
        nid = note.get("id")
        if not nid:
            continue
        seen.add(nid)
        try:
            _reconcile_one(note)
        except Exception:
            logger.exception("[NotesSync] reconcile failed for note %s", nid)

    # Drop state for notes that no longer exist so the maps don't grow unbounded.
    for stale in [nid for nid in _baseline_hash if nid not in seen]:
        _baseline_hash.pop(stale, None)
        _last_file_mtime.pop(stale, None)
        _last_db_updated.pop(stale, None)

    return {"notes": len(notes)}
