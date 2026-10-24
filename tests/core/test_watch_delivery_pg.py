"""Producing-path two-worker delivery against isolated disposable PostgreSQL schemas."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder

from app.core import (
    assistant_service,
    turn_manager,
    watch_delivery,
    watch_manager,
    watches,
)
from app.core import watch_placement as placement
from app.core.config import settings
from app.db import base, database, notifications, turns
from app.db import watch_delivery as delivery
from app.db import watches as store
from app.db.schema_postgres import create_postgres_schema
from app.network import control, runners
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("cross-worker delivery requires PostgreSQL 16")
    with private_postgres(bindir, tmp_path_factory.mktemp("watch-delivery-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def peers(
    cluster: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    names = {"dev_desktop": "DesktopWorker", "dev_server": "ServerWorker"}
    schemas = {device: "delivery_" + uuid4().hex for device in names}
    current: ContextVar[str] = ContextVar("delivery_worker", default="dev_desktop")

    def connect() -> Any:
        conn = pg_connect(cluster)
        conn.autocommit = False
        conn.cursor().execute("SET search_path TO " + schemas[current.get()] + ",public")
        return conn

    for schema in schemas.values():
        with closing(pg_connect(cluster)) as conn:
            c = conn.cursor()
            c.execute("CREATE SCHEMA " + schema)
            c.execute("SET search_path TO " + schema + ",public")
            conn.autocommit = False
            create_postgres_schema(c)
            conn.commit()
    monkeypatch.setattr(base, "get_connection", connect)
    monkeypatch.setattr(database, "get_connection", connect)
    monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())
    monkeypatch.setattr(assistant_service, "assistant_id", lambda: names[current.get()])
    monkeypatch.setattr(placement, "assistant_id", lambda: names[current.get()])
    monkeypatch.setattr(runners, "self_device_id", current.get)

    @contextmanager
    def on(device: str) -> Iterator[None]:
        token = current.set(device)
        previous = settings.instance_id
        monkeypatch.setattr(settings, "instance_id", names[device])
        try:
            yield
        finally:
            current.reset(token)
            monkeypatch.setattr(settings, "instance_id", previous)

    def directory() -> dict[str, Any]:
        return {
            "runners": [
                {
                    "id": device,
                    "legacy_instance_id": name,
                    "is_self": device == current.get(),
                    "online": True,
                    "capabilities": {},
                    "platform": "linux",
                    "device_name": name,
                }
                for device, name in names.items()
            ]
        }

    faults: dict[str, bool] = {}

    async def rpc(device: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        sender = current.get()
        if device not in names:
            raise PermissionError("Unknown authenticated peer")
        assert action == "watches" and control.known_action(action)
        if faults.get("offline") and device == "dev_desktop":
            raise control.RunnerUnreachable("offline fake peer")
        with on(device):
            answer = await control.handle(action, jsonable_encoder(body), from_device=sender)
        if faults.pop("lose_" + body["operation"], False):
            raise control.MaybeApplied("lost acknowledgement after commit")
        return json.loads(json.dumps(jsonable_encoder(answer)))

    monkeypatch.setattr(runners, "directory", directory)
    monkeypatch.setattr(control, "call", rpc)
    monkeypatch.setattr(watch_manager, "summarize", AsyncMock(return_value=("A reminder.", 0.002)))
    from app.services import push_notification

    monkeypatch.setattr(push_notification, "fire_and_forget_notify", lambda *args: None)
    turn_manager.register_kick(None)
    for device in names:
        with on(device):
            database.create_profile("watch-agent", "Watch agent", "custom", {})
    with on("dev_desktop"):
        session = database.create_session(
            uuid4().hex,
            "watch-agent",
            mode="chat",
            is_active=True,
            target_instance_id="DesktopWorker",
        )
    yield {"on": on, "session": session, "faults": faults, "rpc": rpc}
    turn_manager.register_kick(None)


async def create_watch(peers: dict[str, Any]) -> str:
    row = watches.prepare(
        peers["session"]["id"],
        {
            "description": "a scheduled reminder",
            "worker_id": "dev_server",
            "source": {"kind": "schedule", "interval_seconds": 3600},
            "daily_budget_usd": 5,
            "permissions": {"tools": ["WebSearch"]},
        },
    )
    result = await placement.call("dev_server", "create", row=row, watch_id=uuid4().hex)
    return result["watch"]["id"]


@pytest.mark.asyncio
async def test_active_creator_ack_loss_restart_and_transfer(peers: dict[str, Any]) -> None:
    wid = await create_watch(peers)
    peers["faults"]["lose_deliver"] = True
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        event = store.events(wid)[0]
        assert event["status"] == "admitted"
        assert store.get(wid)["failures"] == 0
    with peers["on"]("dev_desktop"):
        turn = turns.get_turn_by_command(peers["session"]["id"], event["id"])
        assert turn and turn["source"] == "watch"
        assert store.get(wid) is None  # Receipts cannot poll or execute watches.
        meta = turn["payload"]["_assistant_proactive"]
        assert meta["read_only"] and watches.tool_surface(meta) == ["WebSearch"]
        await asyncio.to_thread(watches.assert_allowed, meta, peers["session"]["id"])
        forged = dict(meta, permissions={"tools": ["Bash"], "read_only": True})
        with pytest.raises(ValueError, match="provenance"):
            await asyncio.to_thread(watches.assert_allowed, forged, peers["session"]["id"])
        from app.core.session_modes import proactive_tool_denial

        assert "read-only" in proactive_tool_denial(meta, peers["session"]["id"], "Bash")
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        assert store.events(wid)[0]["turn_id"] == turn["id"]
        store.set_status(wid, "admin", "paused")
    with peers["on"]("dev_desktop"):
        with pytest.raises(ValueError, match="paused"):
            await asyncio.to_thread(watches.assert_allowed, meta, peers["session"]["id"])
        with base.get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        assert store.events(wid, ("delivered",))[0]["status"] == "delivered"
        store.set_status(wid, "admin", "active")
        snapshot = store.export_watch(wid, "dev_desktop")
        assert snapshot["creating_device_id"] == "dev_desktop"
    await placement.call("dev_desktop", "import", snapshot=snapshot, source="dev_server")
    await watch_delivery.handle(
        {"operation": "deliver", "watch_id": wid, "event_id": event["id"]}, "dev_server"
    )
    await watch_manager.WatchManager().run_once()
    with base.get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT count(*) AS n FROM turns WHERE command_id=%s", (event["id"],))
        assert c.fetchone()["n"] == 1
    for device in ("dev_desktop", "dev_server"):
        with peers["on"](device):
            rows = notifications.list_notifications("admin")
            assert sum(row["id"] == "watch-" + event["id"] for row in rows) == 1


@pytest.mark.asyncio
async def test_offline_settled_creator_and_notification_ack_loss(peers: dict[str, Any]) -> None:
    wid = await create_watch(peers)
    peers["faults"]["offline"] = True
    for _ in range(4):
        with peers["on"]("dev_server"):
            await watch_manager.WatchManager().run_once()
    with peers["on"]("dev_server"):
        event = store.events(wid)[0]
        assert store.get(wid)["status"] == "active"
        assert event["status"] == "admitted"
        assert event["notification_sent"]
        with base.get_db() as conn:
            conn.cursor().execute(
                "UPDATE watch_events SET deadline_at=NOW()-INTERVAL '1 second' WHERE id=%s",
                (event["id"],),
            )
        await watch_manager.WatchManager().run_once()
        renewed = store.events(wid)[0]
        assert store.at(renewed["deadline_at"]) > store.now()
        assert renewed["id"] == event["id"]
    database.set_session_active(peers["session"]["id"], False)
    peers["faults"]["offline"] = False
    peers["faults"]["lose_report"] = True
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        assert delivery.pending()
        assert store.events(wid, ("delivered",))[0]["status"] == "delivered"
        snapshot = store.export_watch(wid, "dev_desktop")
        assert any(not row["acknowledged"] for row in snapshot["reports"])
    await placement.call("dev_desktop", "import", snapshot=snapshot, source="dev_server")
    await watch_manager.WatchManager().run_once()
    assert not delivery.pending()
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        assert not delivery.pending()
    assert not database.get_session(peers["session"]["id"])["is_active"]
    assert turns.get_turn_by_command(peers["session"]["id"], event["id"]) is None
    notice = notifications.get_notification("admin", "watch-" + event["id"])
    assert store.encode(event["payload"])[:300] in notice["message"]
    assert notice["session_id"] == peers["session"]["id"]


@pytest.mark.asyncio
async def test_forged_identity_and_both_budgets_refuse(
    peers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = await create_watch(peers)
    with peers["on"]("dev_server"):
        watch = store.get(wid)
        store.checkpoint(watch, [("test", {"text": "untrusted"})], {"next": "done"})
        event = store.events(wid)[0]
        assert store.reserve(wid, event["id"], 1)
    with pytest.raises(PermissionError):
        await watch_delivery.handle(
            {"operation": "deliver", "watch_id": wid, "event_id": event["id"]}, "dev_wrong"
        )
    from app.core import assistant_limits

    for blocked in ("ServerWorker", "DesktopWorker"):

        def status(worker: str = blocked) -> dict[str, bool]:
            return {"limit_reached": assistant_service.assistant_id() == worker}

        monkeypatch.setattr(assistant_limits, "status", status)
        with pytest.raises(ValueError, match="budget"):
            await watch_delivery.handle(
                {"operation": "deliver", "watch_id": wid, "event_id": event["id"]}, "dev_server"
            )
    assert delivery.receipt(event["id"]) is None
