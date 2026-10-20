"""A regressed inbound frontier must not hide later peer chats from recall."""
import json

import pytest

from app.core import session_orchestration as orch, turn_scheduler
from app.core.provider.fluid_pg_store import PgFluidStore
from app.db import database, network as store, session_events
from app.network import engine
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode
from fluid.tools.builtins import session_search, session_view
from fluid.tools.builtins.session_orchestration import SPECS
from fluid.tools.registry import ToolCtx
from tests.core.network.harness import converge, exchange, set_owner_pref
from tests.core.network.test_start_session_remote import ARGS, network, start


@pytest.fixture(autouse=True)
def owner_chats(monkeypatch):
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda *args: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda *args: None)


def test_received_watermark_never_moves_backwards(workers):
    worker = workers("viewer")
    with worker.active(), store.get_db() as conn:
        cur = conn.cursor()
        store.set_watermark(cur, "peer", 1, 56)
        store.set_watermark(cur, "peer", 1, 54)
        assert store.get_watermark(cur, "peer", 1) == 56
        # A new incarnation has its own frontier, not the old one's maximum.
        store.set_watermark(cur, "peer", 2, 1)
        assert store.get_watermark(cur, "peer", 2) == 1


def test_single_apply_locks_before_reading_the_frontier(workers, monkeypatch):
    owner, viewer = workers("publisher"), workers("subscriber")
    from tests.core.network.harness import join
    with owner.active():
        engine.create_network("Recall")
    join(owner, viewer)
    with viewer.active():
        marks = store.local_watermarks()
    set_owner_pref(owner, "recall-lock", "reply")
    with owner.active():
        [op] = store.ops_since(marks, exclude_origin=viewer.device_id)
    locked = False
    lock_state, get_watermark = store.lock_state, store.get_watermark

    def lock(cursor):
        nonlocal locked
        result = lock_state(cursor)
        locked = True
        return result

    def frontier(cursor, origin, incarnation):
        assert locked, "a concurrent batch can pass an unlocked single-op reader"
        return get_watermark(cursor, origin, incarnation)

    monkeypatch.setattr(store, "lock_state", lock)
    monkeypatch.setattr(store, "get_watermark", frontier)
    with viewer.active():
        result = engine.apply_batch([op])
        assert result.applied == 1 and result.retry == 0


@pytest.mark.parametrize("remote", [True, False], ids=["peer", "local"])
async def test_started_chat_recall_and_lifecycle_after_stale_frontier(workers, monkeypatch, remote):
    owner, viewer, _ = network(workers, monkeypatch)
    with viewer.active():
        before = store.local_watermarks()[owner.device_id]["1"]
    # Use the publisher and receiver, not seeded mirror rows. Recreate the
    # observed legacy state: the forwarding log is ahead of the applied mark.
    set_owner_pref(owner, "recall-prefix", "first")
    set_owner_pref(owner, "recall-prefix", "second")
    converge([owner, viewer])
    with viewer.active():
        viewer.sql("UPDATE sync_watermarks SET last_seq=? WHERE origin_device_id=? AND incarnation=1",
                   (before, owner.device_id))
        outcome = await start(ARGS if remote else {k: v for k, v in ARGS.items() if k != "worker"})
        assert not outcome.is_error, outcome.text
        target = json.loads(outcome.text)["session_id"]
    runner = owner if remote else viewer
    sdk_id = "ses_" + target
    with runner.active():
        transcript = PgFluidStore()
        await transcript.create_session(sdk_id)
        await transcript.append(sdk_id, SessionNode(node_id="reply", parent_id=None,
            message=Message(role="assistant", content=[Text(text="amber narwhal recall reply")])))
        database.update_session(target, sdk_session_id=sdk_id)
        session_events.append_events(target, [{"type": "message.assistant", "payload": {
            "content": "amber narwhal recall reply", "kind": "text"}}])
        engine.scan_all()
    # The advertised frontier must request the missing prefix, not just the
    # larger forwarding-log head. Otherwise every later catalog/event op waits.
    exchange(owner, viewer)
    with viewer.active():
        row = database.get_session(target)
        assert row is not None, "peer catalog is stuck behind a skipped prefix"
        assert row["sdk_session_id"] == (None if remote else sdk_id)
        transcript = PgFluidStore()
        ctx = ToolCtx(session_id="caller", extra={
            "session_view": transcript.session_slice, "session_search": transcript.search,
            "session_recent": transcript.recent_sessions})
        viewed = await session_view.handler({"session_id": target}, ctx)
        assert not viewed.is_error, viewed.text
        assert "amber narwhal recall reply" in viewed.text
        found = await session_search.handler({"query": "narwhal", "scope": "all"}, ctx)
        assert "amber narwhal recall reply" in found.text
        assert (target if remote else sdk_id) in found.text
        for result in (viewed, found):
            assert ("[on ServerWorker]" in result.text) is remote
        for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
            _, handler = next((s, h) for s, h in SPECS if s.name == name)
            result = await handler({"session_id": target}, ToolCtx(extra={
                "session_orchestration": orch.make_handler(lambda: "caller")}))
            assert not result.is_error, result.text
        assert database.get_session(target)["status"] == "archived"
