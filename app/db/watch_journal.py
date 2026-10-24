"""Bounded Telegram ingress with durable receiver fences and transferable tails."""
from __future__ import annotations

from typing import Any

from app.core.watch_values import encode
from app.db.base import get_db

MAX_ROWS = 1000
EXPIRED = "Telegram journal retention exceeded; create a new watch to opt in again"


def schema(c: Any) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS watch_connector_journals (
        connector_id TEXT PRIMARY KEY, high_seq BIGINT NOT NULL DEFAULT 0,
        floor_seq BIGINT NOT NULL DEFAULT 0, fence TEXT NOT NULL DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS watch_connector_heads (
        connector_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id BIGINT NOT NULL,
        PRIMARY KEY(connector_id,chat_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_watch_inbox_connector_seq "
              "ON watch_connector_inbox(connector_id,seq)")


def lock(c: Any, cid: str) -> dict[str, Any]:
    from app.db.watches import lock_connector

    lock_connector(c, cid)
    c.execute("INSERT INTO watch_connector_journals (connector_id,high_seq) "
              "SELECT %s,COALESCE(MAX(seq),0) FROM watch_connector_inbox WHERE connector_id=%s "
              "ON CONFLICT(connector_id) DO NOTHING", (cid, cid))
    c.execute("SELECT * FROM watch_connector_journals WHERE connector_id=%s", (cid,))
    return dict(c.fetchone())


def consumers(c: Any, cid: str) -> list[dict[str, Any]]:
    from app.db.watches import parse

    # Keep the predicate portable to the SQLite test adapter. Lock ordering is
    # always connector, then watch; checkpoint/export use the same ordering.
    c.execute("SELECT * FROM watches WHERE status IN ('active','paused')")
    rows = [parse(row) for row in c.fetchall()]
    return [row for row in rows if row["source"].get("connector_id") == cid
            and not row["worker_id"].startswith("transfer:")
            and not row["cursor"].get("expired")]


def trim(c: Any, cid: str) -> None:
    """Expire laggards explicitly before deleting content they still needed."""
    state = lock(c, cid)
    c.execute("SELECT seq FROM watch_connector_inbox WHERE connector_id=%s "
              "ORDER BY seq DESC LIMIT 1 OFFSET %s", (cid, MAX_ROWS))
    excess = c.fetchone()
    cutoff = int(excess["seq"]) if excess else int(state["floor_seq"])
    rows = consumers(c, cid)
    for row in rows:
        seq = int(row["cursor"].get("seq", state["high_seq"]))
        if seq < cutoff:
            c.execute("UPDATE watches SET status='paused',reason=%s,cursor=%s,"
                      "revision=revision+1 WHERE id=%s", (
                          EXPIRED, encode(dict(row["cursor"], expired=True)), row["id"],
                      ))
    remaining = [int(row["cursor"].get("seq", state["high_seq"])) for row in rows
                 if int(row["cursor"].get("seq", state["high_seq"])) >= cutoff]
    passed = min(remaining, default=int(state["high_seq"]))
    from app.db.watches import parse

    while True:
        c.execute("SELECT * FROM watch_connector_inbox WHERE connector_id=%s AND seq<=%s "
                  "ORDER BY seq LIMIT 1000", (cid, passed))
        batch = [parse(row) for row in c.fetchall()]
        if not batch:
            break
        # Upgraded journals predate the compact replay watermarks. Seed those
        # before deleting any old content, without retaining an ID per message.
        for entry in batch:
            chat = str(entry["payload"].get("platform_chat_id", ""))
            mid = entry["event_key"].rsplit(":", 1)[-1]
            if chat and mid.isdecimal():
                c.execute("INSERT INTO watch_connector_heads (connector_id,chat_id,message_id) "
                          "VALUES (%s,%s,%s) ON CONFLICT(connector_id,chat_id) DO UPDATE "
                          "SET message_id=GREATEST(watch_connector_heads.message_id,"
                          "excluded.message_id)", (cid, chat, int(mid)))
        c.execute("DELETE FROM watch_connector_inbox WHERE connector_id=%s AND seq<=%s",
                  (cid, batch[-1]["seq"]))
    c.execute("UPDATE watch_connector_journals SET floor_seq=%s WHERE connector_id=%s",
              (max(int(state["floor_seq"]), passed), cid))


def append(cid: str, key: str, payload: dict[str, Any]) -> None:
    if len(encode(payload).encode()) > 16000:
        payload = dict(payload, text=str(payload.get("text", ""))[:2000])
    if len(encode(payload).encode()) > 16000:
        raise ValueError("Telegram journal event exceeds 16000 bytes")
    with get_db() as conn:
        c = conn.cursor()
        state = lock(c, cid)
        if state["fence"]:
            raise ValueError("Telegram receiver is fenced for transfer")
        # Telegram message IDs increase per chat. Persist only a high-water
        # mark, not message content or an unbounded set of compacted keys.
        chat = str(payload.get("platform_chat_id", ""))
        message = key.rsplit(":", 1)[-1]
        mid = int(message) if message.isdecimal() else None
        if chat and mid is not None:
            c.execute("SELECT message_id FROM watch_connector_heads "
                      "WHERE connector_id=%s AND chat_id=%s", (cid, chat))
            head = c.fetchone()
            if head and mid <= head["message_id"]:
                return
            c.execute("INSERT INTO watch_connector_heads (connector_id,chat_id,message_id) "
                      "VALUES (%s,%s,%s) ON CONFLICT(connector_id,chat_id) DO UPDATE "
                      "SET message_id=excluded.message_id", (cid, chat, mid))
        c.execute("INSERT INTO watch_connector_inbox (connector_id,event_key,payload) "
                  "VALUES (%s,%s,%s) ON CONFLICT(connector_id,event_key) DO NOTHING",
                  (cid, key, encode(payload)))
        c.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM watch_connector_inbox "
                  "WHERE connector_id=%s", (cid,))
        high = max(int(state["high_seq"]), int(c.fetchone()["seq"]))
        c.execute("UPDATE watch_connector_journals SET high_seq=%s WHERE connector_id=%s",
                  (high, cid))
        trim(c, cid)


def page(
    cid: str, channel: str, cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    from app.db.watches import parse

    with get_db() as conn:
        c = conn.cursor()
        state = lock(c, cid)
        if not cursor:
            return [], {"seq": state["high_seq"]}
        if cursor.get("expired") or int(cursor["seq"]) < state["floor_seq"]:
            raise ValueError(EXPIRED)
        if int(cursor["seq"]) > state["high_seq"]:
            raise ValueError("Telegram cursor does not belong to this journal")
        c.execute("SELECT * FROM watch_connector_inbox WHERE connector_id=%s AND seq>%s "
                  "ORDER BY seq LIMIT 100", (cid, cursor["seq"]))
        rows = [parse(row) for row in c.fetchall()]
    return [(row["event_key"], row["payload"]) for row in rows
            if str(row["payload"].get("platform_chat_id")) == channel], {
                "seq": rows[-1]["seq"] if rows else cursor["seq"],
            }


def receiver_allowed(cid: str) -> bool:
    with get_db() as conn:
        return not lock(conn.cursor(), cid)["fence"]


def tail(c: Any, watch: dict[str, Any]) -> dict[str, Any]:
    from app.db.watches import parse

    cid = watch["source"]["connector_id"]
    state = lock(c, cid)
    cursor = watch["cursor"]
    if cursor.get("expired") or int(cursor["seq"]) < state["floor_seq"]:
        raise ValueError(EXPIRED)
    c.execute("SELECT event_key,payload FROM watch_connector_inbox "
              "WHERE connector_id=%s AND seq>%s ORDER BY seq", (cid, cursor["seq"]))
    rows = [parse(row) for row in c.fetchall()]
    channel = str(watch["source"]["channel_id"])
    c.execute("SELECT chat_id,message_id FROM watch_connector_heads "
              "WHERE connector_id=%s AND chat_id=%s", (cid, channel))
    return {"entries": [row for row in rows
                        if str(row["payload"].get("platform_chat_id")) == channel],
            "heads": [dict(row) for row in c.fetchall()]}


def restore(c: Any, watch: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    """Rebase the unread tail to destination sequences in the import transaction."""
    cid = watch["source"]["connector_id"]
    state = lock(c, cid)
    baseline = int(state["high_seq"])
    if len(journal["entries"]) > MAX_ROWS:
        raise ValueError("Telegram transfer exceeds journal retention limit")
    c.execute("UPDATE connectors SET is_active=TRUE WHERE id=%s", (cid,))
    for entry in journal["entries"]:
        if (str(entry["payload"].get("platform_chat_id")) != str(watch["source"]["channel_id"])
                or len(encode(entry["payload"]).encode()) > 16000):
            raise ValueError("Telegram transfer exceeds its approved chat or payload scope")
        c.execute("INSERT INTO watch_connector_inbox (connector_id,event_key,payload) "
                  "VALUES (%s,%s,%s) ON CONFLICT(connector_id,event_key) DO NOTHING",
                  (cid, entry["event_key"], encode(entry["payload"])))
    c.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM watch_connector_inbox "
              "WHERE connector_id=%s", (cid,))
    high = max(baseline, int(c.fetchone()["seq"]))
    c.execute("UPDATE watch_connector_journals SET high_seq=%s,fence='' WHERE connector_id=%s",
              (high, cid))
    for head in journal["heads"]:
        c.execute("INSERT INTO watch_connector_heads (connector_id,chat_id,message_id) "
                  "VALUES (%s,%s,%s) ON CONFLICT(connector_id,chat_id) DO UPDATE "
                  "SET message_id=GREATEST(watch_connector_heads.message_id,excluded.message_id)",
                  (cid, head["chat_id"], head["message_id"]))
    return {"seq": baseline}


def purge(c: Any, cid: str) -> None:
    lock(c, cid)
    for row in consumers(c, cid):
        c.execute("UPDATE watches SET status='paused',reason=%s,cursor=%s,revision=revision+1 "
                  "WHERE id=%s", ("Telegram connector deleted; create a new watch",
                                  encode(dict(row["cursor"], expired=True)), row["id"]))
    for table in ("watch_connector_inbox", "watch_connector_heads"):
        c.execute("DELETE FROM " + table + " WHERE connector_id=%s", (cid,))
    # A content-free tombstone also rejects an ingress callback racing deletion.
    c.execute("UPDATE watch_connector_journals SET fence='deleted',floor_seq=high_seq "
              "WHERE connector_id=%s", (cid,))


def sweep() -> None:
    """Compact old installations and idle connectors even without new ingress."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT connector_id FROM watch_connector_inbox")
        ids = [row["connector_id"] for row in c.fetchall()]
        for cid in sorted(ids):
            trim(c, cid)
