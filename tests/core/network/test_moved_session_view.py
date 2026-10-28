"""Recall, search and listing follow ownership after a real handoff (#669, #742)."""
import json

import pytest

from app.core.provider.fluid_pg_store import PgFluidStore
from app.db import database, session_events
from app.db.base import get_db
from app.network import control, engine, handoff
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode
from fluid.tools.builtins import session_view
from fluid.tools.registry import ToolCtx
from tests.core.network.harness import converge
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["view", "listing", "search"])
async def test_source_reads_new_owner_reply_after_handoff(pair, monkeypatch, surface):
    source, owner, _, _ = pair
    sid = "handoff-test"
    # The handoff fixture's minimal legacy message is enough for transfer;
    # normalize it before exercising the real store's append/load path too.
    with source.active(), get_db() as conn:
        conn.cursor().execute("UPDATE fluid_session_nodes SET message=%s WHERE session_id=%s",
            (json.dumps(Message(role="user", content=[Text(text="staleonly native history")]).to_dict()), "ses_source"))

    async def call(device, action, body, **kwargs):
        assert device == source.device_id and action == "handoff"
        with source.active():
            return await control.handle(action, body, from_device=owner.device_id)

    monkeypatch.setattr(control, "call", call)
    with owner.active():
        await handoff.receive(database.get_session(sid))
        new_store_id = database.get_session(sid)["sdk_session_id"]
        assert new_store_id != "ses_source"
        store = PgFluidStore()
        await store.append(new_store_id, SessionNode(node_id="new-owner-reply", parent_id="node1",
            message=Message(role="assistant", content=[Text(text="reply written after the move")])))
        session_events.append_events(sid, [
            {"type": "message.user", "payload": {"content": "follow-up after the move", "kind": "text"}},
            {"type": "message.assistant", "payload": {
                "content": "reply written after the move", "kind": "text"}},
        ])
        engine.scan_all()
    converge([source, owner])
    with source.active():
        row = database.get_session(sid)
        assert row["owner_device_id"] == owner.device_id
        # Handoff retains the old local transcript/id; the mirror must win.
        assert row["sdk_session_id"] == "ses_source"
        # A leftover Claude-format transcript must not supply search hits either.
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO session_store_entries (project_key, session_id, entry) VALUES (%s, %s, %s)",
                ("project", "ses_source", json.dumps({"type": "assistant", "message": {
                    "content": "delegatedonly stale reply"}})),
            )
        store = PgFluidStore()
        if surface == "listing":
            rows = await store.recent_sessions()
            assert [r.session_id for r in rows] == [sid]
            assert rows[0].preview == "reply written after the move"
            assert rows[0].message_count == 2  # mirrored follow-up + reply, not the one local node
            assert rows[0].worker == owner.name
        elif surface == "search":
            hits = await store.search(["reply"])
            assert [h.session_id for h in hits] == [sid]
            assert hits[0].texts == [("assistant", "reply written after the move")]
            assert hits[0].worker == owner.name
            assert await store.search(["staleonly"]) == []
            assert await store.search(["delegatedonly"]) == []
        else:
            for requested_id in (sid, "ses_source"):
                result = await session_view.handler({"session_id": requested_id},
                    ToolCtx(extra={"session_view": store.session_slice}))
                assert not result.is_error, result.text
                assert "reply written after the move" in result.text
                assert "staleonly" not in result.text
                assert f"[on {owner.name}]" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", ["native", "delegated"])
