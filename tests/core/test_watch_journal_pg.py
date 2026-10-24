"""Journal ordering against private PostgreSQL, never the configured app database."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from typing import Any
from uuid import uuid4

import pytest

from app.db import watch_journal as journal
from app.db import watches as store
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def journal_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("journal ordering requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("watch-journal-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn:
            store.create_schema(conn.cursor())
        yield descriptor


@pytest.mark.parametrize("operation", ["append", "page", "baseline"])
def test_journal_serializes_reversed_commit_attempts(
    journal_cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    # Writer one has allocated a BIGSERIAL but cannot commit yet. Writer two
    # attempts to overtake its commit; a reader must not checkpoint past it.
    connector = "journal-" + uuid4().hex
    inserted = threading.Event()
    release = threading.Event()
    state = threading.local()
    pids: queue.Queue[int] = queue.Queue()

    @contextmanager
    def database() -> Iterator[Any]:
        with closing(pg_connect(journal_cluster)) as conn:
            conn.autocommit = False
            conn.cursor().execute("SET statement_timeout TO 10000")
            if getattr(state, "waiter", False):
                pids.put(conn.get_backend_pid())
            try:
                yield conn
                if getattr(state, "hold", False):
                    inserted.set()
                    assert release.wait(10), "test did not release the first commit"
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    monkeypatch.setattr(store, "get_db", database)
    monkeypatch.setattr(journal, "get_db", database)
    store.create(owner="admin", creating_session_id=None, worker_id="source", agent_id="agent",
                 description="journal ordering", source={"kind": "connector",
                 "connector_id": connector, "channel_id": "123"}, filter={}, action="alert",
                 permissions={"read_only": True, "tools": []}, daily_budget_usd=1,
                 cursor={"seq": 0})

    def first_writer() -> None:
        state.hold = True
        store.record_connector_event(connector, "first", {"platform_chat_id": "123"})

    def waiter() -> Any:
        state.waiter = True
        if operation == "append":
            store.record_connector_event(connector, "second", {"platform_chat_id": "123"})
            return None
        return store.connector_page(connector, "123", {} if operation == "baseline" else {"seq": 0})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_writer)
        second = None
        try:
            assert inserted.wait(10)
            second = pool.submit(waiter)
            pid = pids.get(timeout=5)
            deadline = time.monotonic() + 5
            with closing(pg_connect(journal_cluster)) as observer:
                while True:
                    c = observer.cursor()
                    c.execute(
                        "SELECT 1 FROM pg_locks WHERE pid=%s "
                        "AND locktype='advisory' AND NOT granted", (pid,),
                    )
                    if c.fetchone():
                        break
                    assert time.monotonic() < deadline, "journal operation bypassed ingress lock"
                    time.sleep(0.01)
                # This is the attempted reversed commit: without the lock the
                # second insertion/read finishes while the first is invisible.
                assert not second.done()
        finally:
            release.set()
        first.result(timeout=15)
        assert second is not None
        answer = second.result(timeout=15)
    events, cursor = store.connector_page(connector, "123", {"seq": 0})
    expected = ["first", "second"] if operation == "append" else ["first"]
    assert [key for key, _ in events] == expected
    if operation == "page":
        assert answer == (events, cursor)
    if operation == "baseline":
        assert answer == ([], cursor)
    assert store.connector_page(connector, "123", cursor) == ([], cursor)


@pytest.fixture
def workers(
    journal_cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """Two process-local workers, isolated PG schemas and fake Telegram receivers."""
    from contextvars import ContextVar
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core import assistant_service, watch_sources
    from app.core import watch_placement as placement
    from app.core.automation_manager import automation_manager
    from app.core.connectors.manager import ConnectorManager, connector_manager
    from app.core.connectors.telegram import TelegramConnector
    from app.db import connectors, profiles
    from app.network import runners

    current: ContextVar[str] = ContextVar("journal_worker", default="source")
    schemas = {name: "worker_" + uuid4().hex for name in ("source", "target")}
    running: dict[str, Any] = {"source": None, "target": None}
    instances: dict[str, Any] = {}
    starts: list[str] = []
    managers = {name: ConnectorManager() for name in schemas}
    monkeypatch.setattr(connector_manager, "receiver_lock",
                        lambda cid: managers[current.get()].receiver_lock(cid))

    @contextmanager
    def db() -> Iterator[Any]:
        with closing(pg_connect(journal_cluster)) as conn:
            conn.autocommit = False
            conn.cursor().execute("SET search_path TO " + schemas[current.get()])
            conn.cursor().execute("SET statement_timeout TO 10000")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def on(name: str) -> Iterator[None]:
        token = current.set(name)
        try:
            yield
        finally:
            current.reset(token)

    for name, schema in schemas.items():
        with closing(pg_connect(journal_cluster)) as conn:
            conn.cursor().execute("CREATE SCHEMA " + schema)
        with on(name), db() as conn:
            c = conn.cursor()
            store.create_schema(c)
            c.execute("""CREATE TABLE connectors (
                id TEXT PRIMARY KEY, type TEXT, config JSONB, events JSONB,
                api_user_id TEXT, is_active BOOLEAN, status TEXT, runner_id TEXT
            )""")
            c.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, config JSONB, mcp_tools JSONB)")
            c.execute("INSERT INTO agents VALUES ('agent','{}','[]')")
            c.execute("""INSERT INTO connectors VALUES (
                'bot','telegram',%s,'[]',NULL,FALSE,'disconnected',%s
            )""", (store.encode({"bot_token": "fake-bot-secret", "allowed_chat_ids": ["123"]}),
                    name))
        instances[name] = TelegramConnector(
            "bot", "telegram", {"allowed_chat_ids": ["123"]}, AsyncMock(), AsyncMock(),
            on_trigger=connector_manager._handle_trigger,
        )

    for module in (store, journal, connectors, profiles, watch_sources):
        monkeypatch.setattr(module, "get_db", db)
    monkeypatch.setattr(assistant_service, "assistant_id", current.get)
    monkeypatch.setattr(placement, "assistant_id", current.get)
    monkeypatch.setattr(runners, "self_device_id", current.get)
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [
        {"id": name, "online": True, "is_self": name == current.get(),
         "capabilities": {}, "platform": "linux"} for name in schemas
    ]})
    monkeypatch.setattr(automation_manager, "handle_connector_trigger", AsyncMock())
    monkeypatch.setattr(connector_manager, "get_running_connector",
                        AsyncMock(side_effect=lambda _: running[current.get()]))

    async def start(cid: str) -> bool:
        assert cid == "bot" and journal.receiver_allowed(cid)
        # The old side cannot receive or checkpoint before this side starts.
        other = "target" if current.get() == "source" else "source"
        with on(other):
            assert not journal.receiver_allowed(cid)
        starts.append(current.get())
        running[current.get()] = instances[current.get()]
        return True

    monkeypatch.setattr(connector_manager, "start_connector", start)

    async def listing() -> list[dict[str, Any]]:
        result = []
        for name in schemas:
            with on(name):
                result.extend(dict(row, placement_device_id=name)
                              for row in store.list_watches() if row.get("reason") != "Transferred")
        return result

    async def rpc(worker: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        sender = current.get()
        assert action == "watches"
        with on(worker):
            return await placement.handle(body, sender)

    async def receive(number: int, text: str = "fake update") -> None:
        instance = instances[current.get()]
        await instance._handle_message(SimpleNamespace(
            chat=SimpleNamespace(id=123, type="group"),
            from_user=SimpleNamespace(id=7, full_name="Fake sender"),
            text=text, message_id=number,
        ))
        if instance._chat_tasks:
            import asyncio

            await asyncio.gather(*tuple(instance._chat_tasks))

    def create(**changes: Any) -> dict[str, Any]:
        row = {
            "owner": "admin", "creating_session_id": None, "worker_id": current.get(),
            "agent_id": "agent", "description": "Telegram updates", "source": {
                "kind": "connector", "connector_id": "bot", "channel_id": "123",
            }, "filter": {}, "action": "alert", "permissions": {"read_only": True, "tools": []},
            "daily_budget_usd": 1, "cursor": {"seq": 0},
        }
        row.update(changes)
        return store.get(store.create(**row))

    monkeypatch.setattr(placement, "rows", listing)
    monkeypatch.setattr(placement.control, "call", rpc)
    yield SimpleNamespace(on=on, db=db, receive=receive, create=create, starts=starts,
                          running=running, current=current)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [None, "export", "import", "complete"])
async def test_telegram_tail_and_cursor_transfer_with_lost_ack(
    workers: Any, monkeypatch: pytest.MonkeyPatch, lost: str | None,
) -> None:
    from app.core import watch_placement as placement
    from app.network.control import RunnerUnreachable

    watch = workers.create()
    await workers.receive(1, "processed")
    events, cursor = store.connector_page("bot", "123", watch["cursor"])
    assert store.checkpoint(watch, events, cursor)
    processed = store.events(watch["id"])[0]
    await workers.receive(2, "unread before move")
    await workers.receive(3, "unread tail")
    before = store.get(watch["id"])
    with workers.db() as conn:
        conn.cursor().execute("UPDATE connectors SET is_active=FALSE")
    with workers.on("target"):
        # An unrelated destination journal has a different numeric sequence.
        with workers.db() as conn:
            conn.cursor().execute("SELECT setval('watch_connector_inbox_seq_seq',500)")
        store.record_connector_event("bot", "message_received:456:1", {
            "platform_chat_id": "456", "text": "old unwatched content",
        })
        _, destination_cursor = store.connector_page("bot", "123", {})
    original = placement.call
    failed = False

    async def call(worker: str, operation: str, **body: Any) -> dict[str, Any]:
        nonlocal failed
        answer = await original(worker, operation, **body)
        if operation == lost and not failed:
            failed = True
            raise RunnerUnreachable("fake acknowledgement loss")
        return answer

    monkeypatch.setattr(placement, "call", call)
    if lost:
        with pytest.raises(RunnerUnreachable):
            await placement.move(watch["id"], "target")
        assert not journal.receiver_allowed("bot")
        assert not store.checkpoint(before, [("stale", {})], {"seq": 999})
    moved = await placement.move(watch["id"], "target")
    assert moved["worker_id"] == "target"
    assert not journal.receiver_allowed("bot")
    from app.core.connectors.manager import ConnectorManager
    from app.network import ownership

    monkeypatch.setattr(ownership, "decide_connector", lambda _: None)
    assert not await ConnectorManager().start_connector("bot")
    assert not store.checkpoint(before, [("late", {})], {"seq": 999})
    with pytest.raises(ValueError, match="fenced"):
        store.record_connector_event("bot", "message_received:123:4", {})
    with workers.on("target"):
        current = store.get(watch["id"])
        assert current["cursor"] == destination_cursor
        assert current["permissions"] == before["permissions"]
        assert current["source"] == before["source"]
        assert store.events(watch["id"])[0]["id"] == processed["id"]
        await workers.receive(3, "replay after offset acknowledgement loss")
        await workers.receive(4, "new owner update")
        events, cursor = store.connector_page("bot", "123", current["cursor"])
        assert [key for key, _ in events] == [
            "message_received:123:2", "message_received:123:3", "message_received:123:4",
        ]
        assert store.checkpoint(current, events, cursor)
        assert len(store.events(watch["id"])) == 4
        assert store.connector_page("bot", "123", cursor) == ([], cursor)
    assert workers.starts
    with workers.db() as conn:
        c = conn.cursor()
        c.execute("SELECT snapshot FROM watch_transfers WHERE watch_id=%s", (watch["id"],))
        assert c.fetchone()["snapshot"] == {}
        c.execute("SELECT COUNT(*) AS n FROM watch_connector_inbox")
        assert c.fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_retention_preserves_slow_paused_cursor_then_expires_explicitly(
    workers: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal, "MAX_ROWS", 3)
    fast = workers.create()
    slow = workers.create(status="paused")
    for number in range(1, 4):
        await workers.receive(number)
        current = store.get(fast["id"])
        events, cursor = store.connector_page("bot", "123", current["cursor"])
        assert store.checkpoint(current, events, cursor)
    assert len(store.connector_page("bot", "123", slow["cursor"])[0]) == 3
    assert store.get(slow["id"])["cursor"] == {"seq": 0}
    await workers.receive(4)
    expired = store.get(slow["id"])
    assert expired["status"] == "paused" and expired["reason"] == journal.EXPIRED
    assert expired["cursor"] == {"seq": 0, "expired": True}
    with pytest.raises(ValueError, match="opt in again"):
        store.set_status(slow["id"], "admin", "active")
    with pytest.raises(ValueError, match="opt in again"):
        store.connector_page("bot", "123", slow["cursor"])
    with workers.db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM watch_connector_inbox")
        assert c.fetchone()["n"] == 1
    current = store.get(fast["id"])
    events, cursor = store.connector_page("bot", "123", current["cursor"])
    assert store.checkpoint(current, events, cursor)
    await workers.receive(4, "replay must not resurrect compacted content")
    assert store.connector_page("bot", "123", cursor) == ([], cursor)


@pytest.mark.asyncio
async def test_unwatched_content_and_connector_deletion_are_cleaned(workers: Any) -> None:
    from app.db import connectors

    await workers.receive(1, "unwatched private content")
    _, baseline = store.connector_page("bot", "123", {})
    assert baseline["seq"] > 0
    watch = workers.create(cursor=baseline)
    await workers.receive(2, "watched private content")
    with workers.db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM watch_connector_inbox")
        assert c.fetchone()["n"] == 1
    assert connectors.delete_connector("bot")
    assert store.get(watch["id"])["cursor"]["expired"]
    with workers.db() as conn:
        c = conn.cursor()
        for table in ("watch_connector_inbox", "watch_connector_heads"):
            c.execute("SELECT COUNT(*) AS n FROM " + table)
            assert c.fetchone()["n"] == 0
    with pytest.raises(ValueError, match="fenced"):
        store.record_connector_event("bot", "message_received:123:3", {"text": "late"})


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "running", "live-source", "live-target", "credential", "allowlist", "grant", "other-watch",
])
async def test_telegram_transfer_preconditions_fail_closed(
    workers: Any, bad: str,
) -> None:
    from app.core import watch_placement as placement

    watch = workers.create()
    if bad == "running":
        with workers.db() as conn:
            conn.cursor().execute("UPDATE connectors SET is_active=TRUE")
    if bad == "other-watch":
        workers.create()
    if bad.startswith("live-"):
        workers.running[bad.removeprefix("live-")] = object()
    with workers.on("target"), workers.db() as conn:
        c = conn.cursor()
        if bad == "credential":
            c.execute("UPDATE connectors SET config=%s", (store.encode({
                "bot_token": "different-fake-bot", "allowed_chat_ids": ["123"],
            }),))
        if bad == "allowlist":
            c.execute("UPDATE connectors SET config=%s", (store.encode({
                "bot_token": "fake-bot-secret", "allowed_chat_ids": ["456"],
            }),))
        if bad == "grant":
            c.execute("UPDATE connectors SET runner_id='someone-else'")
    with pytest.raises((ValueError, PermissionError)):
        await placement.move(watch["id"], "target")
    assert not workers.starts
    assert store.get(watch["id"])["worker_id"] == "source"


@pytest.mark.asyncio
async def test_legacy_journal_compaction_keeps_replay_watermarks(
    workers: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal, "MAX_ROWS", 3)
    with workers.db() as conn:
        c = conn.cursor()
        # The previous release had only the inbox, without retention metadata.
        for number in range(1, 6):
            c.execute("INSERT INTO watch_connector_inbox (connector_id,event_key,payload) "
                      "VALUES ('bot',%s,%s)", ("message_received:123:" + str(number),
                      store.encode({"platform_chat_id": "123", "text": "legacy content"})))
    slow = workers.create()
    from app.db.async_database import run_maintenance

    await run_maintenance(journal.sweep)
    assert store.get(slow["id"])["cursor"]["expired"]
    _, baseline = store.connector_page("bot", "123", {})
    fresh = workers.create(cursor=baseline)
    await workers.receive(5, "compacted replay")
    assert store.connector_page("bot", "123", fresh["cursor"]) == ([], baseline)
    await workers.receive(6, "new message")
    assert len(store.connector_page("bot", "123", fresh["cursor"])[0]) == 1


@pytest.mark.asyncio
async def test_failed_import_rolls_back_journal_cursor_and_receiver_activation(
    workers: Any,
) -> None:
    from app.core import watch_placement as placement

    watch = workers.create()
    await workers.receive(1)
    prepared = await placement.call("source", "prepare_export", watch_id=watch["id"],
                                    target="target")
    await placement.call("target", "validate", row=watch, moving=True, source="source",
                         prepared=prepared)
    snapshot = await placement.call("source", "export", watch_id=watch["id"], target="target")
    bad = dict(snapshot, events=[{"status": "running"}])
    with pytest.raises(ValueError, match="Invalid transfer event"):
        await placement.call("target", "import", snapshot=bad, source="source")
    with workers.on("target"), workers.db() as conn:
        c = conn.cursor()
        assert store.get(watch["id"]) is None
        assert not journal.receiver_allowed("bot")
        c.execute("SELECT is_active FROM connectors WHERE id='bot'")
        assert not c.fetchone()["is_active"]
        c.execute("SELECT COUNT(*) AS n FROM watch_connector_inbox")
        assert c.fetchone()["n"] == 0
    assert not workers.starts
    result = await placement.call("target", "import", snapshot=snapshot, source="source")
    assert result["watch"]["id"] == watch["id"]


@pytest.mark.asyncio
async def test_receiver_start_failure_retries_committed_import_once(
    workers: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import watch_placement as placement
    from app.core.connectors.manager import connector_manager

    watch = workers.create()
    await workers.receive(1)
    start = connector_manager.start_connector
    failed = False

    async def fail_once(cid: str) -> bool:
        nonlocal failed
        if not failed:
            failed = True
            return False
        return await start(cid)

    monkeypatch.setattr(connector_manager, "start_connector", fail_once)
    with pytest.raises(ValueError, match="imported safely"):
        await placement.move(watch["id"], "target")
    assert not journal.receiver_allowed("bot")
    assert not workers.starts
    result = await placement.move(watch["id"], "target")
    with workers.on("target"):
        events, _ = store.connector_page("bot", "123", result["cursor"])
        assert len(events) == 1
    assert workers.starts == ["target"]


def test_cancelled_sweep_does_not_borrow(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    stop.set()

    def unexpected_borrow() -> Any:
        pytest.fail("cancelled journal maintenance borrowed a connection")

    monkeypatch.setattr(journal, "get_db", unexpected_borrow)
    journal.sweep(stop=stop)
