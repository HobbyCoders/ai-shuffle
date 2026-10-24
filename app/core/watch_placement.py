"""Owner watch placement and authenticated peer controls, never credential transfer.

A transfer freezes the old executor before exporting its cursor/outbox. Ambiguous
network failures remain fenced and retryable, never fail over to two executors.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.assistant_service import assistant_id
from app.db import database
from app.db import watches as store
from app.db.async_database import run_sync
from app.network import control, runners

PREFERENCE = "watch_preferred_worker"


def resolve(requested: str | None = None) -> str:
    listing = runners.directory()
    rows = listing["runners"]
    preferred = requested or database.get_system_setting(PREFERENCE)
    if preferred:
        if preferred == assistant_id():
            return preferred
        for row in rows:
            if preferred in (row["id"], row.get("legacy_instance_id")):
                if not row["online"]:
                    raise ValueError("Preferred watch worker is unavailable; choose another worker")
                return assistant_id() if row["is_self"] else row["id"]
        raise ValueError("Preferred watch worker is not an active network member")
    candidates = [row for row in rows if row["online"] and (
        row["capabilities"].get("always_on") or (
            str(row.get("platform", "")).lower() == "linux"
            and not row["capabilities"].get("desktop", False)
        )
    )]
    if candidates:
        chosen = sorted(candidates, key=lambda row: (
            not row["capabilities"].get("always_on", False), row["id"]
        ))[0]
        return assistant_id() if chosen["is_self"] else chosen["id"]
    # Standalone Linux servers can run continuously. Desktop owners must choose
    # explicitly rather than silently creating a watch that sleeps with them.
    import sys
    if not rows and sys.platform == "linux":
        return assistant_id()
    raise ValueError("No always-on worker is available; explicitly choose worker_id")


def label(worker_id: str) -> str:
    for row in runners.directory()["runners"]:
        if worker_id in (row["id"], row.get("legacy_instance_id")) or (
            row["is_self"] and worker_id == assistant_id()
        ):
            return str(row["device_name"])
    return worker_id


def canonical(worker_id: str) -> str:
    """Transfer receipts/routing use device IDs, not installation execution IDs."""
    for row in runners.directory()["runners"]:
        if worker_id in (row["id"], row.get("legacy_instance_id")) or (
            row["is_self"] and worker_id == assistant_id()
        ):
            return str(row["id"])
    return worker_id


def local(worker_id: str) -> bool:
    return worker_id == assistant_id() or worker_id == runners.self_device_id()


async def call(worker_id: str, operation: str, **body: Any) -> dict[str, Any]:
    if local(worker_id):
        return await handle(dict(body, operation=operation), "local")
    return await control.call(worker_id, "watches", dict(body, operation=operation))


def publish() -> None:
    """Sync display metadata through ordinary settings, never executable watch rows."""
    from app.core.watches import panel_rows
    from app.network import engine

    ctx = engine.cached_context()
    if ctx is None:
        return
    value = store.encode([
        {key: row[key] for key in (
            "id", "description", "status", "reason", "last_fired_at", "cost_usd", "worker_id",
            "what", "can_pause", "can_stop", "daily_budget_usd",
        )} | {"placement_device_id": ctx.device_id, "kind": "watch"}
        for row in panel_rows()
    ])
    key = "watch_directory:" + ctx.device_id
    if database.get_system_setting(key) != value:
        database.set_system_setting(key, value)


def cached_rows() -> list[dict[str, Any]]:
    from app.db.base import get_db

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM system_settings WHERE key LIKE 'watch_directory:%%'")
        return [item for row in c.fetchall() for item in json.loads(row["value"])]


async def rows() -> list[dict[str, Any]]:
    from app.core.watches import panel_rows

    directory = await run_sync(runners.directory)
    members = {peer["id"] for peer in directory["runners"] if not peer["is_self"]}
    result = [row for row in await run_sync(cached_rows)
              if row["placement_device_id"] in members]
    device = await run_sync(canonical, assistant_id())
    result.extend(dict(row, placement_device_id=device) for row in await run_sync(panel_rows))
    for peer in directory["runners"]:
        if peer["is_self"] or not peer["online"]:
            continue
        try:
            answer = await call(peer["id"], "list")
        except control.RunnerUnreachable:
            continue
        result = [row for row in result if row.get("placement_device_id") != peer["id"]]
        result.extend(dict(row, placement_device_id=peer["id"]) for row in answer["watches"])
    # Source-side transfer tombstones are not executable or visible twice.
    return list({row["id"]: row for row in result}.values())


async def change(wid: str, status: str) -> dict[str, Any]:
    for row in await rows():
        if row["id"] == wid:
            target = row.get("placement_device_id") or assistant_id()
            return (await call(target, "status", watch_id=wid, status=status))["watch"]
    raise ValueError("Watch does not exist on an available worker")


async def move(wid: str, target: str) -> dict[str, Any]:
    target = await run_sync(canonical, await run_sync(resolve, target))
    for row in await rows():
        if row["id"] != wid:
            continue
        source = await run_sync(canonical, row.get("placement_device_id") or assistant_id())
        if source == target:
            # Import may have committed while its acknowledgement was lost.
            # Reconcile the original source before reporting an idempotent move.
            receipt = await call(target, "receipt", watch_id=wid)
            if receipt:
                await call(receipt["source"], "complete", watch_id=wid,
                           target=target, token=receipt["token"])
            return row
        # Read-only prerequisite checks must not strand either stopped receiver.
        # Only export commits the source fence, atomically with its snapshot.
        prepared = await call(source, "prepare_export", watch_id=wid, target=target)
        await call(target, "validate", row=row, moving=True, source=source, prepared=prepared)
        snapshot = await call(source, "export", watch_id=wid, target=target)
        # A timeout after import is intentionally not rolled back: retrying the
        # same id imports once; source remains permanently fenced meanwhile.
        answer = await call(target, "import", snapshot=snapshot, source=source)
        await call(source, "complete", watch_id=wid, target=target, token=snapshot["token"])
        return answer["watch"]
    raise ValueError("Watch does not exist on an available worker")


def execution_row(
    row: dict[str, Any], *, stopped_connector: bool = False,
) -> dict[str, Any]:
    from app.core import watches

    if not database.get_profile(row["agent_id"]):
        raise ValueError("Watch agent is not available on the destination")
    permissions = row.get("permissions") or {}
    if set(permissions) - {"tools", "read_only"} or permissions.get("read_only") is not True:
        raise ValueError("Remote watch permissions must retain the read-only ceiling")
    data = dict(row, worker_id=assistant_id(), permissions={"tools": permissions.get("tools", [])})
    return watches.prepare(
        row.get("creating_session_id"), data,
        _session={"profile_id": row["agent_id"]}, _worker=assistant_id(),
        _allow_stopped_connector=stopped_connector,
    )


async def handle(body: dict[str, Any], from_device: str) -> dict[str, Any]:
    """Called only by the authenticated owner-worker control endpoint."""
    from app.core import watch_journal_transfer as journal_transfer
    from app.core import watch_sources, watches

    operation = body.get("operation")
    if operation == "list":
        return {"watches": await run_sync(watches.panel_rows)}
    if operation == "status":
        return {"watch": await run_sync(
            store.set_status, body["watch_id"], "admin", body["status"]
        )}
    if operation == "validate":
        row = await run_sync(execution_row, body["row"], stopped_connector=bool(
            body.get("moving") or body.get("stopped_connector")
        ))
        if body.get("moving"):
            prepared = await journal_transfer.prepare(
                row | {"id": body["row"]["id"]}, body["source"], exporting=False, fence=False,
            )
            if prepared != body.get("prepared", {}):
                raise ValueError(
                    "Telegram transfer requires the same bot credential on both workers"
                )
        return {"row": row}
    if operation == "create":
        row = await run_sync(execution_row, body["row"])
        existing = await run_sync(store.get, body["watch_id"])
        if existing:
            return {"watch": existing}
        cursor: dict[str, Any] = {}
        if row["source"]["kind"] == "connector":
            _, cursor = await watch_sources.poll(dict(row, cursor={}))
        wid = await run_sync(store.create, **row, cursor=cursor, watch_id=body["watch_id"])
        return {"watch": await run_sync(store.get, wid)}
    if operation == "prepare_export":
        watch = await run_sync(store.get, body["watch_id"])
        return await journal_transfer.prepare(watch, body["target"], exporting=True, fence=False)
    if operation == "export":
        watch = await run_sync(store.get, body["watch_id"])
        return await journal_transfer.export(watch, body["target"])
    if operation == "import":
        snapshot = body["snapshot"]
        if not local(await run_sync(resolve, snapshot["target"])):
            raise ValueError("Watch transfer is addressed to another worker")
        row = await run_sync(execution_row, snapshot["watch"],
                             stopped_connector="journal" in snapshot)
        source = await run_sync(canonical, body.get("source") or from_device)
        await journal_transfer.prepare(
            row | {"id": snapshot["watch"]["id"]}, source, exporting=False,
        )
        await run_sync(journal_transfer.validate_import, snapshot, row, source)
        imported = await run_sync(store.import_watch, snapshot, row, source)
        # Import commits both the rebased cursor/tail and the open receive gate.
        # Starting afterwards cannot race ahead of the durable snapshot.
        if "journal" in snapshot:
            await journal_transfer.ensure_started(imported)
        return {"watch": imported}
    if operation == "receipt":
        receipt = await run_sync(store.import_receipt, body["watch_id"])
        if receipt:
            await journal_transfer.ensure_started(await run_sync(store.get, body["watch_id"]))
        return receipt
    if operation == "complete":
        await run_sync(store.complete_transfer, body["watch_id"], body["target"], body["token"])
        return {"ok": True}
    raise ValueError("Unknown watch control operation")
