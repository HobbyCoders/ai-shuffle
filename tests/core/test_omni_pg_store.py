"""PgOmniStore — omni's SessionStore protocol over the app's Postgres.

Runs against the pg_shim (`mock_db`), exactly like the PgSessionStore tests,
so the DDL under test is the real schema_postgres v70 tables. The protocol
semantics asserted here mirror omni's own JsonlStore behaviors: append moves
the head, load_path resolves leaf→root root-first, set_head forks without
copying, and the event log replays idempotently by (session_id, seq).
"""

from __future__ import annotations

import logging

import pytest

from app.core.provider.omni_pg_store import PgOmniStore

from omni.events.model import TextDelta
from omni.messages.content import Message, Text
from omni.store.base import SessionNode


def _node(node_id: str, parent_id, text: str) -> SessionNode:
    return SessionNode(
        node_id=node_id,
        parent_id=parent_id,
        message=Message(role="user", content=[Text(text=text)]),
    )


@pytest.mark.asyncio
async def test_round_trip_and_head_advance(mock_db):
    store = PgOmniStore()
    meta = await store.create_session("ses_pg1")
    assert meta.session_id == "ses_pg1" and meta.head_node_id is None

    await store.append("ses_pg1", _node("n1", None, "hello"))
    await store.append("ses_pg1", _node("n2", "n1", "world"))

    path = await store.load_path("ses_pg1")
    assert [n.node_id for n in path] == ["n1", "n2"]
    assert path[0].message.text() == "hello"
    assert (await store.get_session("ses_pg1")).head_node_id == "n2"


@pytest.mark.asyncio
async def test_persistence_across_store_instances(mock_db):
    store = PgOmniStore()
    await store.create_session("ses_pg2")
    await store.append("ses_pg2", _node("a", None, "root"))
    await store.append("ses_pg2", _node("b", "a", "leaf"))
    await store.append_event("ses_pg2", TextDelta(session_id="ses_pg2", index=0,
                                                  text="x", seq=1))

    # Fresh instance = cold cache = must load everything back from Postgres.
    reloaded = PgOmniStore()
    meta = await reloaded.get_session("ses_pg2")
    assert meta is not None and meta.head_node_id == "b"
    path = await reloaded.load_path("ses_pg2")
    assert [n.node_id for n in path] == ["a", "b"]
    assert path[1].message.text() == "leaf"
    events = await reloaded.events_since("ses_pg2", 0)
    assert len(events) == 1 and events[0].text == "x" and events[0].seq == 1


@pytest.mark.asyncio
async def test_fork_via_set_head(mock_db):
    store = PgOmniStore()
    await store.create_session("ses_pg3")
    await store.append("ses_pg3", _node("r", None, "root"))
    await store.append("ses_pg3", _node("c1", "r", "branch-1"))

    # Rewind to root, then grow a second branch — nothing is copied.
    await store.set_head("ses_pg3", "r")
    await store.append("ses_pg3", _node("c2", "r", "branch-2"))

    assert [n.node_id for n in await store.load_path("ses_pg3")] == ["r", "c2"]
    # The abandoned branch is still addressable by explicit leaf.
    assert [n.node_id for n in await store.load_path("ses_pg3", "c1")] == ["r", "c1"]

    # Fork survives a cold reload.
    reloaded = PgOmniStore()
    assert (await reloaded.get_session("ses_pg3")).head_node_id == "c2"


@pytest.mark.asyncio
async def test_event_append_is_idempotent_per_seq(mock_db):
    store = PgOmniStore()
    await store.create_session("ses_pg4")
    ev = TextDelta(session_id="ses_pg4", index=0, text="dup", seq=7)
    await store.append_event("ses_pg4", ev)

    # A replayed append of the same seq must not duplicate the durable row.
    replayer = PgOmniStore()
    await replayer.append_event("ses_pg4", ev)

    fresh = PgOmniStore()
    events = await fresh.events_since("ses_pg4", 0)
    assert len(events) == 1

    # Cursor semantics: strictly-greater-than.
    assert await fresh.events_since("ses_pg4", 7) == []


@pytest.mark.asyncio
async def test_seq_collision_keeps_first_row_and_warns(mock_db, caplog):
    """The conflict clause exists for legitimate replays, but it also hid real
    loss (an engine whose bus restarted numbering at 1 for a resumed session).
    A dropped insert must now be visible in the logs, and must not raise."""
    store = PgOmniStore()
    await store.create_session("ses_pg7")
    await store.append_event(
        "ses_pg7", TextDelta(session_id="ses_pg7", index=0, text="first", seq=3)
    )

    with caplog.at_level(logging.WARNING, logger="app.core.provider.omni_pg_store"):
        await PgOmniStore().append_event(
            "ses_pg7", TextDelta(session_id="ses_pg7", index=0, text="second", seq=3)
        )

    events = await PgOmniStore().events_since("ses_pg7", 0)
    assert len(events) == 1 and events[0].text == "first"
    assert any(
        "DROPPED by seq collision" in r.getMessage()
        and "ses_pg7" in r.getMessage()
        and "seq=3" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_create_session_resumes_existing(mock_db):
    store = PgOmniStore()
    await store.create_session("ses_pg5")
    await store.append("ses_pg5", _node("n", None, "hi"))

    # create_session on an existing id returns the stored meta (resume), it
    # must not reset the head.
    resumed = await PgOmniStore().create_session("ses_pg5")
    assert resumed.head_node_id == "n"


@pytest.mark.asyncio
async def test_missing_session_is_none(mock_db):
    assert await PgOmniStore().get_session("ses_missing") is None


@pytest.mark.asyncio
async def test_cli_session_id_round_trip(mock_db):
    """The delegated CLI's --resume handle survives a cold reload (v70)."""
    store = PgOmniStore()
    meta = await store.create_session("ses_pg6")
    assert meta.cli_session_id is None  # nothing known until the CLI reports it

    await store.set_cli_session_id("ses_pg6", "cli-uuid-abc")
    # Live cache updates immediately...
    assert (await store.get_session("ses_pg6")).cli_session_id == "cli-uuid-abc"
    # ...and a fresh instance reads it back from Postgres.
    assert (await PgOmniStore().get_session("ses_pg6")).cli_session_id == "cli-uuid-abc"

    # A later init frame overwrites the stored handle.
    await PgOmniStore().set_cli_session_id("ses_pg6", "cli-uuid-def")
    assert (await PgOmniStore().get_session("ses_pg6")).cli_session_id == "cli-uuid-def"

    # Other meta fields are untouched by the update.
    await store.append("ses_pg6", _node("n1", None, "hi"))
    reloaded = await PgOmniStore().get_session("ses_pg6")
    assert reloaded.head_node_id == "n1"
    assert reloaded.cli_session_id == "cli-uuid-def"
