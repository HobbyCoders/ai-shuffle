"""Explicit stopped-receiver preconditions for Telegram watch hand-over.

No credentials move. Both sides must provision the same bot independently;
source and destination are disabled/stopped before preparing hand-over.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.core.connectors.manager import connector_manager
from app.db import database
from app.db import watch_journal as journal
from app.db import watches as store
from app.db.async_database import run_sync


def connector(watch: dict[str, Any]) -> dict[str, Any] | None:
    if watch["source"]["kind"] != "connector":
        return None
    row = database.get_connector(watch["source"]["connector_id"])
    return row if row and row["type"] == "telegram" else None


def identity(row: dict[str, Any]) -> str:
    from app.core.connector_service import decrypt_config

    token = decrypt_config(row.get("config") or {}, "telegram").get("bot_token")
    if not token:
        raise ValueError("Provision the Telegram bot credential locally before moving a watch")
    return hashlib.sha256(str(token).encode()).hexdigest()


def gate(watch: dict[str, Any], value: str, *, exporting: bool) -> str:
    row = connector(watch)
    if row is None:
        return ""
    if row.get("status") != "disconnected" or row.get("is_active"):
        raise ValueError("Stop and disable both Telegram connections before moving the watch")
    cid = row["id"]
    from app.core.connector_service import decrypt_config

    config = decrypt_config(row.get("config") or {}, "telegram")
    if str(watch["source"]["channel_id"]) not in {
        str(chat) for chat in config.get("allowed_chat_ids", [])
    }:
        raise ValueError("The destination Telegram allowlist must include the watched chat")
    result = identity(row)
    with journal.get_db() as conn:
        c = conn.cursor()
        state = journal.lock(c, cid)
        others = [w for w in journal.consumers(c, cid) if w["id"] != watch["id"]]
        if others:
            raise ValueError(
                "Move requires an exclusive Telegram connector; stop its other watches"
            )
        if state["fence"] and state["fence"] != value:
            c.execute("SELECT * FROM watch_transfers WHERE watch_id=%s", (watch["id"],))
            previous = c.fetchone()
            returning = (not exporting and previous and previous["completed"]
                         and value.endswith(":" + previous["target"]))
            if not returning:
                raise ValueError("Telegram receiver is fenced by another transfer")
        c.execute("UPDATE watch_connector_journals SET fence=%s WHERE connector_id=%s",
                  (value, cid))
        if not exporting:
            journal.trim(c, cid)
    return result


async def ensure_started(watch: dict[str, Any]) -> None:
    row = await run_sync(connector, watch)
    if (row is not None
            and await connector_manager.get_running_connector(row["id"]) is None
            and not await connector_manager.start_connector(row["id"])):
        raise ValueError("Watch imported safely but Telegram receiver could not start; "
                         "retry the same move or restart the destination connection")


async def prepare(
    watch: dict[str, Any], peer: str, *, exporting: bool,
) -> dict[str, Any]:
    row = await run_sync(connector, watch)
    if row is None:
        return {}
    cid = row["id"]
    if not exporting:
        existing = await run_sync(store.get, watch["id"])
        receipt = await run_sync(store.import_receipt, watch["id"])
        if (existing and not existing["worker_id"].startswith("transfer:")
                and receipt.get("source") == peer):
            # The first import can already be receiving when its reply is lost.
            # Keep its cursor/gate unchanged; import still verifies the token.
            return {"identity": await run_sync(identity, row)}
    async with connector_manager.receiver_lock(cid):
        if await connector_manager.get_running_connector(cid) is not None:
            raise ValueError("Stop the Telegram receiver before moving its watch")
        value = ("export:" if exporting else "import:") + watch["id"] + ":" + peer
        return {"identity": await run_sync(gate, watch, value, exporting=exporting)}


def validate_import(
    snapshot: dict[str, Any], row: dict[str, Any], source: str,
) -> None:
    telegram = connector(row)
    if telegram is None:
        if "journal" in snapshot:
            raise ValueError("Journal transfer requires a Telegram connector")
        return
    if "journal" not in snapshot or snapshot["journal"]["identity"] != identity(telegram):
        raise ValueError("Telegram transfer requires the same bot credential on both workers")
    with journal.get_db() as conn:
        c = conn.cursor()
        state = journal.lock(c, telegram["id"])
        expected = "import:" + snapshot["watch"]["id"] + ":" + source
        if state["fence"] != expected:
            # Import acknowledgement loss is safe only with the same receipt.
            existing = store.get(snapshot["watch"]["id"])
            receipt = store.import_receipt(snapshot["watch"]["id"])
            if not existing or receipt.get("token") != snapshot["token"]:
                raise ValueError("Prepare the stopped destination receiver before importing")
