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
    monkeypatch.setattr(watch_delivery, "_retry_after", {})
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
@pytest.mark.parametrize(
    ("stale_mirror", "offline"),
    [(False, False), (True, False), (False, True)],
    ids=["missing-mirror", "stale-settled-mirror", "unreachable-owner"],
)
async def test_remote_creator_state_is_not_inferred_from_local_mirror(
    peers: dict[str, Any],
    stale_mirror: bool,
    offline: bool,
) -> None:
    sid = peers["session"]["id"]
    wid = await create_watch(peers)
    peers["faults"]["offline"] = offline
    with peers["on"]("dev_server"):
        if stale_mirror:
            database.create_session(
                sid,
                "watch-agent",
                mode="chat",
                is_active=False,
                target_instance_id="DesktopWorker",
            )
        else:
            assert database.get_session(sid) is None
        await watch_manager.WatchManager().run_once()
        event = store.events(wid)[0]  # Real schedule polling/admission still runs.
        assert notifications.get_notification("admin", "watch-survives-" + wid) is None
        assert not store.get(wid)["creator_notified"]
    assert database.get_session(sid)["is_active"]
    if not offline:
        turn = turns.get_turn_by_command(sid, event["id"])
        assert turn and turn["source"] == "watch"


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
    watch_delivery._retry_after.clear()  # A restart discards in-memory backoff.
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
        watch_delivery._retry_after.clear()
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


@pytest.mark.asyncio
async def test_revoked_expired_ack_loss_cannot_pin_transfer(peers: dict[str, Any]) -> None:
    wid = await create_watch(peers)
    peers["faults"]["lose_deliver"] = True
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        event = store.events(wid)[0]
        assert not event["turn_id"]
        store.set_status(wid, "admin", "paused")
        with base.get_db() as conn:
            conn.cursor().execute(
                "UPDATE watch_events SET deadline_at=NOW()-INTERVAL '1 second' WHERE id=%s",
                (event["id"],),
            )
        await watch_manager.WatchManager().run_once()
        delivered = store.events(wid, ("delivered",))[0]
        assert delivered["cost_usd"] == delivered["reserved_usd"]
        snapshot = store.export_watch(wid, "dev_desktop")
        assert snapshot["events"][0]["id"] == event["id"]
    turn = turns.get_turn_by_command(peers["session"]["id"], event["id"])
    with pytest.raises(PermissionError, match="Not the creating worker"):
        await asyncio.to_thread(
            watches.assert_allowed, turn["payload"]["_assistant_proactive"], peers["session"]["id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_instance", [False, True], ids=["instance", "device-owned"])
async def test_remote_watch_booked_usage_counts_on_both_workers(
    peers: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    legacy_instance: bool,
) -> None:
    from app.core import assistant_limits
    from app.core.query_persistence import usage_settlement_key
    from app.db import analytics
    from app.db import assistant_limits as limits_db

    sid = peers["session"]["id"]
    if legacy_instance:
        from types import SimpleNamespace

        monkeypatch.setattr(
            assistant_service,
            "_network",
            lambda: SimpleNamespace(device_id=runners.self_device_id()),
        )
        with base.get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET instance_id=%s,owner_device_id=%s WHERE id=%s",
                ("LegacyWorker", "dev_desktop", sid),
            )
        assert watches.owner_session(sid)["instance_id"] == "LegacyWorker"
    project = database.create_project(uuid4().hex, "Ordinary project", None, str(tmp_path))
    database.set_session_project(sid, project["id"])
    limits_db.update("DesktopWorker", {"daily_spend_limit_usd": 2})
    wid = await create_watch(peers)
    with peers["on"]("dev_server"):
        limits_db.update("ServerWorker", {"daily_spend_limit_usd": 2})
        await watch_manager.WatchManager().run_once()
        event = store.events(wid)[0]
    turn = turns.get_turn_by_command(sid, event["id"])
    turns.start_turn(turn["id"])
    turns.settle_turn(turn["id"], "completed")
    ordinary = turns.enqueue_turn(sid, "user", prompt="Owner coding", profile_id="watch-agent")
    turns.start_turn(ordinary["id"])
    turns.settle_turn(ordinary["id"], "completed")
    for booked, cost in ((turn, 2.25), (ordinary, 9.0)):
        key = usage_settlement_key(sid, booked)
        assert analytics.settle_turn_usage(sid, key, model="test", cost_usd=cost)
        assert not analytics.settle_turn_usage(sid, key, model="test", cost_usd=cost)
    state = assistant_limits.status()
    assert state["spent_today_usd"] == 2.25  # Never the owner's unrelated $9 turn.
    assert state["limit_reached"]
    with pytest.raises(ValueError, match="budget"):
        await asyncio.to_thread(
            watches.assert_allowed, turn["payload"]["_assistant_proactive"], sid
        )
    if legacy_instance:
        # Device ownership wins in both directions: a matching instance cannot
        # charge a chat which has since moved to another device.
        with base.get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET instance_id=%s,owner_device_id=%s WHERE id=%s",
                ("DesktopWorker", "dev_server", sid),
            )
        assert not assistant_service._owned_here(database.get_session(sid))
        assert assistant_limits.status()["spent_today_usd"] == 0
        with base.get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET instance_id=%s,owner_device_id=%s WHERE id=%s",
                ("LegacyWorker", "dev_desktop", sid),
            )
    with peers["on"]("dev_server"):
        await watch_manager.WatchManager().run_once()
        await watch_manager.WatchManager().run_once()
        assert store.events(wid, ("delivered",))[0]["cost_usd"] == 2.25
        assert assistant_limits.status()["spent_today_usd"] == 2.25
        assert assistant_limits.status()["limit_reached"]
        snapshot = store.export_watch(wid, "dev_desktop")
    await placement.call("dev_desktop", "import", snapshot=snapshot, source="dev_server")
    assert assistant_limits.status()["spent_today_usd"] == 2.25  # No transfer double-booking.


