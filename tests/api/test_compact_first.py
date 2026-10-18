"""Compact first uses the normal send intake and keeps the follow-up durable."""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.core import limit_resume, turn_manager
from app.core.session_runtime import SessionRuntime
from app.db import database, sessions, turns
from app.network import session_active


@pytest.fixture
def compact_chat(mock_db, monkeypatch):
    sessions.create_session("compact-chat", "default", title="Old chat", is_active=True)
    monkeypatch.setattr(limit_resume, "session_leg", lambda row: "openai_responses")
    # Leave turns in the durable queue; drive their state transitions explicitly.
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    app = FastAPI()
    app.include_router(router)
    def owner_auth(request: Request):
        request.state.auth_token = "local-owner"
        return "local-owner"

    app.dependency_overrides[require_auth] = owner_auth
    with TestClient(app) as client:
        yield client


def test_compact_preserves_owner_bound_provenance(compact_chat):
    from app.core import mobile_tool_identity as identity
    from app.core.config import settings
    from app.db import mobile_tool_identity as store
    principal = identity.principal("admin", "install", "local-transport:" + settings.effective_instance_id)
    store.bind("compact-chat", principal, new=True)
    result = send(compact_chat, {"then_send": {"prompt": "continue my chat"}})
    assert result.status_code == 200, result.text
    assert store.read("compact-chat")["owner"] == "install-admin"


def send(client, payload=None):
    return client.post("/api/v1/sessions/compact-chat/compact", **({"json": payload} if payload else {}))


@pytest.mark.parametrize("unsettle", [True, False])
async def test_settle_wait_then_compact_starts_fresh(compact_chat, unsettle):
    from app.core.user_question_handler import user_question_handler
    old = turns.enqueue_turn("compact-chat", "user", prompt="old")
    turns.start_turn(old["id"])
    turns.park_turn(old["id"], [])
    await user_question_handler.post_question(
        request_id="q", tool_use_id="tu", session_id="compact-chat",
        questions=[{"question": "Proceed?"}], broadcast_func=AsyncMock(),
    )
    await session_active.set_shared("compact-chat", False)
    assert turns.get_turn(old["id"])["stop_reason"] == "parked_settled"
    if unsettle:
        await session_active.set_shared("compact-chat", True)
    result = send(compact_chat)
    assert result.status_code == 200, result.text
    assert sessions.get_session("compact-chat")["is_active"]
    compact = turns.get_turn(result.json()["turn_id"])
    assert compact["prompt"] == "/compact"
    assert compact["state"] == "queued"
    assert turns.start_turn(compact["id"])
    assert turns.get_turn(old["id"])["stop_reason"] == "parked_settled"


@pytest.mark.parametrize("busy", [False, True])
def test_compact_followup_is_exactly_next_and_keeps_options(compact_chat, busy):
    if busy:
        old = turns.enqueue_turn("compact-chat", "user", prompt="already working")
        turns.start_turn(old["id"])
    payload = {"type": "query", "prompt": "Read /uploads/photo.png", "profile": "default",
               "session_id": "cannot-change-route-id", "upload_paths": ["/uploads/photo.png"],
               "client_message_id": "client-" + "a" * 32,
               "overrides": {"model": "test-model", "effort": "high", "fast": False,
                             "permission_mode": "default"}}
    result = send(compact_chat, {"then_send": payload})
    assert result.status_code == 200, result.text
    rows = turns.get_queued_turns("compact-chat")
    assert [r["prompt"] for r in rows] == ["/compact", payload["prompt"]]
    compact, followup = rows
    assert result.json() == {"session_id": "compact-chat", "turn_id": compact["id"],
                             "queued_turn_id": followup["id"]}
    assert followup["overrides"]["effort"] == "high"
    assert followup["overrides"]["_permission_explicit"] is True
    assert followup["command_id"] == payload["client_message_id"]
    assert followup["payload"]["deferred_user_record"] is True
    assert followup["payload"]["compact_before_turn_id"] == compact["id"]
    assert not database.get_session_messages("compact-chat")
    # Startup sweeps and later sends may attempt steering; neither may pull the
    # message forward into /compact or into the preceding live turn.
    runtime = SessionRuntime(compact)
    assert runtime._steer_refusal(followup) == "a compact-first follow-up runs as its own turn"


