"""Settle/compact reproduction through HTTP intake, runtime and the real pump.

NativeLoop does the compaction; HTTP admission, membership, lease, pump,
recorder and terminal events remain real. Provider/options/pool and idle-reader
boundaries use the existing engine harness. Old wait rows use the real DB and
question producer rather than seeding settled flags.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_pool, session_runtime
from app.core.session_pool import SessionState
from app.core.user_question_handler import user_question_handler
from app.db import session_events, sessions, turns
from tests.api import test_compact_first
from tests.core.engine_harness import StubPool
from tests.core.test_native_compact_adapter import _adapter, _collect
from tests.core.test_native_usage_settlement_paths import _engine_patches

compact_chat = test_compact_first.compact_chat


def test_retry_endpoint_reactivates_settled_chat(compact_chat):
    sessions.update_session("compact-chat", sdk_session_id="ses_existing")
    settled = compact_chat.patch("/api/v1/sessions/compact-chat/active", json={"is_active": False})
    assert settled.status_code == 200, settled.text
    response = compact_chat.post("/api/v1/sessions/compact-chat/resume")
    assert response.status_code == 200, response.text
    assert turns.get_turn(response.json()["turn_id"])["source"] == "resume"
    assert sessions.get_session("compact-chat")["is_active"]


@pytest.mark.parametrize("unsettle", [False, True])
@pytest.mark.parametrize("then_send", [False, True])
@pytest.mark.parametrize("old_wait", ["parked_question", "running_question", "prose"])
async def test_compact_after_settle_runs_to_completion(
    compact_chat, monkeypatch, tmp_path, unsettle, then_send, old_wait,
):
    adapter, loop = await _adapter(monkeypatch)
    try:
        # Give the actual engine enough history to reclaim, not a fake compact
        # success card. This is the same warm-up as the adapter compaction test.
        for i in range(6):
            adapter.set_turn_source("user")
            await adapter.send_query(f"turn {i} " + "words " * 200)
            await _collect(adapter)
        sessions.update_session("compact-chat", sdk_session_id=loop.session_id)
        old = turns.enqueue_turn("compact-chat", "user", prompt="old wait")
        turns.start_turn(old["id"])
        if old_wait != "running_question":
            turns.park_turn(old["id"], [])
        if old_wait == "prose":
            session_events.append_events("compact-chat", [
                {"type": "message.assistant", "turn_id": old["id"], "payload": {"content": "Proceed?"}},
                {"type": "turn.parked", "turn_id": old["id"], "payload": {"task_ids": []}},
            ])
        else:
            await user_question_handler.post_question(
                request_id="old-question", tool_use_id="old-tool", session_id="compact-chat",
                questions=[{"question": "Proceed?"}], broadcast_func=AsyncMock(),
            )
        response = compact_chat.patch("/api/v1/sessions/compact-chat/active", json={"is_active": False})
        assert response.status_code == 200, response.text
        after_settle = turns.get_turn(old["id"])
        if old_wait == "parked_question":
            assert after_settle["stop_reason"] == "parked_settled"
        elif old_wait == "prose":
            # Settling prose clears its attention, not its parked-idle turn.
            assert after_settle["state"] == "parked"
            assert sessions.get_session("compact-chat")["last_assistant_question_seq"] is None
        else:
            assert after_settle["state"] == "aborted"
            assert any(e["event_type"] == "turn.aborted" and e["turn_id"] == old["id"]
                       and e["payload"]["message"] == "Session settled"
                       for e in session_events.get_events("compact-chat"))
        if unsettle:
            response = compact_chat.patch("/api/v1/sessions/compact-chat/active", json={"is_active": True})
            assert response.status_code == 200, response.text
        response = test_compact_first.send(compact_chat, {"then_send": {"prompt": "continue my work"}} if then_send else None)
        assert response.status_code == 200, response.text
        assert sessions.get_session("compact-chat")["is_active"]
        compact_id = response.json()["turn_id"]
        # A production pooled adapter was connected with a permission callback.
        adapter._can_use_tool = AsyncMock()
        pool = StubPool(SessionState(adapter=adapter, is_connected=True))
        with ExitStack() as stack:
            _engine_patches(stack, pool, str(tmp_path))
            stack.enter_context(patch.object(session_pool, "_session_pool", pool))
            # There is no between-turn CLI stream in this in-memory provider.
            stack.enter_context(patch.object(session_runtime, "start_idle_reader"))
            stack.enter_context(patch.object(session_runtime, "stop_idle_reader", AsyncMock()))
            assert await session_runtime.SessionRuntime(turns.get_turn(compact_id)).execute() == "completed"
            if then_send:
                followup_id = response.json()["queued_turn_id"]
                assert turns.pop_ready("compact-chat")["id"] == followup_id
                assert await session_runtime.SessionRuntime(turns.get_turn(followup_id)).execute() == "completed"
        events = session_events.get_events("compact-chat")
        assert any(e["event_type"] == "system.compaction_boundary" for e in events)
        for tid in [compact_id] + ([followup_id] if then_send else []):
            assert turns.get_turn(tid)["state"] == "completed"
            assert not [e for e in events if e.get("turn_id") == tid and e["event_type"] == "turn.aborted"]
        if old_wait == "parked_question":
            assert turns.get_turn(old["id"])["stop_reason"] == "parked_settled"
    finally:
        await adapter.disconnect()
