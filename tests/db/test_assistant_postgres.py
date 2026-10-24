"""Watch transactions and restart contracts on disposable PostgreSQL."""

from __future__ import annotations
from pytest import MonkeyPatch
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_real_postgres_atomic_cursor_budget_and_restart(monkeypatch: MonkeyPatch) -> None:
    from app.core.config import settings
    from app.db import base, watches as store

    identity = "watch-pg-" + uuid4().hex
    monkeypatch.setattr(settings, "instance_id", identity)
    wid = store.create(
        owner="admin",
        creating_session_id=None,
        worker_id=identity,
        agent_id="default",
        description="Watch",
        source={"kind": "schedule", "interval_seconds": 60},
        filter={},
        action="alert",
        permissions={"tools": [], "read_only": True},
        daily_budget_usd=1,
    )
    watch = store.get(wid)
    with ThreadPoolExecutor(max_workers=4) as executor:
        writes = list(
            executor.map(lambda _: store.checkpoint(watch, [("one", {})], {"next": 1}), range(8))
        )
    assert sum(writes) == 1
    event = store.events(wid)[0]
    with ThreadPoolExecutor(max_workers=4) as executor:
        admissions = list(executor.map(lambda _: store.reserve(wid, event["id"], 1), range(8)))
    assert sum(admissions) == 1
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert store.get(wid)["cursor"] == {"next": 1}
    assert len(store.events(wid)) == 1
