"""Owner-authoritative Active/Settled changes and ordered read-back."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.db import database
from app.db.async_database import run_sync
from app.db.base import get_db
from app.network import control, engine, transport

logger = logging.getLogger(__name__)


def _write(session_id: str, active: bool) -> tuple[dict, dict]:
    with get_db() as conn:
        cur = conn.cursor()
        # Serialize with ownership transfer and other membership changes.
        cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError("Unknown session")
        if control.owner_of(dict(row)) is not None:
            raise PermissionError("This worker does not own the session")
        cur.execute("UPDATE sessions SET is_active=%s, updated_at=%s WHERE id=%s RETURNING *",
                    (active, datetime.now(timezone.utc).isoformat(), session_id))
        row = dict(cur.fetchone())
        envelope = engine.record(cur, "sessions", session_id, row, force=True)
        if envelope is None:
            # Do not commit a remote edit that cannot be published yet.
            raise PermissionError("Owner synchronization is not ready; retry after it finishes")
    engine.kick()
    return row, envelope


async def handle(body: dict, from_device: str) -> dict:
    session_id, active = body.get("session_id"), body.get("is_active")
    if not isinstance(session_id, str) or not session_id or not isinstance(active, bool):
        raise ValueError("session_id and boolean is_active are required")
    row, envelope = await run_sync(_write, session_id, active)
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_updated(
            user_id=row.get("api_user_id") or "admin", session_id=session_id,
            updates={"is_active": active}, source_device_id=None,
        )
    except Exception:  # durable update already committed
        logger.warning("Could not broadcast remote settlement for %s", session_id, exc_info=True)
    # Only the signed catalog representation crosses the worker boundary.
    return {"envelope": envelope}


async def set_remote(owner: str, session_id: str, active: bool) -> dict:
    response = await control.call(owner, "session-active", {
        "session_id": session_id, "is_active": active,
    })
    envelope = response.get("envelope")
    if (not isinstance(envelope, dict) or envelope.get("collection") != "sessions"
            or envelope.get("record_id") != session_id or envelope.get("origin_device_id") != owner
            or envelope.get("op") != "upsert"):
        raise control.ControlRefused("Owner returned an invalid session update")
    result = await run_sync(engine.apply_batch, [envelope])
    if result.rejected or result.retry:
        raise control.ControlRefused("Owner saved the change, but its signed update could not be applied")
    if result.pending:
        # The reply may overtake earlier owner operations. Fill the real stream
        # rather than skipping its holes or overwriting the mirror out of order.
        try:
            async with asyncio.timeout(control.REQUEST_TIMEOUT):
                link = transport._links.get(owner)
                if link is None:
                    raise transport.PeerError("Owner disconnected before synchronization")
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    await link.sync_once(client, engine.require_context(), catch_up=True)
        except (transport.PeerError, httpx.HTTPError, TimeoutError) as exc:
            raise control.RunnerUnreachable(
                "Owner saved the change, but synchronization is incomplete; reconnect and refresh"
            ) from exc
    row = await run_sync(database.get_session, session_id)
    if row is None:
        raise control.ControlRefused("The session no longer exists after synchronization")
    return row