@pytest.mark.parametrize("local_id", ["app", "store"])
async def test_moved_chat_search_falls_back_until_events_arrive(pair, monkeypatch, transcript, local_id):
    source, owner, _, _ = pair
    sid = "handoff-test"
    with source.active():
        store = PgFluidStore()
        # Use normal writers for the fallback, then move the chat for real.
        with get_db() as conn:
            conn.cursor().execute("UPDATE fluid_session_nodes SET message=%s WHERE session_id=%s",
                (json.dumps(Message(role="user", content=[Text(text="original history")]).to_dict()), "ses_source"))
        store_id = sid if local_id == "app" else "ses_source"
        if transcript == "native":
            if local_id == "app":
                await store.create_session(store_id)
            await store.append(store_id, SessionNode(node_id="fallback", parent_id=None,
                message=Message(role="assistant", content=[Text(text="localonly fallback reply")])))
        else:
            with get_db() as conn:
                conn.cursor().execute("INSERT INTO session_store_entries (project_key, session_id, entry) "
                    "VALUES (%s, %s, %s)", ("project", store_id, json.dumps({"type": "assistant", "message": {
                        "content": "localonly fallback reply"}})))

    async def call(device, action, body, **kwargs):
        assert device == source.device_id and action == "handoff"
        with source.active():
            return await control.handle(action, body, from_device=owner.device_id)

    monkeypatch.setattr(control, "call", call)
    with owner.active():
        await handoff.receive(database.get_session(sid))
    converge([source, owner])
    with source.active():
        assert database.get_session(sid)["owner_device_id"] == owner.device_id
        assert session_events.get_events(sid) == []
        [hit] = await PgFluidStore().search(["localonly", "fallback"])
        assert hit.session_id == store_id
        assert hit.texts == [("assistant", "localonly fallback reply")]

    with owner.active():
        session_events.append_events(sid, [{"type": "message.assistant", "payload": {
            "content": "mirroronly current reply", "kind": "text"}}])
        engine.scan_all()
    converge([source, owner])
    with source.active():
        assert await PgFluidStore().search(["localonly"]) == []
        [hit] = await PgFluidStore().search(["mirroronly"])
        assert hit.session_id == sid and hit.worker == owner.name


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("transcript", ["native", "delegated"])
@pytest.mark.parametrize("term_count", [1, 8])
def test_moved_search_materializes_ownership_once_per_arm(indexed, transcript, term_count):
    from app.core.provider import fluid_pg_store as recall

    class Cursor:
        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchone(self):
            return None  # No deferred-history columns in this SQL-shape test.

        def fetchall(self):
            return []

    cur = Cursor()
    native = transcript == "native"
    table, key, ts, payload = (("fluid_session_nodes", "node_id", "ts", "message") if native
        else ("session_store_entries", "id", "created_at", "entry"))
    patterns = [f"%term{i}%" for i in range(term_count)]
    select = f"session_id, {ts}, {payload}"
    if indexed:
        recall._indexed_top_rows(cur, recall.session_recall.NATIVE if native else recall.session_recall.ENTRY,
            table, key, "session_id <> %s", ["caller"], select, patterns, 25, "local-worker")
    else:
        recall._top_rows(cur, table, "recall_text", ts, "session_id <> %s", ["caller"],
            select, patterns, "local-worker")
    # No ids are fetched into Python or repeated in every term's NOT IN list.
    # Even eight terms bind the worker once and calculate mirrored owners once.
    assert cur.params[0] == "local-worker" and cur.params.count("local-worker") == 1
    assert cur.sql.count("SELECT s.id, s.sdk_session_id FROM sessions s") == 1
    assert cur.sql.count("EXISTS (SELECT 1 FROM session_events e WHERE e.session_id = s.id)") == 1
    assert "mirrored_ids AS MATERIALIZED" in cur.sql
    assert "NOT EXISTS (SELECT 1 FROM mirrored_ids WHERE mirrored_id = session_id)" in cur.sql
    assert "NOT IN" not in cur.sql
    assert cur.sql.index("mirrored_sessions AS MATERIALIZED") < cur.sql.index("), m AS (")


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", ["native", "delegated"])
async def test_unmoved_chat_keeps_reading_local_transcript(pair, transcript):
    source, _, _, _ = pair
    with source.active():
        store = PgFluidStore()
        local_message = Message(role="assistant", content=[Text(text="localonly reply")])
        with get_db() as conn:
            cur = conn.cursor()
            if transcript == "native":
                cur.execute("UPDATE fluid_session_nodes SET message=%s WHERE session_id=%s",
                            (json.dumps(local_message.to_dict()), "ses_source"))
            else:
                database.update_session("handoff-test", sdk_session_id="ses_delegated")
                cur.execute("INSERT INTO session_store_entries (project_key, session_id, entry) "
                            "VALUES (%s, %s, %s)",
                            ("project", "ses_delegated", json.dumps({"type": "assistant", "message": {
                                "content": "localonly reply"}})))
        session_events.append_events("handoff-test", [{"type": "message.assistant", "payload": {
            "content": "mirroronly reply", "kind": "text"}}])
        store_id = database.get_session("handoff-test")["sdk_session_id"]
        rows = await store.recent_sessions()
        assert [r.session_id for r in rows] == [store_id]
        assert rows[0].preview == "localonly reply" and rows[0].message_count == 1
        assert rows[0].worker is None
        hits = await store.search(["localonly"])
        assert store_id in {h.session_id for h in hits}
        assert all(h.worker is None for h in hits)
        assert await store.search(["mirroronly"]) == []
        for requested_id in ("handoff-test", store_id):
            window = await store.session_slice(requested_id)
            assert window is not None and window.worker is None
            assert [m.text for m in window.messages] == ["localonly reply"]
