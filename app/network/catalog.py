"""Owner-ordered collections: the session catalog and transcript mirrors.

Only the device a session executes on publishes it. Each owned session's
row, tags, events and pending prompts travel as ordinary envelopes; the
event stream carries the owner's dense ``seq`` in ``order_seq`` so a mirror
appends in order and holds holes. A mirror is read-only: sending a message,
answering a prompt or interrupting goes to the owner through ``control``.

``publish_owned()`` runs from the scanner. It is incremental: sessions by
content hash, events by a per-session cursor (``network_catalog_cursors``).
Transcripts older than ``HISTORY_DAYS`` are published on demand only
(``hydrate``), so a large backlog never delays a sidebar width.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.db import network as store
from app.db.base import get_db
from app.network import engine, envelope as envelope_mod, policy

logger = logging.getLogger(__name__)

HISTORY_DAYS = 30
EVENT_BATCH = 400
# Peers refuse an envelope field over 4 MiB (``envelope._text``); the sealed
# payload is base64 of nonce + ciphertext + tag, so bound the plaintext at
# three quarters of that with room for the AEAD overhead.
MAX_EVENT_BYTES = (1 << 22) * 3 // 4 - 4096


def _owned_session_ids(cursor, device_id: str, *, recent_only: bool, include_held: bool = False,
                       unpublished_only: bool = False) -> List[str]:
    """Sessions this device publishes.

    Owned rows always. With ``include_held``, also rows this worker still
    holds for a legacy install that migrated to its own device but has not
    published the session itself yet (its origin never appears in
    ``sync_records`` for that row): the source of a shared-database cluster
    hands those over, and stops once the owner speaks.
    """
    sql = "SELECT id FROM sessions WHERE (owner_device_id = %s OR (owner_device_id IS NULL AND instance_id = %s))"
    params: List[Any] = [device_id, _my_instance_id()]
    if include_held:
        sql = ("SELECT s.id FROM sessions s LEFT JOIN sync_records r ON r.collection = 'sessions' AND r.record_id = s.id "
               "WHERE (s.owner_device_id = %s OR (s.owner_device_id IS NULL AND s.instance_id = %s) "
               "OR (s.owner_device_id IS NOT NULL AND s.owner_device_id <> %s AND s.instance_id IS NOT NULL "
               "AND (r.origin_device_id IS NULL OR r.origin_device_id = %s)))")
        params = [device_id, _my_instance_id(), device_id, device_id]
    if recent_only:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
        column = "s.updated_at" if include_held else "updated_at"
        sql += f" AND ({column} >= %s OR {column} IS NULL)"
        params.append(cutoff)
    if unpublished_only:
        # One indexed existence check per eligible session, rather than two
        # client/server round trips for every already-published transcript.
        # Missing/zero cursors still visit the publisher so a cleared stream
        # can retract its old tail before publishing replacement events.
        sql = (f"SELECT owned.id FROM ({sql}) owned "
               "LEFT JOIN network_catalog_cursors c ON c.session_id = owned.id "
               "WHERE c.published_seq IS NULL OR c.published_seq = 0 OR EXISTS ("
               "SELECT 1 FROM session_events e WHERE e.session_id = owned.id AND e.seq > c.published_seq)")
    cursor.execute(sql, tuple(params))
    return [r["id"] for r in cursor.fetchall()]


def _my_instance_id() -> str:
    from app.core.config import settings
    return settings.effective_instance_id


def publish_owned(*, ctx: Optional[engine.NetworkContext] = None) -> int:
    """Publish this device's sessions, tags, prompts and recent transcript tail."""
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    published = 0
    published += engine.scan_collection("sessions", ctx=ctx, owner_only=True)
    published += _publish_held_sessions(ctx)
    from app.network.session_runtime import publish_owned as publish_runtime
    published += publish_runtime(ctx)
    published += _publish_child_rows("session_tags", ctx)
    published += _publish_child_rows("pending_interactions", ctx)
    published += publish_events(ctx=ctx, recent_only=True)
    if published:
        engine.kick()
    return published


def _publish_held_sessions(ctx: engine.NetworkContext) -> int:
    """Hand migrated sessions to their owner device (shared-database cluster migration)."""
    coll = policy.collection("sessions")
    count = 0
    with get_db() as conn:
        cursor = conn.cursor()
        held = [sid for sid in _owned_session_ids(cursor, ctx.device_id, recent_only=False, include_held=True)]
        if not held:
            return 0
        cursor.execute("SELECT id, owner_device_id FROM sessions WHERE owner_device_id IS NOT NULL AND owner_device_id <> %s",
                       (ctx.device_id,))
        foreign = {r["id"] for r in cursor.fetchall()}
        for session_id in held:
            if session_id not in foreign:
                continue
            row = engine._read_row(cursor, coll, session_id)
            if row is None:
                continue
            existing = store.get_record(cursor, "sessions", session_id)
            if existing is not None and existing["origin_device_id"] != ctx.device_id:
                continue  # the owner has spoken
            if engine.record(cursor, "sessions", session_id, row, ctx=ctx) is not None:
                count += 1
    return count


