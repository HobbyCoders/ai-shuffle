"""Installed Gmail package replay with fake HTTP and disposable PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import closing, contextmanager
from uuid import uuid4
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core import connector_packages, connector_service, watches, watch_sources
from app.core import watch_manager as manager
from app.core.connectors import package_proxy
from app.core.connectors.manager import connector_manager
from app.db import database, notifications, watches as store
from tests.core.test_watches import owner  # noqa: F401
from tests.db.test_bounded_maintenance_postgres import cluster  # noqa: F401
from tests.db.schema_lifecycle_helpers import pg_connect


# Imported only by the actual isolated worker, not by the host.
FAKE_PACKAGE = r'''from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from app.core.connectors.gmail import GmailConnector
from app.core.connectors.base import ConnectorStatus

class Reply:
    def __init__(self, value: dict[str, Any], status: int = 0) -> None:
        self.value, self.status = value, status

    def execute(self) -> dict[str, Any]:
        if self.status:
            exc = RuntimeError("fake HTTP error")
            exc.resp = SimpleNamespace(status=self.status)
            raise exc
        return self.value

class FakeSDK:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.state = json.loads(self.path.read_text())
        self.resource = ""

    def users(self) -> Any:
        return self

    def history(self) -> Any:
        self.resource = "history"
        return self

    def messages(self) -> Any:
        self.resource = "messages"
        return self

    def getProfile(self, **kwargs: Any) -> Reply:
        return Reply({"historyId": self.state.get("anchor", "100")})

    def list(self, **kwargs: Any) -> Reply:
        with self.path.with_suffix(".calls").open("a") as log:
            log.write(json.dumps([self.resource, kwargs]) + "\n")
        if self.resource == "messages":
            return Reply(self.state.get("snapshot", {}), self.state.get("snapshot_error", 0))
        page = self.state.get("pages", {}).get(kwargs.get("pageToken", "first"), {})
        return Reply(page, self.state.get("error", 0))

    def get(self, **kwargs: Any) -> Reply:
        return Reply({"snippet": "ordinary mail", "payload": {"headers": [
            {"name": "Subject", "value": self.state.get("subject", "important")}
        ]}})

class FakeConnector(GmailConnector):
    async def connect(self) -> bool:
        self._status = ConnectorStatus.CONNECTED
        return True

    async def disconnect(self) -> None:
        pass

    def build_thread_client(self) -> Any:
        return FakeSDK(self.config["state_path"])
'''


@pytest.fixture
async def mailbox(
    owner: dict[str, Any], cluster: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[dict[str, Any], Any, Path]]:
    schema = "gmail_" + uuid4().hex
    with closing(pg_connect(cluster)) as conn:
        conn.cursor().execute("CREATE SCHEMA " + schema)

    @contextmanager
    def connect() -> Iterator[Any]:
        with closing(pg_connect(cluster)) as conn:
            conn.autocommit = False
            with conn:
                conn.cursor().execute("SET search_path TO " + schema)
                yield conn

    monkeypatch.setattr(store, "get_db", connect)
    monkeypatch.setattr(watches, "get_db", connect)
    with connect() as conn:
        store.create_schema(conn.cursor())
    monkeypatch.setattr(connector_packages, "require_installed", lambda _: "fake-runtime")
    connector = await connector_service.create_connector("Mail", "gmail", {}, [])
    database.update_connector(connector["id"], is_active=True)
    state = tmp_path / "mail.json"
    state.write_text('{"anchor": "100"}')
    package = tmp_path / "package"
    package.mkdir()
    (package / "fake_mail.py").write_text(FAKE_PACKAGE)
    sources = Path(connector_packages.__file__).with_name("connectors")
    for filename in ("gmail.py", "gmail_actions.py", "gmail_history.py"):
        (package / filename).write_bytes((sources / filename).read_bytes())
    spec = {"module": "fake_mail", "class": "FakeConnector", "files": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in package.iterdir()
    }}
    monkeypatch.setattr(package_proxy, "interpreter", lambda _: Path(sys.executable))
    cls = package_proxy.proxy_class("gmail", package, spec)
    instance = cls(connector["id"], "gmail", {"state_path": str(state)}, AsyncMock(), AsyncMock())
    try:
        assert await instance.connect()
        assert not hasattr(instance, "build_thread_client")
        assert not hasattr(instance, "_credentials")
        monkeypatch.setattr(
            connector_manager, "get_running_connector", AsyncMock(return_value=instance)
        )
        yield connector, instance, state
    finally:
        await instance.disconnect()
        with closing(pg_connect(cluster)) as conn:
            conn.cursor().execute("DROP SCHEMA " + schema + " CASCADE")


def history(ids: list[str], end: str = "200", token: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {"historyId": end, "history": [{"messagesAdded": [
        {"message": {"id": mid, "labelIds": ["INBOX"]}} for mid in ids
    ]}]}
    if token:
        page["nextPageToken"] = token
    return page


def make_watch(owner: dict[str, Any], connector: dict[str, Any]) -> dict[str, Any]:
    prepared = watches.prepare(owner["id"], {
        "description": "important mail", "filter": {"contains": "important"},
        "source": {"kind": "connector", "connector_id": connector["id"]},
    })
    return store.get(store.create(**prepared))


@pytest.mark.asyncio
async def test_package_pages_atomic_rollback_replay_and_grants(
    owner: dict[str, Any], mailbox: tuple[dict[str, Any], Any, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, instance, state = mailbox
    watch = make_watch(owner, connector)
    events, cursor = await watch_sources.poll(watch)
    assert events == [] and cursor == {"history_id": "100"}
    assert store.checkpoint(watch, events, cursor)
    watch = store.get(watch["id"])
    state.write_text(json.dumps({"pages": {
        "first": history(["read-mail", "read-mail"], token="next"),
        "next": history(["second"]),
    }}))
    events, cursor = await watch_sources.poll(watch)
    assert len(events) == 1 and events[0][0] == "email_received:read-mail"
    assert cursor == {"history_id": "100", "page_token": "next"}
    insert = store._insert
    with monkeypatch.context() as patch:
        def fail(*args: Any, **kwargs: Any) -> None:
            insert(*args, **kwargs)
            raise RuntimeError("failure after insert but before commit")
        patch.setattr(store, "_insert", fail)
        with pytest.raises(RuntimeError, match="before commit"):
            store.checkpoint(watch, events, cursor)
    assert store.get(watch["id"])["cursor"] == {"history_id": "100"}
    assert store.events(watch["id"]) == []
    assert await watch_sources.poll(store.get(watch["id"])) == (events, cursor)
    assert store.checkpoint(watch, events, cursor)
    assert not store.checkpoint(watch, events, cursor)  # Stale/raced poll cannot advance.
    await instance.disconnect()
    assert await instance.connect()  # New isolated process resumes the saved page token.
    events, cursor = await watch_sources.poll(store.get(watch["id"]))
    assert cursor == {"history_id": "200"}
    assert store.checkpoint(store.get(watch["id"]), events, cursor)
    assert len(store.events(watch["id"])) == 2
    calls = [json.loads(line) for line in state.with_suffix(".calls").read_text().splitlines()]
    assert calls[-1][1]["pageToken"] == "next"
    assert calls[-1][1]["startHistoryId"] == "100"
    database.update_connector(connector["id"], is_active=False)
    with pytest.raises(PermissionError):
        await watch_sources.poll(store.get(watch["id"]))
    assert len(state.with_suffix(".calls").read_text().splitlines()) == len(calls)


@pytest.mark.asyncio
async def test_idle_nonmatching_and_expired_gap_are_model_free(
    owner: dict[str, Any], mailbox: tuple[dict[str, Any], Any, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, _, state = mailbox
    watch = make_watch(owner, connector)
    model = AsyncMock(side_effect=AssertionError("must not invoke a model"))
    monkeypatch.setattr(manager, "summarize", model)
    deliver = AsyncMock(side_effect=AssertionError("must not deliver nonmatching mail"))
    loop = manager.WatchManager()
    monkeypatch.setattr(loop, "_deliver", deliver)
    await loop._run_watch(watch)
    state.write_text(json.dumps({"pages": {"first": {"historyId": "101"}}}))
    await manager.WatchManager()._run_watch(store.get(watch["id"]))
    state.write_text(json.dumps({"subject": "routine", "pages": {"first": history(["x"])}}))
    await loop._run_watch(store.get(watch["id"]))
    assert store.get(watch["id"])["cursor"] == {"history_id": "200"}
    assert store.events(watch["id"]) == []
    state.write_text(json.dumps({"error": 404, "anchor": "300", "subject": "routine",
        "snapshot": {"messages": [{"id": "read-mail"}], "nextPageToken": "omitted"}}))
    before = store.get(watch["id"])
    with monkeypatch.context() as patch:
        patch.setattr(notifications, "create_notification", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("notice not durable")
        ))
        with pytest.raises(RuntimeError, match="not durable"):
            await loop._run_watch(before)
    assert store.get(watch["id"])["cursor"] == {"history_id": "200"}
    # Lost checkpoint acknowledgement must not duplicate the deterministic warning.
    with monkeypatch.context() as patch:
        patch.setattr(store, "checkpoint", lambda *a: False)
        await loop._run_watch(before)
    await loop._run_watch(before)
    current = store.get(watch["id"])
    assert current["cursor"]["history_id"] == "300"
    assert current["cursor"]["history_gap"]["truncated"] is True
    notices = notifications.list_notifications("admin")
    gaps = [n for n in notices if n["title"] == "Gmail watch history gap"]
    assert len(gaps) == 1 and "omitted" in gaps[0]["message"]
    assert store.events(watch["id"]) == []
    model.assert_not_awaited()
    deliver.assert_not_awaited()
    calls = [json.loads(line) for line in state.with_suffix(".calls").read_text().splitlines()]
    snapshots = [args for kind, args in calls if kind == "messages"]
    assert all(args == {"userId": "me", "labelIds": ["INBOX"], "maxResults": 100}
               for args in snapshots)
    assert len(snapshots) == 3  # One bounded page per attempt; never follows "omitted".


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_non_expiry_failures_do_not_rebase(
    owner: dict[str, Any], mailbox: tuple[dict[str, Any], Any, Path], status: int
) -> None:
    connector, _, state = mailbox
    watch = make_watch(owner, connector)
    store.checkpoint(watch, [], {"history_id": "100"})
    state.write_text(json.dumps({"error": status}))
    with pytest.raises(ValueError, match="Update the connector package"):
        await watch_sources.poll(store.get(watch["id"]))
    assert store.get(watch["id"])["cursor"] == {"history_id": "100"}
    assert all(json.loads(line)[0] == "history"
               for line in state.with_suffix(".calls").read_text().splitlines())


@pytest.mark.asyncio
async def test_confirmed_package_watch_queues_matching_mail_once(
    owner: dict[str, Any], mailbox: tuple[dict[str, Any], Any, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.permission_handler import permission_handler
    from app.core.agent_types import PermissionResultAllow
    from app.db import turns

    connector, instance, state = mailbox
    monkeypatch.setattr(
        permission_handler, "request_permission", AsyncMock(return_value=PermissionResultAllow())
    )
    result = await watches.create_from_conversation(owner["id"], {
        "description": "important mail", "filter": {"contains": "important"},
        "source": {"kind": "connector", "connector_id": connector["id"]},
    })
    watch = result["watch"]
    assert watch["cursor"] == {"history_id": "100"}
    state.write_text(json.dumps({"pages": {"first": history(["matching-read-mail"])}}))
    await manager.WatchManager()._run_watch(watch)
    event = store.events(watch["id"])[0]
    turn = turns.get_turn_by_command(owner["id"], event["id"])
    assert turn and turn["source"] == "watch" and turn["state"] == "queued"
    assert event["payload"]["message_id"] == "matching-read-mail"
    await instance.disconnect()
    assert await instance.connect()
    await manager.WatchManager()._run_watch(store.get(watch["id"]))
    assert len(store.events(watch["id"])) == 1
    assert store.events(watch["id"])[0]["turn_id"] == turn["id"]
    store.set_status(watch["id"], "admin", "paused")
    before = state.with_suffix(".calls").read_text()
    await manager.WatchManager()._run_watch(store.get(watch["id"]))
    store.set_status(watch["id"], "admin", "stopped")
    await manager.WatchManager()._run_watch(store.get(watch["id"]))
    assert state.with_suffix(".calls").read_text() == before


@pytest.mark.asyncio
async def test_legacy_cursor_and_resync_failure_keep_bookmarks(
    owner: dict[str, Any], mailbox: tuple[dict[str, Any], Any, Path],
) -> None:
    connector, _, state = mailbox
    watch = make_watch(owner, connector)
    legacy = {"legacy_seen_ids": ["old"], "legacy_after": "1234"}
    store.checkpoint(watch, [], legacy)
    state.write_text(json.dumps({"anchor": "500", "snapshot": {
        "messages": [{"id": "old"}, {"id": "new"}], "nextPageToken": "legacy-next"
    }}))
    events, cursor = await watch_sources.poll(store.get(watch["id"]))
    assert [key for key, _ in events] == ["email_received:new"]
    assert cursor == dict(legacy, history_id="500", legacy_page_token="legacy-next")
    store.checkpoint(store.get(watch["id"]), events, cursor)
    state.write_text(json.dumps({"snapshot": {}}))
    events, cursor = await watch_sources.poll(store.get(watch["id"]))
    assert cursor == {"history_id": "500"}
    assert events == []
    store.checkpoint(store.get(watch["id"]), events, cursor)
    state.write_text(json.dumps({"error": 404, "anchor": "600", "snapshot_error": 500}))
    with pytest.raises(ValueError):
        await watch_sources.poll(store.get(watch["id"]))
    assert store.get(watch["id"])["cursor"] == {"history_id": "500"}
