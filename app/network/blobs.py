"""Content-addressed blobs: artifacts and note attachments, fetched on demand.

A synced record names a hash; the bytes are not in the envelope. Every
device that holds the bytes serves them at ``/peer/blob/{hash}``; a device
that materialized a record without bytes asks online holders (the owner
first) and stores what it gets. ``sync_blobs`` tracks what is present and
who else has it.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

import httpx

from app.db import network as store
from app.db.base import get_db
from app.network import engine, transport

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
            if not data:
                continue
            digest = sha256(bytes(data))
            cursor.execute("UPDATE note_attachments SET blob_hash = %s WHERE id = %s", (digest, row["id"]))
            store.upsert_blob(digest, size=len(data), present=True, _conn=conn)
            updated += 1
    return updated


def register_present(hash_value: str, size: int) -> None:
    ctx = engine.context()
    store.upsert_blob(hash_value, size=size, present=True, holder=ctx.device_id if ctx else None)


def read_local(hash_value: str) -> Optional[bytes]:
    """Bytes for a hash from the artifact store or a note attachment, else None."""
    try:
        from app.core.artifact_store import artifact_path
        path = artifact_path(hash_value)
        if path is not None:
            return path.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM note_attachments WHERE blob_hash = %s AND data IS NOT NULL LIMIT 1", (hash_value,))
        row = cursor.fetchone()
    if row and row["data"] is not None:
        data = row["data"]
        return data.tobytes() if isinstance(data, memoryview) else bytes(data)
    return None


def note_missing(hash_value: str, size: int, holder: Optional[str]) -> None:
    """A record arrived whose bytes we do not have."""
    if read_local(hash_value) is not None:
        store.upsert_blob(hash_value, size=size, present=True, holder=holder)
        return
    store.upsert_blob(hash_value, size=size, present=False, holder=holder)


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
    store.upsert_blob(hash_value, size=len(data), present=True)
    return wrote


# ---------------------------------------------------------------------------
# Fetching from peers
# ---------------------------------------------------------------------------

async def fetch_missing(limit: int = 20) -> int:
    ctx = engine.context()
    if ctx is None:
        return 0
    missing = store.missing_blobs(limit)
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
                    await link.authenticate(client, ctx)
                    response = await client.get(link.base_url + f"/api/v1/network/peer/blob/{blob['hash']}",
                                                headers={"Authorization": "Bearer " + (link.token or "")},
                                                timeout=120.0)
                except (httpx.HTTPError, transport.PeerError):
                    continue
                if response.status_code != 200 or len(response.content) > MAX_BLOB_BYTES:
                    continue
                if store_fetched(blob["hash"], response.content):
                    fetched += 1
                    break
    return fetched


async def fetch_one(hash_value: str) -> bool:
    """Fetch a single blob from any online holder right now (an artifact download)."""
    ctx = engine.context()
    if ctx is None:
        return False
    if read_local(hash_value) is not None:
        return True
    existing = store.get_blob(hash_value)
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
                await link.authenticate(client, ctx)
                response = await client.get(link.base_url + f"/api/v1/network/peer/blob/{hash_value}",
                                            headers={"Authorization": "Bearer " + (link.token or "")}, timeout=120.0)
            except (httpx.HTTPError, transport.PeerError):
                continue
            if response.status_code == 200 and len(response.content) <= MAX_BLOB_BYTES:
                if store_fetched(hash_value, response.content):
                    return True
    return False


def blob_status() -> Dict[str, Any]:
    missing = store.missing_blobs(1000)
    return {"missing": len(missing), "bytes_missing": sum(int(b.get("size") or 0) for b in missing)}
