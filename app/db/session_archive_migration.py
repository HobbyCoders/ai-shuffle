"""Retire chat archive, retaining signed history and real activity timestamps.

Legacy catalog values are projections, not new membership writes. A newer shared
membership always wins. Requests, in contrast, are new ordinary settle intents.
"""
import json
from uuid import NAMESPACE_URL, uuid4, uuid5
from app.db.base import table_exists, column_exists


def canonical_session(record):
    record = dict(record)
    if record.get("status") == "archived":
        record.update(status="active", is_active=False)
    return record


def migrate(cursor):
    if not table_exists(cursor, "sessions"):
        return []
    # A durable handoff to runtime settlement, in the same transaction as conversion.
    cursor.execute("""CREATE TABLE IF NOT EXISTS session_archive_retirement (
        session_id TEXT PRIMARY KEY, membership_revision BIGINT NOT NULL,
        membership_epoch TEXT NOT NULL, done BOOLEAN NOT NULL DEFAULT FALSE,
        cancelled_peers TEXT NOT NULL DEFAULT '[]',
        cancelled_wakes TEXT NOT NULL DEFAULT '[]')""")
    if not column_exists(cursor, "session_archive_retirement", "cancelled_peers"):
        cursor.execute("ALTER TABLE session_archive_retirement ADD COLUMN cancelled_peers TEXT NOT NULL DEFAULT '[]'")
    if not column_exists(cursor, "session_archive_retirement", "cancelled_wakes"):
        cursor.execute("ALTER TABLE session_archive_retirement ADD COLUMN cancelled_wakes TEXT NOT NULL DEFAULT '[]'")
    cursor.execute("SELECT id FROM sessions WHERE status='archived' ORDER BY id FOR UPDATE")
    ids = [row["id"] for row in cursor.fetchall()]
    for sid in ids:
        cursor.execute("""INSERT INTO network_session_active
            (session_id,is_active,updated_at,local_pending,local_revision,local_revision_epoch)
            SELECT id,FALSE,updated_at,TRUE,1,%s FROM sessions WHERE id=%s
            ON CONFLICT (session_id) DO UPDATE SET is_active=FALSE,local_pending=TRUE,
            local_revision=network_session_active.local_revision+1,
            local_revision_epoch=CASE WHEN network_session_active.local_revision_epoch=''
                THEN EXCLUDED.local_revision_epoch ELSE network_session_active.local_revision_epoch END
            RETURNING local_revision,local_revision_epoch""", (str(uuid4()), sid))
        revision = cursor.fetchone()
        cursor.execute("""INSERT INTO session_archive_retirement
            (session_id,membership_revision,membership_epoch) VALUES (%s,%s,%s)
            ON CONFLICT (session_id) DO UPDATE SET membership_revision=EXCLUDED.membership_revision,
            membership_epoch=EXCLUDED.membership_epoch,done=FALSE""",
            (sid, revision["local_revision"], revision["local_revision_epoch"]))
        cursor.execute("UPDATE sessions SET status='active',is_active=FALSE WHERE id=%s", (sid,))
        # Old archive queues are evidence, never invitations to resume on boot.
        cursor.execute("""UPDATE turns SET state='aborted',stop_reason='removed_from_queue',ended_at=NOW()
            WHERE session_id=%s AND state='queued' RETURNING id,source""", (sid,))
        peers = [row["id"] for row in cursor.fetchall() if row["source"] == "agent_message"]
        # A legacy archive could retain timers whose guard used to refuse execution.
        # Remove only executable inputs, retain their full evidence on the receipt.
        # Pure cancellation notices are not turns and may still be delivered.
        cursor.execute("""DELETE FROM pending_wakeups WHERE session_id=%s
            AND source <> 'profile_cancelled' RETURNING *""", (sid,))
        wakes = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""UPDATE session_archive_retirement SET cancelled_peers=%s,
            cancelled_wakes=%s WHERE session_id=%s""", (json.dumps(peers), json.dumps(wakes, default=str), sid))
    return [f"sessions.archive_retired({len(ids)})"] if ids else []


async def reconcile():
    """Before boot recovery admits work, process a crash-safe, revision-fenced receipt.

    Keep failed markers for the next boot. Explicit Stop latches are never touched.
    Mirrored work belongs to its execution owner; it receives shared membership.
    """
    from app.db.base import get_db
    from app.db.async_database import run_sync
    from app.core.session_settlement import dismiss_owner_waits

    def pending():
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM session_archive_retirement WHERE NOT done")
            return [dict(row) for row in cur.fetchall()]

    def finish(sid):
        with get_db() as conn:
            conn.cursor().execute("UPDATE session_archive_retirement SET done=TRUE WHERE session_id=%s", (sid,))

    for row in await run_sync(pending):
        from app.core.wakeup_service import _disarm
        _disarm([wake["id"] for wake in json.loads(row["cancelled_wakes"])])
        peers = json.loads(row["cancelled_peers"])
        if peers:
            from app.db.session_events import append_events
            event_id = str(uuid5(NAMESPACE_URL,
                f"archive-retired:{row['session_id']}:{row['membership_epoch']}:{row['membership_revision']}"))
            await run_sync(append_events, row["session_id"], [{"id": event_id,
                "type": "queue.cancelled", "payload": {"cancelled_turn_ids": peers}}])
        await dismiss_owner_waits(row["session_id"], expected_revision={
            "membership_revision": row["membership_revision"], "membership_epoch": row["membership_epoch"]})
        await run_sync(finish, row["session_id"])
