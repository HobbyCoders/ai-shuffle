"""Snooze on a worker that is not networked: API, lists, wake loop and early wakes."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import sessions as api
from app.core import session_snooze
from app.db import database


def _new(active=True, parent=None):
    sid = str(uuid.uuid4())
    database.create_session(session_id=sid, profile_id="default", is_active=active,
                            parent_session_id=parent)
    return sid


async def _patch(sid, active, snoozed_until=None):
    return await api.set_session_active(
        Request({"type": "http", "headers": []}), sid,
        api.SessionActiveUpdate(is_active=active, snoozed_until=snoozed_until), token="admin",
    )


async def _woke(sid):
    """Wait for the scheduled early wake (prompt paths do not await it)."""
    import asyncio
    for _ in range(300):
        row = database.get_session(sid)
        if row["is_active"] in (True, 1):
            return row
        await asyncio.sleep(0.01)
    return database.get_session(sid)


def _soon(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


@pytest.fixture
def broadcasts(monkeypatch):
    from app.core.sync_engine import sync_engine
    sent = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", sent)
    return sent


def _membership(name):
    return {s["id"] for s in database.get_sessions_cursor(limit=100, membership=name)[0]}


async def test_snooze_moves_chat_to_its_own_list_and_unsnooze_clears(mock_db, broadcasts):
    sid = _new()
    when = _soon()
    row = await _patch(sid, False, when)
    assert row["is_active"] in (False, 0)
    assert session_snooze.parse(row["snoozed_until"]) == when.replace(microsecond=0)
    assert broadcasts.call_args.kwargs["updates"]["snoozed_until"] == session_snooze.canonical(when)
    assert sid in _membership("snoozed")
    assert sid not in _membership("settled") and sid not in _membership("active")
    assert database.count_sessions(membership="snoozed") == 1
    assert database.count_sessions(membership="settled") == 0

    # Unsnooze is a plain activate; settle is a plain settle. Both clear it.
    row = await _patch(sid, True)
    assert row["snoozed_until"] is None and row["is_active"] in (True, 1)
    await _patch(sid, False, _soon())
    row = await _patch(sid, False)
    assert row["snoozed_until"] is None
    assert sid in _membership("settled") and sid not in _membership("snoozed")


@pytest.mark.parametrize("body", [
    {"active": False, "snoozed_until": datetime.now(timezone.utc) - timedelta(minutes=1)},
    {"active": False, "snoozed_until": datetime(2030, 1, 1, 9, 0)},  # no timezone
    {"active": True, "snoozed_until": datetime.now(timezone.utc) + timedelta(hours=1)},
])
async def test_snooze_rejects_past_naive_or_active(mock_db, body):
    sid = _new()
    with pytest.raises(HTTPException) as err:
        await _patch(sid, body["active"], body["snoozed_until"])
    assert err.value.status_code == 422
    assert database.get_session(sid)["snoozed_until"] is None


async def test_wake_loop_wakes_due_and_leaves_future(mock_db, broadcasts):
    due, later = _new(), _new()
    await _patch(due, False, _soon(1))
    await _patch(later, False, _soon(60))
    assert await session_snooze.wake_due_once(now=_soon(2)) == 1
    assert database.get_session(due)["is_active"] in (True, 1)
    assert database.get_session(due)["snoozed_until"] is None
    assert database.get_session(later)["snoozed_until"] is not None
    assert broadcasts.call_args.kwargs["updates"] == {"is_active": True, "status": "active", "snoozed_until": None}
    # Idempotent: nothing is due any more.
    assert await session_snooze.wake_due_once(now=_soon(2)) == 0


async def test_wake_never_undoes_a_newer_change(mock_db, broadcasts):
    sid = _new()
    first = _soon(1)
    await _patch(sid, False, first)
    seen = session_snooze.canonical(first)
    # The owner re-snoozes after the loop read the due row.
    await _patch(sid, False, _soon(90))
    assert not await session_snooze.wake(sid, expected=seen, reason="test")
    assert database.get_session(sid)["snoozed_until"] is not None
    # ...or settles it: nothing to wake.
    await _patch(sid, False)
    assert not await session_snooze.wake(sid, reason="test")
    assert database.get_session(sid)["is_active"] in (False, 0)


async def test_needs_owner_wakes_snoozed_chat_and_its_parent(mock_db, broadcasts):
    parent = _new()
    child = _new(active=False, parent=parent)
    await _patch(parent, False, _soon())
    assert await session_snooze.wake_for_attention(child)
    assert database.get_session(parent)["is_active"] in (True, 1)
    # A chat that is not snoozed is left alone, including a settled one.
    plain = _new()
    await _patch(plain, False)
    assert not await session_snooze.wake_for_attention(plain)
    assert database.get_session(plain)["is_active"] in (False, 0)


async def test_permission_prompt_wakes_snoozed_chat(mock_db, broadcasts):
    import asyncio
    from unittest.mock import patch
    from app.core.permission_handler import PermissionHandler
    sid = _new()
    await _patch(sid, False, _soon())
    handler = PermissionHandler()
    sent = AsyncMock()
    with patch("app.db.database.get_permission_rules", return_value=[]):
        task = asyncio.create_task(handler.request_permission(
            request_id="req-1", session_id=sid, profile_id="default", tool_name="Bash",
            tool_input={"command": "rm -rf build"}, broadcast_func=sent))
        for _ in range(500):
            if sent.called:
                break
            await asyncio.sleep(0.01)
        assert sent.called
        row = await _woke(sid)
        assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
        await handler.cancel_request("req-1", sid)
        await task


async def test_question_prompt_wakes_snoozed_chat(mock_db, broadcasts, monkeypatch):
    from app.core.user_question_handler import UserQuestionHandler
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.user_question_handler.UserQuestionHandler._retire_native_question", AsyncMock())
    sid = _new()
    await _patch(sid, False, _soon())
    result = await UserQuestionHandler().post_question(
        request_id="q1", tool_use_id="tu", session_id=sid,
        questions=[{"question": "Ship it?"}], broadcast_func=AsyncMock())
    assert result["status"] == "posted"
    row = await _woke(sid)
    assert row["is_active"] in (True, 1) and row["snoozed_until"] is None


def test_owner_send_reactivation_clears_snooze(mock_db):
    from app.core import turn_manager
    sid = _new()
    database.set_session_active(sid, False, None, session_snooze.canonical(_soon()))
    reactivated = {}
    turn_manager._reactivate_for_send(sid, "user", {}, reactivated)
    row = database.get_session(sid)
    assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
    assert reactivated  # announced to sidebars
    # Background work does not wake it.
    database.set_session_active(sid, False, None, session_snooze.canonical(_soon()))
    turn_manager._reactivate_for_send(sid, "automation", {}, {})
    assert database.get_session(sid)["snoozed_until"] is not None


def test_canonical_and_validate():
    assert session_snooze.canonical("2030-01-01T09:00:00.123456-05:00") == "2030-01-01T14:00:00+00:00"
    assert session_snooze.canonical(None) is None
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        session_snooze.validate_future(now, now=now)
    assert session_snooze.validate_future(now + timedelta(seconds=5), now=now) == "2030-01-01T00:00:05+00:00"


async def test_private_access_request_wakes_snoozed_chat(mock_db, broadcasts, monkeypatch):
    import asyncio
    from app.core import chat_access, chat_vault
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("local", "worker")))
    monkeypatch.setattr(chat_vault, "catalog", lambda owner, **kwargs: [])
    sid = _new()
    await _patch(sid, False, _soon())
    broker = chat_access.Broker()
    task = asyncio.create_task(broker.request(sid, "machine", "Need server access"))
    row = await _woke(sid)
    assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not broker.requests


def test_voice_request_ends_a_snooze_but_keeps_a_settled_chat_settled(mock_db):
    from app.core import turn_manager
    voice = {turn_manager.VOICE_DELEGATION: True}
    sid = _new()
    database.set_session_active(sid, False, None, session_snooze.canonical(_soon()))
    reactivated = {}
    turn_manager._reactivate_for_send(sid, "user", voice, reactivated)
    row = database.get_session(sid)
    assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
    assert reactivated  # announced to sidebars
    # A call on a plain settled chat leaves it settled, as before.
    plain = _new()
    database.set_session_active(plain, False)
    reactivated = {}
    turn_manager._reactivate_for_send(plain, "user", voice, reactivated)
    assert database.get_session(plain)["is_active"] in (False, 0) and reactivated == {}
    # Background work with the flag still does not wake it.
    database.set_session_active(sid, False, None, session_snooze.canonical(_soon()))
    turn_manager._reactivate_for_send(sid, "automation", voice, {})
    assert database.get_session(sid)["snoozed_until"] is not None


async def test_voice_submit_wakes_a_snoozed_chat(mock_db, monkeypatch):
    """The real intake: a delegated spoken turn queues AND ends the snooze."""
    from app.core import turn_manager
    from app.db import turns
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    announced = AsyncMock()
    monkeypatch.setattr(turn_manager, "_announce_reactivated", announced)
    sid = _new()
    database.set_session_active(sid, False, None, session_snooze.canonical(_soon()))
    turn_id = await turn_manager.submit_async(
        sid, "user", prompt="Make the icons bigger.", profile_id="default",
        payload={turn_manager.VOICE_DELEGATION: True})
    assert turns.get_turn(turn_id)["state"] == "queued"
    row = database.get_session(sid)
    assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
    announced.assert_awaited_once()


def _drain_wakes():
    import asyncio
    return asyncio.gather(*session_snooze._pending_wakes)


@pytest.mark.parametrize("how", ["owner", "away", "dismissed"])
async def test_owner_answer_to_a_retained_question_card_ends_a_later_snooze(mock_db, broadcasts, monkeypatch, how):
    """The direct-enqueue answer path: only the owner's own answer wakes the chat."""
    from app.core.user_question_handler import UserQuestionHandler
    from app.db import turns
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.user_question_handler.UserQuestionHandler._retire_native_question", AsyncMock())
    monkeypatch.setattr("app.core.session_runtime.try_steer_pending", AsyncMock())
    monkeypatch.setattr("app.core.session_runtime._broadcast_queue_after_start", AsyncMock())
    handler = UserQuestionHandler()
    sid = _new()
    await handler.post_question(request_id="q1", tool_use_id="tu", session_id=sid,
                                questions=[{"question": "Ship it?"}], broadcast_func=AsyncMock())
    await _drain_wakes()
    # The card stays on screen; the owner snoozes the chat after it appeared.
    await _patch(sid, False, _soon())
    answered = await handler.respond("q1", sid, {"Ship it?": "Yes"},
                                     dismissed=how == "dismissed", human_input=how != "away")
    assert answered
    row = database.get_session(sid)
    if how == "owner":
        assert row["is_active"] in (True, 1) and row["snoozed_until"] is None
        assert "Ship it?\nYes" in turns.get_queued_turns(sid)[0]["prompt"]
    else:
        assert row["is_active"] in (False, 0) and row["snoozed_until"] is not None


async def test_archived_rows_are_never_woken(mock_db, broadcasts):
    from app.db.base import get_db
    sid = _new()
    await _patch(sid, False, _soon(1))
    # A legacy archived status that still carries a wake time.
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET status='archived' WHERE id=%s", (sid,))
    assert session_snooze.due_snoozes(_soon(10)) == []
    assert await session_snooze.wake_due_once(now=_soon(10)) == 0
    assert not await session_snooze.wake_for_attention(sid)
    assert database.get_session(sid)["is_active"] in (False, 0)


async def test_legacy_archive_form_ends_the_snooze(mock_db, broadcasts):
    sid = _new()
    await _patch(sid, False, _soon(1))
    row = database.update_session(session_id=sid, status="archived")
    assert row["status"] == "active" and row["is_active"] in (False, 0)
    assert row["snoozed_until"] is None
    assert session_snooze.due_snoozes(_soon(10)) == []
