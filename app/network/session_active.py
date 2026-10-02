"""Owner-authoritative Active/Settled changes and ordered read-back."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.db import database
from app.db.async_database import run_sync
from app.db.base import get_db
from app.network import control, engine, membership, transport

logger = logging.getLogger(__name__)


def _write(session_id: str, active: bool, *, local: bool = False) -> tuple[dict, Optional[dict]]:
    """Change membership on the owner; ``local`` marks an edit from this worker's own UI.

    A peer needs the signed envelope to update its mirror, so a remote edit
    that cannot be published yet is refused. A local edit needs no envelope:
    while the outbox is held (startup until a peer has compared streams, which
    lasts as long as every peer is offline) it is committed like any other
    local write and the scanner publishes the row once publishing resumes.
    """
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
        if envelope is None and not local:
            # Do not commit a remote edit that cannot be published yet.
            raise PermissionError(
                "The worker that runs this session is not ready yet: it is still syncing "
                "with the other workers. Try again in a minute."
            )
    engine.kick()
    return row, envelope


async def handle(body: dict, from_device: str) -> dict:
    session_id, active = body.get("session_id"), body.get("is_active")
    if not isinstance(session_id, str) or not session_id or not isinstance(active, bool):
        raise ValueError("session_id and boolean is_active are required")
    row, envelope = await run_sync(_write, session_id, active)
    await _broadcast(row, session_id, active)
    # Only the signed catalog representation crosses the worker boundary.
    return {"envelope": envelope}


async def set_local(session_id: str, active: bool) -> dict:
    """Settle or reopen a session this worker owns, for its own UI."""
    row, _envelope = await run_sync(_write, session_id, active, local=True)
    await _broadcast(row, session_id, active)
    return row


async def _broadcast(row: dict, session_id: str, active: bool) -> None:
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_updated(
            user_id=row.get("api_user_id") or "admin", session_id=session_id,
            updates={"is_active": active}, source_device_id=None,
        )
    except Exception:  # durable update already committed
        logger.warning("Could not broadcast settlement for %s", session_id, exc_info=True)


def _offline_message(device_id: str, reason: str) -> Optional[str]:
    """Plain wording for an owner that is offline; None when it was removed."""
    ctx = engine.context()
    member = ctx.member(device_id) if ctx is not None else None
    if member is not None and member.status != membership.STATUS_ACTIVE:
        return None  # control.call already explains a removed worker
    name = (getattr(member, "name", None) or "").strip()
    who = f"{name}, the worker that runs this session," if name else "The worker that runs this session"
    if reason.startswith("HTTP 500"):
        # The owner was reached and failed; "can't be reached" would send the
        # user looking for a network problem that is not there.
        return f"{who} hit an error while updating it (HTTP 500). Its logs have the details."
    return f"{who} can't be reached right now. Try again once it is back online."


async def set_remote(owner: str, session_id: str, active: bool) -> dict:
    try:
        response = await control.call(owner, "session-active", {
            "session_id": session_id, "is_active": active,
        })
    except control.RunnerUnreachable as exc:
        # The transport reason ("PeerError: HTTP 530") says nothing to the
        # person settling the session; name the worker they need back online.
        message = _offline_message(owner, str(exc))
        if message is None:
            raise
        logger.info("Owner %s unreachable for settlement of %s: %s", owner, session_id, exc)
        raise control.RunnerUnreachable(message) from exc
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