def _publish_child_rows(collection: str, ctx: engine.NetworkContext) -> int:
    """Rows keyed by session (tags, prompts) for the sessions we own."""
    coll = policy.collection(collection)
    count = 0
    with get_db() as conn:
        cursor = conn.cursor()
        owned = set(_owned_session_ids(cursor, ctx.device_id, recent_only=False))
        # Even after the last owned session disappears, its previously
        # published children need tombstones. Peer-origin records stay intact.
        known = store.records_for_collection(cursor, collection)
        rows = []
        if owned:
            # Filter at the database boundary instead of loading every mirrored
            # tag/prompt payload. Keep the same ownership rule as `owned` above.
            cursor.execute(
                f"SELECT child.* FROM {coll.table} child JOIN sessions s ON s.id = child.session_id "
                "WHERE s.owner_device_id = %s OR (s.owner_device_id IS NULL AND s.instance_id = %s)",
                (ctx.device_id, _my_instance_id()))
            rows = [dict(r) for r in cursor.fetchall()]
        seen = set()
        for row in rows:
            if row.get("session_id") not in owned:
                continue
            plaintext = engine.serialize_row(coll, row)
            if plaintext is None:
                continue
            record_id = coll.record_id(plaintext)
            seen.add(record_id)
            existing = known.get(record_id)
            if existing is not None and not bool(existing["deleted"]) and \
                    existing["content_hash"] == engine.content_hash(coll, plaintext):
                continue
            if engine.record(cursor, collection, record_id, row, ctx=ctx) is not None:
                count += 1
        for record_id, existing in known.items():
            if record_id in seen or bool(existing["deleted"]) or existing["origin_device_id"] != ctx.device_id:
                continue
            if engine.record(cursor, collection, record_id, None, ctx=ctx) is not None:
                count += 1
    return count