@pytest.mark.asyncio
async def test_outbox_bounds_backlog_and_backs_off_only_failed_target(
    peers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for i in range(40):
        delivery.enqueue(str(i), ["dev_absent"], {"message": "report", "title": "watch"})
    for i in range(2):
        delivery.enqueue(str(i), ["dev_healthy"], {"message": "report", "title": "watch"})

    async def call(worker: str, operation: str, **body: Any) -> dict[str, Any]:
        calls.append(worker)
        if worker == "dev_absent":
            await asyncio.Event().wait()
        return {"acknowledged": True}

    monkeypatch.setattr(placement, "call", call)
    monkeypatch.setattr(watch_delivery, "REPORT_TIMEOUT_SECONDS", 0.02)
    await asyncio.wait_for(watch_delivery.flush(), 1)
    assert calls == ["dev_absent", "dev_healthy"]
    await asyncio.wait_for(watch_delivery.flush(), 1)
    assert calls == ["dev_absent", "dev_healthy", "dev_healthy"]
    assert len(delivery.pending()) == 40
    assert not delivery.batch(16, ["dev_absent"])


@pytest.mark.asyncio
async def test_outbox_rotates_more_than_one_batch_of_continuously_backlogged_peers(
    peers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [f"dev_peer_{i:02d}" for i in range(20)]
    calls: list[str] = []

    async def call(worker: str, operation: str, **body: Any) -> dict[str, Any]:
        calls.append(worker)
        return {"acknowledged": True}

    monkeypatch.setattr(placement, "call", call)
    for pass_number in range(3):
        # Every target has new pending work before every pass; successful early
        # peers cannot starve the tail by immediately becoming eligible again.
        delivery.enqueue(f"report-{pass_number}", targets, {"title": "Watch", "message": "matched"})
        await watch_delivery.flush()
        # Simulate losing dispatcher memory: fairness must survive restarts.
        monkeypatch.setattr(watch_delivery, "_retry_after", {})
    assert len(calls) == 3 * watch_delivery.REPORTS_PER_PASS
    assert calls[:16] == targets[:16]
    assert calls[16:20] == targets[16:]
    assert set(calls[:32]) == set(targets)
    assert all(calls.count(target) >= 2 for target in targets)


@pytest.mark.asyncio
async def test_outbox_peer_io_does_not_hold_reconciliation_lock(
    peers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def flush() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(watch_delivery, "flush", flush)
    manager = watch_manager.WatchManager()
    first = asyncio.create_task(manager.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not manager._lock.locked()
        await asyncio.wait_for(manager.run_once(), 1)
        assert calls == 2
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_overlapping_delivery_waits_for_original_admission_commit(
    peers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import queue
    import threading
    import time

    from app.core import watch_sources

    wid = await create_watch(peers)
    with peers["on"]("dev_server"):
        watch = store.get(wid)
        observed, cursor = await watch_sources.poll(watch)
        assert store.checkpoint(watch, observed, cursor)
        event = store.events(wid)[0]
        assert store.reserve(wid, event["id"], 1)
    original = turns.enqueue_turn
    inserted, release = threading.Event(), threading.Event()
    second_pid: queue.Queue[int] = queue.Queue()
    lock = threading.Lock()
    calls = 0

    def enqueue(session_id: str, source: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        with lock:
            calls += 1
            number = calls
        with base.get_db() as conn:
            if number == 2:
                second_pid.put(conn.get_backend_pid())
            result = original(session_id, source, _conn=conn, **kwargs)
            if number == 1:
                inserted.set()  # Row exists, but its admission transaction is still uncommitted.
                assert release.wait(5), "test did not release the first admission"
            return result

    def wait_for_lock(pid: int) -> None:
        deadline = time.monotonic() + 5
        with base.get_db() as conn:
            while time.monotonic() < deadline:
                c = conn.cursor()
                c.execute("SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted", (pid,))
                if c.fetchone():
                    return
                time.sleep(0.01)
        raise AssertionError("retry did not wait for the original admission transaction")

    monkeypatch.setattr(turns, "enqueue_turn", enqueue)
    body = {"operation": "deliver", "watch_id": wid, "event_id": event["id"]}
    first = asyncio.create_task(watch_delivery.handle(body, "dev_server"))
    second = None
    try:
        assert await asyncio.to_thread(inserted.wait, 5)
        assert delivery.receipt(event["id"]) is not None
        assert turns.get_turn_by_command(peers["session"]["id"], event["id"]) is None
        second = asyncio.create_task(watch_delivery.handle(body, "dev_server"))
        pid = await asyncio.to_thread(second_pid.get, True, 5)
        await asyncio.to_thread(wait_for_lock, pid)
        assert not second.done()
    finally:
        release.set()
        answers = await asyncio.gather(first, *([second] if second is not None else []))
    assert len(answers) == 2
    assert answers[0]["turn_id"] == answers[1]["turn_id"]
    with base.get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT count(*) AS n FROM turns WHERE command_id=%s", (event["id"],))
        assert c.fetchone()["n"] == 1
