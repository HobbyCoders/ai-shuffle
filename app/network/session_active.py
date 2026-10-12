"""Shared Active/Settled membership, independent of execution ownership.

The owner-control protocol remains for older workers. New UIs write a shared
record locally; normal signed network replication reconciles it on reconnect.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.db import database
from app.db.async_database import run_sync
from app.db import network as store
from app.db.base import get_db
from app.network import control, engine, membership, transport

logger = logging.getLogger(__name__)


def project(cursor, record: dict) -> None:
    """Project shared membership without touching execution or recency.

    There is deliberately no FK: a membership envelope can precede the session
    catalog on a third worker. prepare_session_mirror applies it on insertion.
    """
    cursor.execute("UPDATE sessions SET is_active=%s WHERE id=%s",
                   (record["is_active"], record["session_id"]))


def _record_active(cursor, session_id: str, active: bool) -> Optional[dict]:
    record = {"session_id": session_id, "is_active": active,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    cursor.execute(
        "INSERT INTO network_session_active (session_id, is_active, updated_at, local_pending) VALUES (%s, %s, %s, TRUE) "
        "ON CONFLICT (session_id) DO UPDATE SET is_active=EXCLUDED.is_active, updated_at=EXCLUDED.updated_at, local_pending=TRUE",
        (session_id, active, record["updated_at"]),
    )
    project(cursor, record)
    # While publishing is held, the scanner signs this durable row later.
    return engine.record(cursor, "session_active", session_id, record, force=True)


def write_shared(session_id: str, active: bool) -> Optional[dict]:
    """Commit a sidebar edit here, even when its execution owner is offline."""
    if not isinstance(active, bool):
        raise ValueError("boolean is_active is required")
    with get_db() as conn:
        cur = conn.cursor()
        # Incoming replication locks network_state before projecting sessions.
        # Use the same order so a simultaneous sidebar click cannot deadlock.
        store.lock_state(cur)
        cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        if cur.fetchone() is None:
            return None
        if not active:
            from app.db.assistant import assert_can_settle
            assert_can_settle(conn, session_id)
        _record_active(cur, session_id, active)
        cur.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
        row = dict(cur.fetchone())
    engine.kick()
    return row


def write_shared_many(session_ids: list[str], active: bool) -> list[dict]:
    """``write_shared`` for many sessions: one transaction, one state lock.

    Any error rolls the whole batch back, so callers can retry item by item.
    Unknown ids are skipped and absent from the result.
    """
    if not isinstance(active, bool):
        raise ValueError("boolean is_active is required")
    rows = []
    with get_db() as conn:
        cur = conn.cursor()
        store.lock_state(cur)
        for session_id in session_ids:
            cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
            if cur.fetchone() is None:
                continue
            if not active:
                from app.db.assistant import assert_can_settle
                assert_can_settle(conn, session_id)
            _record_active(cur, session_id, active)
            cur.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
            rows.append(dict(cur.fetchone()))
    engine.kick()
    return rows


async def set_shared(session_id: str, active: bool) -> dict:
    row = await run_sync(write_shared, session_id, active)
    if row is None:
        raise LookupError("Unknown session")
    await _apply_settle_change(row, active)
    await _broadcast(row, session_id)
    return row


async def _apply_settle_change(row: dict, active: bool, archived: Optional[bool] = None) -> None:
    """Dismiss waits on the owner; shared membership remains offline-first."""
    if active or archived is not None or row.get("status") == "archived":
        return
    owner = control.owner_of(row)
    try:
        if owner is None:
            from app.core.session_settlement import dismiss_owner_waits
            if await dismiss_owner_waits(row["id"]):
                row.update(await run_sync(database.get_session, row["id"]))
        else:
            # A fast path only: replication also dismisses waits on the owner.
            # No reachable address means no control call (idle/offline settle
            # remains a local membership write, as before).
            from app.network.endpoints import peer_endpoints
            member = await run_sync(store.get_member, owner)
            if member and peer_endpoints(member, engine.presence_of(owner)):
                async with asyncio.timeout(3):
                    await set_remote(owner, row["id"], False)
    except Exception:
        logger.warning("Could not dismiss owner waits for settled session %s", row["id"], exc_info=True)


async def dismiss_replicated(session_ids: list[str]) -> None:
    """After shared records commit, act only on locally owned, settled chats."""
    from app.core.session_settlement import dismiss_owner_waits
    for session_id in session_ids:
        row = await run_sync(database.get_session, session_id)
        if row and not row.get("is_active") and control.owner_of(row) is None:
            try:
                await dismiss_owner_waits(session_id)
                await _broadcast(row, session_id)
            except Exception:
                logger.warning("Could not dismiss replicated owner wait for %s", session_id, exc_info=True)


def _write(session_id: str, active: bool, *, archived: Optional[bool] = None,
           local: bool = False) -> tuple[dict, Optional[dict]]:
    """Change membership on the owner; ``local`` marks an edit from this worker's own UI.

    A peer needs the signed envelope to update its mirror, so a remote edit
    that cannot be published yet is refused. A local edit needs no envelope:
    while the outbox is held (startup until a peer has compared streams, which
    lasts as long as every peer is offline) it is committed like any other
    local write and the scanner publishes the row once publishing resumes.
    """
    with get_db() as conn:
        cur = conn.cursor()
        store.lock_state(cur)
        # Serialize with ownership transfer and other membership changes.
        cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError("Unknown session")
        if control.owner_of(dict(row)) is not None:
            raise PermissionError("This worker does not own the session")
        if archived or not active:
            from app.db.assistant import assert_can_settle
            assert_can_settle(conn, session_id)
        # Older peers still ask the execution owner. Write the shared record
        # too so their edits participate in the same conflict order.
        active = False if archived else active
        _record_active(cur, session_id, active)
        status_sql, status_params = database.archive_status_sql(archived, "%s")
        cur.execute(f"UPDATE sessions SET is_active=%s, updated_at=%s{status_sql} WHERE id=%s RETURNING *",
                    (active, datetime.now(timezone.utc).isoformat(), *status_params, session_id))
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
    archived = body.get("archived")
    if not isinstance(session_id, str) or not session_id or not isinstance(active, bool):
        raise ValueError("session_id and boolean is_active are required")
    if archived is not None and not isinstance(archived, bool):
        raise ValueError("archived must be a boolean when given")
    view = body.get("owner_watermarks")
    if engine.outbox_held() and isinstance(view, dict):
        # Startup sync: our outbox is held until a peer has compared streams
        # with us. The asking peer's view of our stream is that comparison
        # (#1621); without it the request is refused below as before.
        await run_sync(transport.release_hold_for_peer_view, from_device, view)
    restoring = archived is False and await _is_archived(session_id)
    row, envelope = await run_sync(lambda: _write(session_id, active, archived=archived))
    await _apply_settle_change(row, active, archived)
    await _broadcast(row, session_id)
    await _apply_archive_change(session_id, archived, restoring)
    # Only the signed catalog representation crosses the worker boundary.
    return {"envelope": envelope}


class MembershipUpdateFailed(RuntimeError):
    """The local writer did not return an updated session."""


async def update_membership(existing: dict, active: bool, archived: Optional[bool] = None) -> dict:
    """Apply an authorized UI or tool edit through the same owner/sync paths."""
    session_id = existing["id"]
    if engine.context() is not None:
        if archived is None:
            return await set_shared(session_id, active)
        owner = control.owner_of(existing)
        if owner is not None:
            await set_remote(owner, session_id, active, archived)
            # Prevent a stale local membership record overriding the owner reply.
            return await set_shared(session_id, False if archived else active)
        return await set_local(session_id, active, archived)
    row = await run_sync(database.set_session_active, session_id, active, archived)
    if row is None:
        raise MembershipUpdateFailed("Failed to update active status")
    await _apply_settle_change(row, active, archived)
    await _broadcast(row, session_id)
    await _apply_archive_change(session_id, archived, existing.get("status") == "archived")
    return row


async def set_local(session_id: str, active: bool, archived: Optional[bool] = None) -> dict:
    """Settle, reopen, archive or restore a session this worker owns, for its own UI."""
    restoring = archived is False and await _is_archived(session_id)
    row, _envelope = await run_sync(lambda: _write(session_id, active, archived=archived, local=True))
    await _apply_settle_change(row, active, archived)
    await _broadcast(row, session_id)
    await _apply_archive_change(session_id, archived, restoring)
    return row


async def _is_archived(session_id: str) -> bool:
    from app.core import archive_guard

    return await archive_guard.is_archived_async(session_id)


async def _apply_archive_change(session_id: str, archived: Optional[bool],
                                restored: bool = False) -> None:
    """On the execution owner, after an archive or restore committed (#1822).

    Archive stops the chat's running and parked work so nothing is left to wake
    it; it runs after the write, so every wake producer already reads the chat
    as archived while the stop is in progress. Restore makes the chat fully
    usable again by releasing the Stop latch archiving set; ``restored`` says the
    chat really was archived, so an ``archived=False`` write to a chat that was
    not leaves a Stop the owner pressed alone. ``None`` (a plain settle or
    reopen) changes nothing.
    """
    from app.core import archive_guard

    if archived:
        await archive_guard.stop_work(session_id)
    elif archived is False and restored:
        archive_guard.release_after_restore(session_id)


async def _broadcast(row: dict, session_id: str) -> None:
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_updated(
            user_id=row.get("api_user_id") or "admin", session_id=session_id,
            updates={"is_active": bool(row.get("is_active")), "status": row.get("status")},
            source_device_id=None,
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


def _view_of(owner: str) -> dict:
    """What this worker holds of the owner's stream, as an exchange would send it."""
    return store.local_watermarks().get(owner) or {}


async def set_remote(owner: str, session_id: str, active: bool, archived: Optional[bool] = None) -> dict:
    try:
        payload = {
            "session_id": session_id, "is_active": active,
            # Lets an owner whose outbox is still held after a restart compare
            # streams with us and sign the change (owners before #1621 ignore it).
            "owner_watermarks": await run_sync(_view_of, owner),
        }
        if archived is not None:
            payload["archived"] = archived
        response = await control.call(owner, "session-active", payload)
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
