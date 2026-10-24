"""Upgrade the old loop's actual source/cursor/event/run rows into watches."""

from __future__ import annotations
from typing import Any
from datetime import timedelta

import pytest

from app.core import watch_manager
from app.db import assistant, database, turns, watch_migration, watches as store
from app.db.base import get_db, table_exists
from tests.core.test_watches import owner, mail  # noqa: F401


def legacy_schema(c: Any) -> None:
    c.execute("""CREATE TABLE assistant_sources (
        assistant_id TEXT,connector_id TEXT,enabled BOOLEAN,watermark JSONB,
        last_observed_at TIMESTAMPTZ,
        PRIMARY KEY(assistant_id,connector_id))""")
    c.execute("""CREATE TABLE assistant_runs (
        id TEXT PRIMARY KEY,assistant_id TEXT,session_id TEXT,turn_id TEXT,status TEXT,
        deadline_at TIMESTAMPTZ,cost_usd DOUBLE PRECISION,created_at TIMESTAMPTZ)""")
    c.execute("""CREATE TABLE assistant_events (
        id TEXT PRIMARY KEY,assistant_id TEXT,source_id TEXT,event_type TEXT,fingerprint TEXT,
        payload JSONB,status TEXT,run_id TEXT,observed_at TIMESTAMPTZ)""")


@pytest.mark.asyncio
async def test_email_loop_upgrade_adopts_inflight_turn_and_preserves_backlog(
    owner: dict[str, Any], mail: Any
) -> None:
    connector, instance, sdk = mail
    assistant.configure("watch-test", {"agent_id": "watch-agent", "enabled": True})
    session = database.create_session(
        "old-research", "watch-agent", mode="chat", is_active=True, target_instance_id="watch-test"
    )
    turn = turns.enqueue_turn(
        session["id"],
        "assistant",
        prompt="Read only this email batch",
        profile_id="watch-agent",
        command_id="old-run",
        overrides={"_assistant_proactive": {"version": 1}},
    )
    stamp = store.now()
    with get_db() as conn:
        c = conn.cursor()
        legacy_schema(c)
        c.execute(
            "INSERT INTO assistant_sources VALUES (%s,%s,TRUE,%s,%s)",
            ("watch-test", connector["id"], store.encode({"seen_ids": ["already-seen"]}), stamp),
        )
        c.execute(
            "INSERT INTO assistant_runs VALUES (%s,%s,%s,%s,'running',%s,0,%s)",
            (
                "old-run",
                "watch-test",
                session["id"],
                turn["id"],
                stamp + timedelta(seconds=180),
                stamp,
            ),
        )
        c.execute(
            "INSERT INTO assistant_runs VALUES (%s,%s,%s,NULL,'completed',%s,0.4,%s)",
            ("done-run", "watch-test", session["id"], stamp, stamp),
        )
        for eid, mid, status, run in [
            ("backlog", "backlog-mail", "pending", None),
            ("active", "active-mail", "claimed", "old-run"),
            ("done", "done-mail", "settled", "done-run"),
        ]:
            payload = {"data": store.encode({"message_id": mid, "subject": "Untrusted mail"})}
            c.execute(
                "INSERT INTO assistant_events VALUES (%s,%s,%s,'email_received',%s,%s,%s,%s,%s)",
                (
                    eid,
                    "watch-test",
                    connector["id"],
                    mid,
                    store.encode(payload),
                    status,
                    run,
                    stamp,
                ),
            )
        assert watch_migration.migrate(c) == ["email_research_to_watches"]
        for table in ("assistant_sources", "assistant_runs", "assistant_events"):
            assert not table_exists(c, table)
        assert watch_migration.migrate(c) == []
    watch = store.list_watches()[0]
    assert watch["source"] == {"kind": "connector", "connector_id": connector["id"]}
    assert watch["cursor"]["legacy_seen_ids"] == ["already-seen"]
    assert watch["permissions"] == {"read_only": True, "tools": []}
    assert watch["cost_usd"] == 0.4
    pending = store.events(watch["id"], ("pending",))
    assert len(pending) == 1 and pending[0]["payload"]["message_id"] == "backlog-mail"
    active = store.events(watch["id"], ("running",))[0]
    assert active["command_id"] == "old-run" and active["session_id"] == session["id"]
    metadata = turns.get_turn(turn["id"])["overrides"]["_assistant_proactive"]
    assert metadata["kind"] == "watch" and metadata["event_id"] == active["id"]
    await watch_manager.WatchManager()._deliver(watch, active)
    assert turns.get_turn_by_command(session["id"], "old-run")["id"] == turn["id"]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM turns WHERE session_id=%s", (session["id"],))
        assert c.fetchone()["n"] == 1


@pytest.mark.parametrize(
    "state", [{"enabled": True, "paused": True}, {"enabled": False, "paused": False}]
)
def test_migration_preserves_paused_or_disabled_loop(
    owner: dict[str, Any], mail: Any, state: Any
) -> None:
    connector, _, _ = mail
    assistant.configure("watch-test", dict(agent_id="watch-agent", **state))
    with get_db() as conn:
        c = conn.cursor()
        legacy_schema(c)
        c.execute(
            "INSERT INTO assistant_sources VALUES (%s,%s,TRUE,NULL,NULL)",
            ("watch-test", connector["id"]),
        )
        watch_migration.migrate(c)
    row = store.list_watches()[0]
    assert row["status"] == "paused" and "paused or disabled" in row["reason"]


def test_missing_agent_aborts_instead_of_discarding_email_sources(
    owner: dict[str, Any], mail: Any
) -> None:
    connector, _, _ = mail
    assistant.configure("watch-test", {"agent_id": None, "enabled": True})
    with get_db() as conn:
        c = conn.cursor()
        legacy_schema(c)
        c.execute("DELETE FROM agents WHERE id='default'")
        c.execute(
            "INSERT INTO assistant_sources VALUES (%s,%s,TRUE,NULL,NULL)",
            ("watch-test", connector["id"]),
        )
    with pytest.raises(ValueError, match="requires an owner agent"):
        with get_db() as conn:
            watch_migration.migrate(conn.cursor())
    with get_db() as conn:
        assert table_exists(conn.cursor(), "assistant_sources")
    assert store.list_watches() == []


def test_disabled_or_non_email_sources_do_not_gain_permission(
    owner: dict[str, Any], mail: Any
) -> None:
    connector, _, _ = mail
    assistant.configure("watch-test", {"agent_id": "watch-agent", "enabled": True})
    with get_db() as conn:
        c = conn.cursor()
        legacy_schema(c)
        c.execute(
            "INSERT INTO assistant_sources VALUES (%s,%s,FALSE,NULL,NULL)",
            ("watch-test", connector["id"]),
        )
        watch_migration.migrate(c)
        assistant.create_schema(c)  # Fresh schema does not re-create retired tables.
        assert not table_exists(c, "assistant_sources")
    assert store.list_watches() == []
