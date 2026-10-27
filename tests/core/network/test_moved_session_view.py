"""Recall on the source follows the event mirror after a real handoff (#669)."""
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
async def test_source_views_new_owner_reply_after_handoff(pair, monkeypatch):
    source, owner, _, _ = pair
    sid = "handoff-test"
    # The handoff fixture's minimal legacy message is enough for transfer;
    # normalize it before exercising the real store's append/load path too.
    with source.active(), get_db() as conn:
        conn.cursor().execute("UPDATE fluid_session_nodes SET message=%s WHERE session_id=%s",
            (json.dumps(Message(role="user", content=[Text(text="Native history")]).to_dict()), "ses_source"))

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
        session_events.append_events(sid, [{"type": "message.assistant", "payload": {
            "content": "reply written after the move", "kind": "text"}}])
        engine.scan_all()
    converge([source, owner])
    with source.active():
        row = database.get_session(sid)
        assert row["owner_device_id"] == owner.device_id
        # Handoff retains the old local transcript/id; the mirror must win.
        assert row["sdk_session_id"] == "ses_source"
        store = PgFluidStore()
        for requested_id in (sid, "ses_source"):
            result = await session_view.handler({"session_id": requested_id},
                ToolCtx(extra={"session_view": store.session_slice}))
            assert not result.is_error, result.text
            assert "reply written after the move" in result.text
            assert f"[on {owner.name}]" in result.text
