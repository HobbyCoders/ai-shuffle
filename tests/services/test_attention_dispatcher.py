"""Needs you only: real DB producers with a fake FCM transport, never Google."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.db import database as db, interactions, session_events
from app.db.base import get_db
from app.services import attention_dispatcher as attention
from app.services.push_notification import NotificationResult


@pytest.fixture
def chat(mock_db, monkeypatch):
    db.create_session("push-chat", "default", title="Review")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", ("worker-a", "push-chat"))
    sender = AsyncMock()
    sender.send_to_all_devices.return_value = [NotificationResult(True)]
    dispatcher = attention.AttentionDispatcher(sender)
    monkeypatch.setattr(dispatcher, "worker_id", lambda: "worker-a")
    monkeypatch.setattr(dispatcher, "private_needs", lambda sid: set())
    return dispatcher, sender


def commands(sender):
    return [call.kwargs["data"] for call in sender.send_to_all_devices.call_args_list]


@pytest.mark.asyncio
async def test_zero_nonzero_extra_clear_and_new_episode(chat):
    dispatcher, sender = chat
    first = interactions.create_interaction("push-chat", "question", {"question": "private text"}, 120)
    await dispatcher.reconcile("push-chat")
    second = interactions.create_interaction("push-chat", "permission", {"owner_confirm": True}, None)
    await dispatcher.reconcile("push-chat")
    assert [c["action"] for c in commands(sender)] == ["alert", "update"]
    assert commands(sender)[0]["episode"] == commands(sender)[1]["episode"]
    assert "private text" not in json.dumps(commands(sender))
    assert commands(sender)[0]["body"] == "A chat needs you: Review"
    interactions.answer_interaction(first["id"], {})
    await dispatcher.reconcile("push-chat")
    interactions.cancel_interaction(second["id"])
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1]["action"] == "clear"
    interactions.create_interaction("push-chat", "question", {}, 120)
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1]["action"] == "alert"
    assert commands(sender)[-1]["episode"] != commands(sender)[0]["episode"]


@pytest.mark.asyncio
async def test_restart_reconnect_retry_and_mirror(chat, monkeypatch):
    dispatcher, sender = chat
    interactions.create_interaction("push-chat", "permission", {}, 120)
    sender.send_to_all_devices.return_value = [NotificationResult(False)]
    await dispatcher.reconcile("push-chat")
    first = commands(sender)[0]
    sender.send_to_all_devices.return_value = [NotificationResult(True)]
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1] == first  # Same durable version/episode on retry.
    await dispatcher.reconcile("push-chat")
    replacement = attention.AttentionDispatcher(sender)
    monkeypatch.setattr(replacement, "worker_id", lambda: "worker-a")
    monkeypatch.setattr(replacement, "private_needs", lambda sid: set())
    await replacement.reconcile("push-chat")
    monkeypatch.setattr(replacement, "worker_id", lambda: "mirror")
    result = await replacement.reconcile("push-chat", explicit_id="mirror-call")
    assert not result["sent"]
    assert len(commands(sender)) == 2


@pytest.mark.asyncio
async def test_explicit_tool_id_dedupe_survives_reply_and_restart(chat):
    dispatcher, sender = chat
    await dispatcher.reconcile("push-chat", explicit_id="tool-1")
    await dispatcher.reconcile("push-chat", explicit_id="tool-1")
    await dispatcher.reconcile("push-chat", explicit_id="tool-2")
    assert [c["action"] for c in commands(sender)] == ["alert", "update"]
    session_events.append_events("push-chat", [{"type": "message.user", "payload": {"content": "Here is my answer"}}])
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1]["action"] == "clear"
    await dispatcher.reconcile("push-chat", explicit_id="tool-1")
    assert len(commands(sender)) == 3
    await dispatcher.reconcile("push-chat", explicit_id="tool-3")
    assert commands(sender)[-1]["action"] == "alert"


@pytest.mark.asyncio
async def test_trailing_prose_producing_path_and_owner_answer(chat):
    dispatcher, sender = chat
    session_events.append_events("push-chat", [
        {"type": "turn.started", "payload": {}},
        {"type": "assistant.text_delta", "payload": {"text": "Will you approve the plan?"}},
    ])
    await dispatcher.reconcile("push-chat")
    sender.send_to_all_devices.assert_not_awaited()  # Not streaming punctuation.
    session_events.append_events("push-chat", [{"type": "turn.completed", "payload": {}}])
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1]["action"] == "alert"
    session_events.append_events("push-chat", [{"type": "message.user", "payload": {"content": "Yes"}}])
    await dispatcher.reconcile("push-chat")
    assert commands(sender)[-1]["action"] == "clear"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["turn.completed", "turn.parked", "turn.aborted", "error", "system.note"])
async def test_lifecycle_and_settle_never_alert(chat, kind):
    dispatcher, sender = chat
    session_events.append_events("push-chat", [{"type": kind, "payload": {"subtype": "session_settled"}}])
    await dispatcher.reconcile("push-chat")
    db.set_session_active("push-chat", False)
    await dispatcher.reconcile("push-chat")
    sender.send_to_all_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_and_expiry_only_clear(chat):
    dispatcher, sender = chat
    interactions.create_interaction("push-chat", "permission", {}, 120)
    await dispatcher.reconcile("push-chat")
    db.set_session_active("push-chat", False)
    session_events.append_events("push-chat", [{"type": "system.note", "payload": {"subtype": "session_settled"}}])
    await dispatcher.reconcile("push-chat")
    assert [c["action"] for c in commands(sender)] == ["alert", "clear"]
    await dispatcher.reconcile("push-chat")
    assert len(commands(sender)) == 2


@pytest.mark.asyncio
async def test_expired_interaction_and_api_user_do_not_notify(chat):
    dispatcher, sender = chat
    interactions.create_interaction("push-chat", "question", {}, -1)
    await dispatcher.reconcile("push-chat")
    sender.send_to_all_devices.assert_not_awaited()
    db.create_api_user("someone-else", "Someone else", "different-hash")
    with get_db() as conn:
        # A foreign principal must never reach the owner's phone.
        conn.cursor().execute("UPDATE sessions SET api_user_id=%s WHERE id=%s", ("someone-else", "push-chat"))
    await dispatcher.reconcile("push-chat", explicit_id="tool-foreign")
    sender.send_to_all_devices.assert_not_awaited()


def test_replayed_need_identity_after_zero_is_silent():
    state, _ = attention.reduce_attention({}, {"question:one"}, [None, None])
    state, _ = attention.reduce_attention(state, set(), [None, None])
    state, changed = attention.reduce_attention(state, {"question:one"}, [None, None])
    assert changed and state["action"] == "update"


@pytest.mark.asyncio
async def test_private_access_waits_and_approvals_use_executing_session(chat, monkeypatch):
    from app.core import chat_access
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("worker-a", "runner", None)))
    dispatcher, sender = chat
    monkeypatch.setattr(dispatcher, "private_needs", attention.AttentionDispatcher.private_needs)
    # Service preparation returns its real pending card without making any request
    # to the service. The purpose must never appear in phone commands.
    from fluid.tools.registry import current_tool_use_id
    token = current_tool_use_id.set("tool-private-1")
    await broker.request("push-chat", "service", "private access purpose")
    try:
        assert broker.requests
        await dispatcher.reconcile("push-chat")
        broker.dismiss_pending("push-chat")
        await dispatcher.reconcile("push-chat")
        assert [c["action"] for c in commands(sender)] == ["alert", "clear"]
        assert "private access purpose" not in json.dumps(commands(sender))
        # The in-memory card can be recreated with a different ID after restart.
        # Its canonical tool use is still the same need and cannot re-alert.
        await broker.request("push-chat", "service", "private access purpose")
        await dispatcher.reconcile("push-chat")
        assert commands(sender)[-1]["action"] == "update"
    finally:
        current_tool_use_id.reset(token)
        broker.dismiss_pending("push-chat")
        await asyncio.gather(*list(broker._notifications))


@pytest.mark.asyncio
async def test_no_credentials_is_not_a_future_alert_backlog(chat):
    dispatcher, sender = chat
    sender.send_to_all_devices.return_value = [NotificationResult(False, simulated=True)]
    interactions.create_interaction("push-chat", "question", {}, 120)
    assert not (await dispatcher.reconcile("push-chat"))["sent"]
    sender.send_to_all_devices.return_value = [NotificationResult(True)]
    await dispatcher.reconcile("push-chat")
    assert len(commands(sender)) == 1


@pytest.mark.asyncio
async def test_private_action_producer_and_denial_clear(chat, monkeypatch):
    from app.core import chat_access
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    owner = ("worker-a", "runner", None)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", AsyncMock(return_value=False))
    dispatcher, sender = chat
    monkeypatch.setattr(dispatcher, "private_needs", attention.AttentionDispatcher.private_needs)
    grant = chat_access.Grant("grant", "push-chat", owner, "Resource", "service", "redacted", attention.time.time() + 120)
    task = asyncio.create_task(broker.approve(grant, "http", {"private": "not transmitted"}))
    try:
        async with asyncio.timeout(2):
            while not broker.actions:
                await asyncio.sleep(0.01)
        await dispatcher.reconcile("push-chat")
        next(iter(broker.actions.values())).decision.set_result(False)
        with pytest.raises(chat_access.AccessDenied):
            await task
        await dispatcher.reconcile("push-chat")
        assert [c["action"] for c in commands(sender)] == ["alert", "clear"]
        assert "not transmitted" not in json.dumps(commands(sender))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*list(broker._notifications))


def test_cli_canonical_tool_use_id_is_read_without_private_input(chat):
    session_events.append_events("push-chat", [{"type": "tool.use", "payload": {
        "id": "cli-notify-1", "name": "mcp__ai_shuffle__NotifyDevices", "input": {"body": "private"}}}])
    assert attention.latest_notify_tool_id("push-chat") == "cli-notify-1"
    session_events.append_events("push-chat", [{"type": "tool.use", "payload": {"id": "other", "name": "Bash"}}])
    assert attention.latest_notify_tool_id("push-chat") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_first", [True, False])
async def test_explicit_and_automatic_need_share_one_clearable_episode(chat, explicit_first):
    dispatcher, sender = chat
    if explicit_first:
        await dispatcher.reconcile("push-chat", explicit_id="one-call")
    question = interactions.create_interaction("push-chat", "question", {}, 120)
    await dispatcher.reconcile("push-chat", explicit_id=None if explicit_first else "one-call")
    interactions.answer_interaction(question["id"], {})
    await dispatcher.reconcile("push-chat")
    assert [c["action"] for c in commands(sender)].count("alert") == 1
    assert commands(sender)[-1]["action"] == "clear"
