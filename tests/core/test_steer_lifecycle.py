"""Admission/refusal, cancel-before-send, and owner-routed unsend contracts."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
from app.core import message_admission, turn_manager
from app.db import turns
from app.network import control
from tests.api.test_chat_ws_v2_turns import _make_session, _Patches

CMD = "client-" + "e" * 32


@pytest.fixture(autouse=True)
def clean_tombstones():
    message_admission._tombstones.clear()
    yield
    message_admission._tombstones.clear()


def handler(sid):
    frames = []
    ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
    h = cwh.ChatWebSocketHandler(ws, "test-device", "admin", None)
    h.current_session_id = sid
    writer = MagicMock(alive=True)
    writer.enqueue.side_effect = lambda text, key: frames.append(json.loads(text)) or True
    h._ensure_writer = lambda: writer
    return h, frames


@pytest.mark.parametrize("gate", ["access", "archived", "mobile", "agent-restriction",
                                  "missing-agent", "empty", "project", "placement",
                                  "attachments", "target", "forward-unreachable", "forward-refused",
                                  "upload-lift", "remote-permission"])
async def test_every_preflight_refusal_is_correlated(mock_db, gate):
    sid = _make_session("refusal")
    h, frames = handler(sid)
    data = {"session_id": sid, "prompt": "recover these words", "profile": "default", "client_message_id": CMD}
    reason = "test refusal: " + gate

    async def refuse(*args, **kwargs):
        await h.send_json({"type": "error", "message": reason})
        return (False, None) if gate == "agent-restriction" else gate == "archived"

    with _Patches(), patch.object(turn_manager, "submit_async", new_callable=AsyncMock) as submit:
        if gate in {"access", "archived", "mobile", "agent-restriction"}:
            name = {"access": "_may_act_on_session", "archived": "_refuse_archived",
                    "mobile": "_authorize_mobile_session", "agent-restriction": "_restrict_profile"}[gate]
            setattr(h, name, refuse)
        elif gate == "missing-agent":
            data["profile"] = "deleted-agent"
        elif gate == "empty":
            data["prompt"] = " "
        elif gate == "attachments":
            data.pop("session_id")
            data["upload_paths"] = ["/missing/image.png"]
        elif gate == "project":
            data["project"] = "not-permitted"
        elif gate == "target":
            data.pop("session_id")
            data["target_instance_id"] = "other"
            h._validate_target_install = refuse
        if gate in {"placement", "forward-unreachable", "forward-refused", "upload-lift", "remote-permission"}:
            decision = SimpleNamespace(kind="none" if gate == "placement" else "runner", runner_id="other")
        else:
            decision = SimpleNamespace(kind="local")
        h._try_auto_adopt = AsyncMock(return_value=False)
        h._network_owner = AsyncMock(return_value="other")
        if gate == "remote-permission":
            h._may_send_elsewhere = refuse
        if gate == "upload-lift":
            data["upload_paths"] = ["/upload.png"]
            h._lift_uploads = AsyncMock(side_effect=ValueError(reason))
        exc = control.RunnerUnreachable("other") if gate == "forward-unreachable" else control.ControlRefused(reason)
        with patch("app.core.placement.resolve_placement", return_value=decision),                 patch("app.api.common.require_origin_access", side_effect=HTTPException(403, reason)),                 patch.object(control, "submit_turn", new=AsyncMock(side_effect=exc)):
            await h.handle_query(data)
        assert not submit.called
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors and all(f.get("client_message_id") == CMD for f in errors)
    assert all(f.get("session_id") == data.get("session_id") for f in errors)


async def test_compact_http_and_legacy_queue_keep_the_refusal_reason(mock_db):
    from app.api.chat_query import HTTPQuerySender
    sid = _make_session("http-refusal")
    data = {"session_id": sid, "profile": "deleted-agent", "prompt": "recover", "client_message_id": CMD}
    with _Patches(), patch("app.api.chat_query.get_api_user_from_request", return_value=None):
        h = HTTPQuerySender(None, sid, None)
        with pytest.raises(HTTPException, match="That agent no longer exists"):
            await h.handle_query(data, compact_first=True)
    h, frames = handler(sid)
    h._may_act_on_session = AsyncMock(return_value=True)
    async def refuse(*args):
        await h.send_json({"type": "error", "message": "Cannot queue here"})
        return False
    h._authorize_mobile_session = refuse
    with _Patches():
        await h.handle_queue_message({"session_id": sid, "content": "recover", "client_message_id": CMD})
    assert frames[-1]["client_message_id"] == CMD
    assert frames[-1]["message"] == "Cannot queue here"


async def test_unknown_id_is_withdrawn_and_late_query_never_enqueues(mock_db):
    sid = _make_session("late-send")
    h, frames = handler(sid)
    with _Patches(), patch.object(cwh.database, "add_session_message") as record:
        await h.handle_remove_queued_message({"session_id": sid, "client_message_id": CMD, "message_id": CMD})
        await h.handle_query({"session_id": sid, "profile": "default", "prompt": "never run", "client_message_id": CMD})
        record.assert_not_called()
    removal = next(f for f in frames if f.get("type") == "queued_message_removal")
    assert removal["verdict"] == "withdrawn" and removal["outcome"] == "cancelled"
    assert removal["client_message_id"] == CMD
    assert turns.get_turn_by_command(sid, CMD) is None
    assert any(f.get("client_message_id") == CMD and "cancelled" in f.get("message", "") for f in frames)
    assert any(f.get("event_type") == "queue_updated" for f in frames)


async def test_fence_covers_forwarded_and_sync_intake_and_expires(mock_db, monkeypatch):
    sid = _make_session("fence")
    assert message_admission.cancel_unknown(sid, CMD) is None
    with pytest.raises(message_admission.CancelledSend):
        await control._submit_forwarded_turn({"session_id": sid, "prompt": "late", "profile_id": "default"}, "peer", CMD)
    with pytest.raises(message_admission.CancelledSend):
        turn_manager.submit(sid, "user", prompt="late", command_id=CMD)
    monkeypatch.setattr(message_admission, "monotonic", lambda: float("inf"))
    assert turn_manager.submit(sid, "user", prompt="retry after expiry", command_id=CMD, no_kick=True)


async def test_correlated_idle_admission_acks_and_defers_history_until_execution(mock_db):
    sid = _make_session("idle-admission")
    h, frames = handler(sid)
    with _Patches():
        result = await h.handle_query({"session_id": sid, "profile": "default", "prompt": "run once", "client_message_id": CMD})
    row = turns.get_turn_by_command(sid, CMD)
    assert result["turn_id"] == row["id"]
    assert row["payload"]["deferred_user_record"] is True
    assert [f for f in frames if f.get("type") == "query_queued"] == [
        {"type": "query_queued", "session_id": sid, "client_message_id": CMD, "turn_id": row["id"], "_seq": 1}]


async def test_cancellation_during_preflight_does_not_write_idle_transcript(mock_db):
    sid = _make_session("race")
    h, frames = handler(sid)
    async def cancels_during_profile_check(requested, fallback):
        message_admission.cancel_unknown(sid, CMD)
        return True, "default"
    h._restrict_profile = cancels_during_profile_check
    with _Patches(), patch.object(cwh.database, "add_session_message") as record:
        await h.handle_query({"session_id": sid, "profile": "default", "prompt": "never run", "client_message_id": CMD})
    record.assert_not_called()
    assert turns.get_turn_by_command(sid, CMD) is None


def test_tombstones_are_bounded_and_session_scoped(mock_db, monkeypatch):
    monkeypatch.setattr(message_admission, "MAX_TOMBSTONES", 2)
    sid = _make_session("bounded")
    for key in ("one", "two", "three"):
        assert message_admission.cancel_unknown(sid, key) is None
    assert len(message_admission._tombstones) == 2
    message_admission.check_cancelled(sid, "one")  # oldest evicted
    message_admission.check_cancelled("different-session", "three")
    with pytest.raises(message_admission.CancelledSend):
        message_admission.check_cancelled(sid, "three")


async def test_mirror_removal_routes_to_owner_and_does_not_fence_locally(mock_db):
    sid = _make_session("mirror")
    h, frames = handler(sid)
    removal = {"type": "queued_message_removal", "session_id": sid, "message_id": "row", "client_message_id": CMD,
               "verdict": "withdrawn", "outcome": "cancelled"}
    snapshot = {"queue_status": "available", "queued_messages": [{"id": "other", "content": "keep me"}], "pending_peer_messages": []}
    with _Patches(), patch.object(control, "owner_of", return_value="executor"),             patch.object(control, "call", new=AsyncMock(side_effect=[{"removal": removal}, snapshot])) as call:
        await h.handle_remove_queued_message({"session_id": sid, "message_id": "row", "client_message_id": CMD})
    assert call.call_args_list[0].args[:2] == ("executor", "queue-remove")
    assert removal.items() <= next(f for f in frames if f.get("type") == "queued_message_removal").items()
    assert any(f.get("data", {}).get("queued_messages") == snapshot["queued_messages"] for f in frames)
    assert not message_admission._tombstones


async def test_unreachable_owner_is_distinct_and_never_fenced(mock_db):
    sid = _make_session("offline")
    h, frames = handler(sid)
    with patch.object(control, "owner_of", return_value="offline"),             patch.object(control, "call", new=AsyncMock(side_effect=control.RunnerUnreachable("offline"))):
        await h.handle_remove_queued_message({"session_id": sid, "client_message_id": CMD, "message_id": CMD})
    assert frames[-1]["verdict"] == "unavailable"
    assert "Can't reach" in frames[-1]["message"]
    assert not message_admission._tombstones


async def test_network_remove_has_queue_view_owner_authority(mock_db):
    sid = _make_session("network-remove")
    with _Patches(), patch.object(control, "_require_owned", return_value={}) as owned:
        response = await control._queue_remove({"session_id": sid, "client_message_id": CMD, "message_id": CMD}, "peer")
    owned.assert_called_once_with(sid)
    assert response["removal"]["verdict"] == "withdrawn"
    with patch.object(control, "_require_owned", side_effect=ValueError("not owner")):
        with pytest.raises(ValueError, match="not owner"):
            await control._queue_remove({"session_id": sid, "client_message_id": CMD}, "peer")