def publish_events(*, ctx: Optional[engine.NetworkContext] = None, recent_only: bool = True,
                   session_ids: Optional[List[str]] = None, limit: int = EVENT_BATCH) -> int:
    """Append new transcript events of owned sessions to the outbox, in seq order."""
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY or engine.outbox_held():
        # While the outbox is held (see ``engine.hold_outbox``) nothing may
        # move the per-session cursor past events that were not published.
        return 0
    coll = policy.collection("session_events")
    count = 0
    with get_db() as conn:
        cursor = conn.cursor()
        targets = session_ids if session_ids is not None else _owned_session_ids(
            cursor, ctx.device_id, recent_only=recent_only, include_held=True, unpublished_only=True)
        for session_id in targets:
            if count >= limit:
                break
            published_seq = store.catalog_cursor(cursor, session_id)
            if published_seq == 0:
                # A cleared transcript restarts at seq 1 (its cursor row is
                # gone): retract what the network still holds beyond the new
                # end before the fresh events replace the low seqs.
                count += _retract_stale_events(cursor, session_id, ctx)
            cursor.execute(
                "SELECT * FROM session_events WHERE session_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                (session_id, published_seq, max(1, limit - count)))
            rows = [dict(r) for r in cursor.fetchall()]
            skipped = 0
            for row in rows:
                record_id = coll.record_id(row)
                # One event the envelope cannot carry (over the size cap)
                # must not freeze this session or the ones after it.
                try:
                    plaintext = engine.serialize_row(coll, row)
                    if plaintext is not None and len(envelope_mod.record_bytes(plaintext)) > MAX_EVENT_BYTES:
                        raise envelope_mod.EnvelopeError("Event exceeds the envelope size cap")
                    if engine.record(cursor, "session_events", record_id, row, ctx=ctx, order_seq=int(row["seq"])) is not None:
                        count += 1
                except (envelope_mod.EnvelopeError, engine.ApplyError, ValueError, TypeError):
                    skipped += 1
                published_seq = int(row["seq"])
            if skipped:
                logger.warning("Skipped %d unpublishable transcript event(s) of session %s", skipped, session_id)
            if rows:
                store.set_catalog_cursor(cursor, session_id, published_seq)
    if count:
        engine.kick()
    return count


def _retract_stale_events(cursor, session_id: str, ctx: engine.NetworkContext) -> int:
    """Delete published events past the transcript's current end (a restarted stream)."""
    cursor.execute("SELECT MAX(seq) AS s FROM session_events WHERE session_id = %s", (session_id,))
    row = cursor.fetchone()
    max_seq = int(row["s"]) if row and row["s"] is not None else 0
    cursor.execute(
        "SELECT record_id FROM sync_records WHERE collection = 'session_events' AND deleted = FALSE "
        "AND order_seq > %s AND record_id LIKE %s", (max_seq, session_id + "|%"))
    stale = [r["record_id"] for r in cursor.fetchall() if r["record_id"].split("|", 1)[0] == session_id]
    count = 0
    for record_id in stale:
        if engine.record(cursor, "session_events", record_id, None, ctx=ctx) is not None:
            count += 1
    return count


def hydrate(session_id: str) -> int:
    """Publish a whole transcript on demand (a peer asked for an old session)."""
    ctx = engine.context()
    if ctx is None:
        return 0
    with get_db() as conn:
        cursor = conn.cursor()
        if session_id not in _owned_session_ids(cursor, ctx.device_id, recent_only=False):
            return 0
    return publish_events(ctx=ctx, session_ids=[session_id], limit=1_000_000)


# ---------------------------------------------------------------------------
# Mirror-side hooks (called by the engine after materializing a record)
# ---------------------------------------------------------------------------

def prepare_session_mirror(cursor, record: Dict[str, Any]) -> Dict[str, Any]:
    """A mirrored session must not reference local-only rows it cannot see."""
    record = dict(record)
    project_id = record.get("project_id")
    if project_id:
        cursor.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
        if cursor.fetchone() is None:
            record["project_id"] = None
    profile_id = record.get("profile_id")
    if profile_id:
        cursor.execute("SELECT 1 FROM agents WHERE id = %s", (profile_id,))
        if cursor.fetchone() is None:
            record["profile_id"] = None
    api_user_id = record.get("api_user_id")
    if api_user_id:
        cursor.execute("SELECT 1 FROM api_users WHERE id = %s", (api_user_id,))
        if cursor.fetchone() is None:
            record["api_user_id"] = None
    parent = record.get("parent_session_id")
    if parent:
        cursor.execute("SELECT 1 FROM sessions WHERE id = %s", (parent,))
        if cursor.fetchone() is None:
            record["parent_session_id"] = None
    return record


def after_event_mirrored(cursor, record: Dict[str, Any]) -> None:
    """Keep the mirror's seq counter honest and wake the event gateway."""
    session_id = record.get("session_id")
    seq = int(record.get("seq") or 0)
    if not session_id or not seq:
        return
    cursor.execute("SELECT next_seq FROM session_event_counters WHERE session_id = %s", (session_id,))
    row = cursor.fetchone()
    if row is not None and int(row["next_seq"]) > seq:
        # The owner restarted its stream (a cleared transcript): the row at
        # this seq was replaced, and the counter must follow what is left.
        cursor.execute("SELECT MAX(seq) AS s FROM session_events WHERE session_id = %s", (session_id,))
        top = cursor.fetchone()
        seq = max(seq, int(top["s"]) if top and top["s"] is not None else 0)
        cursor.execute("UPDATE session_event_counters SET next_seq = %s WHERE session_id = %s", (seq, session_id))
        store.set_catalog_cursor(cursor, session_id, seq)
    else:
        cursor.execute(
            """INSERT INTO session_event_counters (session_id, next_seq) VALUES (%s, %s)
               ON CONFLICT (session_id) DO UPDATE SET next_seq = CASE
                   WHEN session_event_counters.next_seq > EXCLUDED.next_seq THEN session_event_counters.next_seq
                   ELSE EXCLUDED.next_seq END""",
            (session_id, seq))
        # Should this device become the owner later (a migrated session), it
        # publishes from here on instead of re-sending what the network has.
        if store.catalog_cursor(cursor, session_id) < seq:
            store.set_catalog_cursor(cursor, session_id, seq)
    try:
        from app.db.session_events import NOTIFY_CHANNEL
        cursor.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, json.dumps({"session_id": session_id, "max_seq": seq})))
    except Exception:  # noqa: BLE001 — absent on the SQLite shim; the gateway also polls
        pass


def transcript_text(session_id: str, *, limit: int = 400) -> str:
    """Plain-text rendering of the last ``limit`` events of a mirrored transcript (for a fork's context)."""
    from app.db.session_events import _parse_payload
    lines: List[str] = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT event_type, payload FROM session_events WHERE session_id = %s ORDER BY seq DESC LIMIT %s",
            (session_id, limit))
        events = [dict(r) for r in cursor.fetchall()][::-1]
    for event in events:
        event["payload"] = _parse_payload(event.get("payload"))
        payload = event.get("payload") or {}
        kind = event.get("event_type") or payload.get("type")
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if kind == "message.user":
            lines.append("User: " + content.strip())
        elif kind == "message.assistant" and payload.get("kind", "text") == "text":
            lines.append("Assistant: " + content.strip())
    return "\n\n".join(lines)
