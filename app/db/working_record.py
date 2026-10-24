"""Owner working items; independent rows sync without replacing the whole record.

The worker network belongs to one owner. @owner is its canonical identity (not
an API user or a worker-local authentication id). No session FK: reports and
choices must survive settling/deleting the conversation that produced them.
"""
from datetime import UTC, datetime
from uuid import uuid4

from app.db.base import get_db

OWNER = "@owner"
KINDS = ("goal", "decision", "pending", "needs_owner", "report")


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS owner_working_items (
        id TEXT PRIMARY KEY, owner_key TEXT NOT NULL,
        kind TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
        source_session_id TEXT, source_turn_id TEXT,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL)""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_working_items_owner "
                   "ON owner_working_items(owner_key, status, updated_at)")
    # A local admission receipt, not shared owner state. Source session ownership
    # already serializes hand-off. Fresh context is never a fork of the old SDK id.
    cursor.execute("""CREATE TABLE IF NOT EXISTS coordinator_handoffs (
        source_session_id TEXT PRIMARY KEY, successor_session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL)""")


def add(kind, content, session_id, *, item_id=None, turn_id=None):
    if kind not in KINDS:
        raise ValueError("Unknown working-record kind")
    if not isinstance(content, str) or not content.strip() or len(content) > 2000 or "\x00" in content:
        raise ValueError("content must be 1–2000 characters without NUL")
    now = datetime.now(UTC).isoformat()
    item_id = item_id or str(uuid4())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO owner_working_items
            (id, owner_key, kind, content, status, source_session_id, source_turn_id, created_at, updated_at)
            VALUES (%s,%s,%s,%s,'open',%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING""",
            (item_id, OWNER, kind, content.strip(), session_id, turn_id, now, now))
    _changed(item_id)
    return item_id


def resolve(item_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE owner_working_items SET status='resolved', updated_at=%s "
                    "WHERE id=%s AND owner_key=%s AND status='open' RETURNING id",
                    (datetime.now(UTC).isoformat(), item_id, OWNER))
        changed = cur.fetchone() is not None
    _changed(item_id)
    return changed


def items(*, include_resolved=False, limit=100, offset=0, kind=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM owner_working_items WHERE owner_key=%s "
                    + ("" if include_resolved else "AND status='open' ")
                    + ("AND kind=%s " if kind else "")
                    + "ORDER BY updated_at DESC, id LIMIT %s OFFSET %s",
                    (OWNER, *((kind,) if kind else ()), limit, offset))
        return [dict(row) for row in cur.fetchall()]


def _changed(item_id):
    # The regular scanner retries while publication is held/offline. Kick it,
    # just as other shared owner edits do; don't require a peer to save an item.
    import logging

    from app.network import engine
    try:
        engine.scan_collection("working_record", record_ids=[item_id])
    except Exception:
        logging.getLogger(__name__).warning("Working-record sync deferred", exc_info=True)
    engine.kick()
