"""Content-addressed blobs: artifacts and note attachments, fetched on demand.

A synced record names a hash; the bytes are not in the envelope. Every
device that holds the bytes serves them in encrypted parts at
``/peer/blob/{hash}/parts/{index}`` (``peer_files``); a device that
materialized a record without bytes asks online holders (the owner first)
and stores what it gets. ``sync_blobs`` tracks what is present and who else
has it.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import nullcontext
from typing import Any, Dict, Optional, Tuple

import httpx

from app.db import network as store
from app.db.async_database import run_sync
from app.db.base import get_db, utc_now_iso
from app.network import engine, policy, transport

logger = logging.getLogger(__name__)

MAX_BLOB_BYTES = 256 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Local inventory
# ---------------------------------------------------------------------------

def backfill_attachment_hashes() -> int:
    """Stamp ``blob_hash`` on attachments that predate the network."""
    updated = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, data FROM note_attachments WHERE blob_hash IS NULL AND data IS NOT NULL")
        rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            data = row["data"]
            if isinstance(data, memoryview):
                data = data.tobytes()
            if data is None:
                continue
            digest = sha256(bytes(data))
            cursor.execute("UPDATE note_attachments SET blob_hash = %s WHERE id = %s", (digest, row["id"]))
            store.upsert_blob(digest, size=len(data), present=True, _conn=conn)
            updated += 1
    return updated


def register_present(hash_value: str, size: int) -> None:
    ctx = engine.context()
    store.upsert_blob(hash_value, size=size, present=True, holder=ctx.device_id if ctx else None)


def read_local(hash_value: str, *, _conn=None) -> Optional[bytes]:
    """Bytes for a hash from the artifact store, a note attachment or a kept conflict, else None."""
    try:
        from app.core.artifact_store import artifact_path
        path = artifact_path(hash_value)
        if path is not None:
            return path.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    with nullcontext(_conn) if _conn is not None else get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM note_attachments WHERE blob_hash = %s AND data IS NOT NULL LIMIT 1", (hash_value,))
        row = cursor.fetchone()
        if not row:
            # An attachment a peer's note deletion removed (keep_cascaded_attachments).
            cursor.execute("SELECT kept_blob AS data FROM sync_conflicts WHERE kept_blob_hash = %s "
                           "AND kept_blob IS NOT NULL AND expires_at > %s LIMIT 1", (hash_value, utc_now_iso()))
            row = cursor.fetchone()
    if row and row["data"] is not None:
        data = row["data"]
        return data.tobytes() if isinstance(data, memoryview) else bytes(data)
    return None


def read_local_range(hash_value: str, offset: int, length: int) -> Optional[Tuple[bytes, int]]:
    """``(bytes[offset:offset+length], total size)`` without loading a whole file, else None.

    A file-backed blob is seeked; a database-backed one is read once per call.
    """
    try:
        from app.core.artifact_store import artifact_path
        path = artifact_path(hash_value)
        if path is not None:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                fh.seek(offset)
                return fh.read(max(0, length)), size
    except Exception:  # noqa: BLE001
        pass
    data = read_local(hash_value)
    if data is None:
        return None
    return data[offset:offset + max(0, length)], len(data)


def note_missing(hash_value: str, size: int, holder: Optional[str], *, _cursor=None) -> None:
    """A record arrived whose bytes we do not have.

    Bytes already here (another attachment, a kept conflict) are copied into
    this hash's empty attachment rows: a row never depends on a copy that can
    go before it does, such as a kept conflict's (#1693).
    """
    data = read_local(hash_value)
    if data is not None:
        fill = ("UPDATE note_attachments SET data = %s, size_bytes = %s WHERE blob_hash = %s AND data IS NULL",
                (data, len(data), hash_value))
        if _cursor is not None:
            _cursor.execute(*fill)
        else:
            with get_db() as conn:
                conn.cursor().execute(*fill)
        store.upsert_blob(hash_value, size=size, present=True, holder=holder, _cursor=_cursor)
        return
    store.upsert_blob(hash_value, size=size, present=False, holder=holder, _cursor=_cursor)


def keep_cascaded_attachments(cursor, note_id: str, *, deleted_by: Optional[str], clock: Optional[int]) -> int:
    """Before a peer's deletion of a note cascades, keep the attachments it removes (#1693).

    ``note_attachments.data`` never travels, so this worker may be the only
    holder of an attachment the deleting device never had. Each one becomes a
    conflict (the losing attachment, the winning note tombstone) that also
    holds its bytes, so ``read_local`` still finds them by hash while the
    conflict is kept; dismissing or expiring it drops them. Same transaction
    as the cascade, so a crash keeps either the row or the conflict.

    Every attachment is kept, whoever published it: a record's origin does not
    say the deleting device still held it (a restore from an older backup).
    Its own tombstone for the attachment does, and ``release_covered_attachment``
    then drops the conflict. A local note deletion never reaches here.
    Returns the number of conflicts recorded.
    """
    # Lock the note before reading its attachments: an upload's FK check
    # waits on it, so the cascade removes no attachment this read missed.
    cursor.execute("SELECT id FROM notes WHERE id = %s FOR UPDATE", (note_id,))
    if cursor.fetchone() is None:
        return 0
    # Then the attachment rows, by id: a fetch filling ``data`` waits for the
    # cascade instead of landing after this read. Copied one at a time.
    cursor.execute("SELECT id FROM note_attachments WHERE note_id = %s ORDER BY id FOR UPDATE", (note_id,))
    ids = [r["id"] for r in cursor.fetchall()]
    coll = policy.collection("note_attachments")
    ctx = engine.cached_context()
    kept = 0
    for attachment_id in ids:
        cursor.execute("SELECT * FROM note_attachments WHERE id = %s", (attachment_id,))
        row = dict(cursor.fetchone())
        version = store.get_record(cursor, coll.name, row["id"])
        try:
            losing = engine.serialize_row(coll, row, cursor=cursor) or {}
        except engine.ApplyError:
            losing = {"id": row["id"], "note_id": note_id, "blob_hash": row.get("blob_hash")}
        data = row.get("data")
        if isinstance(data, memoryview):
            data = data.tobytes()
        # Keyed by the bytes' own hash, also for a row whose blob_hash predates
        # the network. A row without bytes keeps none, but its hash still marks
        # the conflict as one this function wrote (release_covered_attachment).
        blob = ((sha256(bytes(data)), bytes(data)) if data is not None
                else (row.get("blob_hash") or "", None))
        store.add_conflict(cursor, collection=coll.name, record_id=row["id"],
                           losing_clock=int(version["clock"]) if version is not None else 0,
                           losing_device_id=(version["origin_device_id"] if version is not None
                                             else (ctx.device_id if ctx is not None else "")),
                           losing_record=engine.redact_secrets(losing),
                           winning_clock=int(clock or 0), winning_device_id=deleted_by or "",
                           kept_blob=blob)
        kept += 1
    return kept


def release_covered_attachment(cursor, attachment_id: str, *, deleted_by: Optional[str]) -> int:
    """A device's own tombstone for an attachment: its note deletion covered it (#1693).

    A device signs an attachment tombstone only for a record it held (its
    scan of the cascade, or a deliberate attachment delete), so the copy kept
    when its deletion of the note arrived is not needed: that was an ordinary
    delete. Drops only conflicts ``keep_cascaded_attachments`` wrote for that
    device's deletion; each holds its own copy, so no other conflict or row
    loses bytes. Called for every such tombstone, winning or not.
    """
    if not deleted_by:
        return 0
    cursor.execute("DELETE FROM sync_conflicts WHERE collection = 'note_attachments' AND record_id = %s "
                   "AND winning_device_id = %s AND kept_blob_hash IS NOT NULL", (attachment_id, deleted_by))
    return cursor.rowcount or 0


def store_fetched(hash_value: str, data: bytes) -> bool:
    if sha256(data) != hash_value:
        logger.warning("Blob %s failed its hash check", hash_value[:12])
        return False
    wrote = False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE note_attachments SET data = %s, size_bytes = %s WHERE blob_hash = %s AND data IS NULL",
                       (data, len(data), hash_value))
        wrote = bool(cursor.rowcount)
        cursor.execute("SELECT 1 FROM artifacts WHERE hash = %s", (hash_value,))
        is_artifact = cursor.fetchone() is not None
    if is_artifact:
        try:
            from app.core.artifact_store import store_bytes
            from app.core.config import settings
            store_bytes(data, runner_id=settings.effective_instance_id)
            wrote = True
        except Exception:  # noqa: BLE001
            logger.warning("Could not store fetched artifact %s", hash_value[:12], exc_info=True)
            # A matching note may have been written, but artifact consumers need
            # the disk copy too. Keep retrying until every referenced sink exists.
            store.upsert_blob(hash_value, size=len(data), present=False)
            return False
    present = wrote or read_local(hash_value) is not None
    store.upsert_blob(hash_value, size=len(data), present=present)
    return present


# ---------------------------------------------------------------------------
# Fetching from peers
# ---------------------------------------------------------------------------

async def fetch_missing(limit: int = 20) -> int:
    ctx = engine.context()
    if ctx is None:
        return 0
    missing = await run_sync(store.missing_blobs, limit)
    if not missing:
        return 0
    fetched = 0
    online = transport.online_devices()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for blob in missing:
            candidates = [h for h in blob.get("holders") or [] if h in online and h != ctx.device_id]
            candidates += [d for d in online if d not in candidates and d != ctx.device_id]
            for holder in candidates:
                link = transport._links.get(holder)
                if link is None:
                    continue
                try:
                    await link.open(client, ctx)
                    from app.network import peer_files
                    data = await peer_files.fetch(client, link, blob['hash'])
                except (httpx.HTTPError, transport.PeerError, ValueError):
                    continue
                if data is None:
                    continue
                if await run_sync(store_fetched, blob["hash"], data):
                    fetched += 1
                    break
    return fetched


async def fetch_one(hash_value: str) -> bool:
    """Fetch a single blob from any online holder right now (an artifact download)."""
    ctx = engine.context()
    if ctx is None:
        return False
    if await run_sync(read_local, hash_value) is not None:
        return True
    existing = await run_sync(store.get_blob, hash_value)
    holders = list((existing or {}).get("holders") or [])
    online = transport.online_devices()
    candidates = [h for h in holders if h in online and h != ctx.device_id]
    candidates += [d for d in online if d not in candidates and d != ctx.device_id]
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for holder in candidates:
            link = transport._links.get(holder)
            if link is None:
                continue
            try:
                await link.open(client, ctx)
                from app.network import peer_files
                data = await peer_files.fetch(client, link, hash_value)
            except (httpx.HTTPError, transport.PeerError, ValueError):
                continue
            if data is not None:
                if await run_sync(store_fetched, hash_value, data):
                    return True
    return False


def blob_status() -> Dict[str, Any]:
    missing = store.missing_blobs(1000)
    return {"missing": len(missing), "bytes_missing": sum(int(b.get("size") or 0) for b in missing)}