@pytest.mark.parametrize("outcome", ["completed", "aborted"])
def test_followup_survives_compaction_failure(compact_chat, outcome):
    result = send(compact_chat, {"then_send": {"prompt": "my message"}})
    assert result.status_code == 200, result.text
    compact_id, followup_id = result.json()["turn_id"], result.json()["queued_turn_id"]
    assert turns.start_turn(compact_id)
    turns.settle_turn(compact_id, outcome, **({"error": "Compaction failed"} if outcome == "aborted" else {}))
    assert turns.pop_ready("compact-chat")["id"] == followup_id
    assert turns.start_turn(followup_id)
    assert turns.get_turn(followup_id)["prompt"] == "my message"
    assert turns.get_turn(compact_id)["state"] == outcome


@pytest.mark.parametrize("payload", [{"then_send": []}, {"then_send": {"prompt": 1}},
                                      {"then_send": {"prompt": " "}},
                                      {"then_send": {"prompt": "hello", "profile": "missing"}},
                                      {"then_send": {"prompt": "hello", "client_message_id": "bad"}}])
def test_invalid_send_queues_neither_turn(compact_chat, payload):
    result = send(compact_chat, payload)
    assert result.status_code in (400, 422), result.text
    assert turns.get_queued_turns("compact-chat") == []

def test_pair_rolls_back_if_followup_admission_fails(compact_chat, monkeypatch):
    original = turns._session_bound_overrides
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("admission failed")
        return original(*args)

    monkeypatch.setattr(turns, "_session_bound_overrides", fail_second)
    with pytest.raises(ValueError, match="admission failed"):
        send(compact_chat, {"then_send": {"prompt": "keep my draft"}})
    assert turns.get_queued_turns("compact-chat") == []


@pytest.mark.parametrize("lift_fails", [False, True])
def test_remote_attachments_use_normal_lift_before_pair_admission(compact_chat, monkeypatch, lift_fails):
    from types import SimpleNamespace
    from app.api.chat_ws_handler import ChatWebSocketHandler
    from app.core import placement

    monkeypatch.setattr(placement, "resolve_placement", lambda row: SimpleNamespace(kind="runner", runner_id="other"))
    monkeypatch.setattr(ChatWebSocketHandler, "_try_auto_adopt", AsyncMock(return_value=False))
    monkeypatch.setattr(ChatWebSocketHandler, "_network_owner", AsyncMock(return_value=None))
    lift = AsyncMock(side_effect=ValueError("file missing")) if lift_fails else AsyncMock(return_value=["blob-1"])
    monkeypatch.setattr(ChatWebSocketHandler, "_lift_uploads", lift)
    result = send(compact_chat, {"then_send": {"prompt": "inspect file", "upload_paths": ["/uploads/a.png"]}})
    lift.assert_awaited_once_with(["/uploads/a.png"], None, "compact-chat")
    rows = turns.get_queued_turns("compact-chat")
    if lift_fails:
        assert result.status_code == 400
        assert rows == []
    else:
        assert result.status_code == 200, result.text
        assert len(rows) == 2
        assert "upload_blob_ids" not in rows[0]["payload"]
        assert rows[1]["payload"]["upload_blob_ids"] == ["blob-1"]


async def test_native_failed_compact_card_keeps_followup_and_drains(compact_chat, monkeypatch):
    from app.core import turn_scheduler
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events
    from tests.core.test_native_compact_adapter import _adapter, _cards, _collect

    result = send(compact_chat, {"then_send": {"prompt": "continue after failure"}}).json()
    compact_id, followup_id = result["turn_id"], result["queued_turn_id"]
    turns.start_turn(compact_id)
    adapter, _ = await _adapter(monkeypatch)
    try:
        adapter.set_turn_source("user")
        await adapter.send_query(turns.get_turn(compact_id)["prompt"])
        events = await _collect(adapter)
        [card] = _cards(events, "compact_failed")
        recorder = await TurnRecorder.maybe_create("compact-chat")
        await recorder.on_event({"type": "system", "subtype": card.subtype, "data": card.data})
        await recorder.close()
        turns.settle_turn(compact_id, "completed")
        # The ordinary settle hook, not a special success callback, runs the
        # queue. A compact_failed card does not discard or strand the message.
        from unittest.mock import Mock
        kick_sync = Mock()
        monkeypatch.setattr(turn_scheduler, "kick", kick_sync)
        await turn_scheduler.on_turn_settled("compact-chat", compact_id, "completed")
        kick_sync.assert_called_once_with("compact-chat")
        assert turns.pop_ready("compact-chat")["id"] == followup_id
        assert any(r["event_type"] == "system.note" and r["payload"].get("subtype") == "compact_failed"
                   for r in session_events.get_events("compact-chat"))
    finally:
        await adapter.disconnect()

