"""
Tests for the connector permission funnel (app/core/connector_permissions.py)
and the permission_handler hook that drives it.
"""

import asyncio
import time
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.connector_permissions import deliver_permission_prompt
from app.core.connectors.base import OutgoingMessage


def _fake_instance(sent=True):
    inst = MagicMock()
    inst.send_permission_prompt = AsyncMock(return_value=sent)
    inst.send_message = AsyncMock(return_value=sent)
    return inst


# =============================================================================
# deliver_permission_prompt
# =============================================================================


class TestDeliverPermissionPrompt:
    @pytest.mark.asyncio
    async def test_delivers_to_running_connector(self):
        inst = _fake_instance()
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]

        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                session_id="sess-1",
                request_id="req-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                description="run ls",
            )

        mgr.get_running_instance.assert_called_once_with("c1")
        # The chat gets one plain line; approval happens in the owner's app.
        inst.send_permission_prompt.assert_not_awaited()
        inst.send_message.assert_awaited_once()
        outgoing = inst.send_message.await_args.args[0]
        assert isinstance(outgoing, OutgoingMessage)
        assert outgoing.platform_chat_id == "chan-9"
        assert outgoing.text == "Waiting for the owner to approve Bash."
        assert not outgoing.buttons

    @pytest.mark.asyncio
    async def test_skips_connector_not_running(self):
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = None
            # Must not raise.
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        mgr.get_running_instance.assert_called_once_with("c1")

    @pytest.mark.asyncio
    async def test_no_connector_sessions_is_noop(self):
        with patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=[],
        ):
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )

    @pytest.mark.asyncio
    async def test_lookup_failure_is_swallowed(self):
        with patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            side_effect=RuntimeError("db down"),
        ):
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )

    @pytest.mark.asyncio
    async def test_platform_send_failure_does_not_raise(self):
        inst = _fake_instance(sent=False)
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        inst.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_connectors_failure_does_not_stop_the_other(self):
        bad = _fake_instance()
        bad.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        good = _fake_instance()
        cs = [
            {"connector_id": "c1", "platform_chat_id": "chan-1"},
            {"connector_id": "c2", "platform_chat_id": "chan-2"},
        ]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.side_effect = lambda cid: (
                bad if cid == "c1" else good
            )
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        good.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rows_missing_ids_are_skipped(self):
        inst = _fake_instance()
        cs = [
            {"connector_id": None, "platform_chat_id": "chan-1"},
            {"connector_id": "c2", "platform_chat_id": None},
        ]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        inst.send_message.assert_not_awaited()
        mgr.get_running_instance.assert_not_called()


# =============================================================================
# The owner's plain-text answer: the only control a connector chat has
# =============================================================================


def _chat_message(text="yes", user="owner-account", attachments=None, message_id=None, chat="chan-9"):
    from app.core.connectors.base import IncomingMessage

    return IncomingMessage(
        connector_id="c1", platform_chat_id=chat, platform_user_id=user,
        platform_user_name="Someone", text=text, attachments=attachments or [],
        platform_message_id=uuid.uuid4().hex if message_id is None else message_id,
    )


def _open_request(tool="Bash", tool_input=None, kind="permission", request_id="req-1"):
    return {"kind": kind, "payload": {
        "request_id": request_id, "tool_name": tool, "tool_input": tool_input or {"command": "ls"},
    }}


class TestAnswerFromChat:
    @pytest.fixture
    def channel(self):
        """A linked chat with one open request, and the respond seam."""
        from app.core import connector_permissions as bridge  # noqa: F401

        instance = _fake_instance()
        with patch("app.core.connector_service.sender_is_owner", side_effect=lambda _c, user: user == "owner-account"), \
                patch("app.db.database.get_connector_session",
                      return_value={"session_id": "sess-1", "is_active": True}), \
                patch("app.db.interactions.get_pending_interactions", return_value=[_open_request()]) as pending, \
                patch("app.network.ownership.enrolled", return_value=False), \
                patch("app.core.connectors.manager.connector_manager") as manager, \
                patch("app.core.permission_handler.permission_handler.respond",
                      new=AsyncMock(return_value={"resolved": True})) as respond:
            manager.get_running_instance.return_value = instance
            yield {"respond": respond, "pending": pending, "instance": instance}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,decision", [("yes", "allow"), ("No.", "deny"), ("go ahead", "allow")])
    async def test_the_owners_firm_answer_settles_the_request(self, channel, text, decision):
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(text)) is True
        channel["respond"].assert_awaited_once_with("req-1", "sess-1", decision)
        channel["instance"].send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_redelivered_answer_never_approves_the_next_request(self, channel, mock_db):
        """The platform repeats the owner's yes after request A settled and B opened."""
        from app.core.connector_permissions import answer_from_chat

        channel["pending"].side_effect = [[_open_request(request_id="req-a")],
                                          [_open_request(request_id="req-b")]]
        reply = _chat_message(message_id="wamid.1")
        assert await answer_from_chat(reply) is True
        assert await answer_from_chat(reply) is True  # consumed, not an agent turn
        channel["respond"].assert_awaited_once_with("req-a", "sess-1", "allow")

    @pytest.mark.asyncio
    async def test_concurrent_deliveries_approve_at_most_once(self, channel, mock_db):
        from app.core.connector_permissions import answer_from_chat

        reply = _chat_message(message_id="wamid.2")
        results = await asyncio.gather(*(answer_from_chat(reply) for _ in range(8)))
        assert results == [True] * 8
        channel["respond"].assert_awaited_once_with("req-1", "sess-1", "allow")

    @pytest.mark.asyncio
    async def test_a_redelivered_answer_stays_consumed_with_nothing_open(self, channel, mock_db):
        from app.core.connector_permissions import answer_from_chat

        reply = _chat_message(message_id="wamid.3")
        assert await answer_from_chat(reply) is True
        channel["pending"].return_value = []
        assert await answer_from_chat(reply) is True
        channel["respond"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_redelivering_an_ordinary_yes_never_answers_a_later_request(self, channel, mock_db):
        """Delivered when nothing was open, so it was a message; a retry is not an answer."""
        from app.core.connector_permissions import answer_from_chat

        channel["pending"].return_value = []
        reply = _chat_message(message_id="wamid.4")
        assert await answer_from_chat(reply) is False
        channel["pending"].return_value = [_open_request()]
        assert await answer_from_chat(reply) is True
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message_id", ["", "   "])
    async def test_a_message_with_no_platform_id_cannot_approve(self, channel, mock_db, message_id):
        """Nothing identifies a repeat of it, so it stays an ordinary message."""
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(message_id=message_id)) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_same_id_in_another_chat_is_another_message(self, channel, mock_db):
        """Telegram numbers messages per chat."""
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(message_id="7", chat="chan-1")) is True
        assert await answer_from_chat(_chat_message(message_id="7", chat="chan-2")) is True
        assert channel["respond"].await_count == 2

    @pytest.mark.asyncio
    async def test_a_consumed_redelivery_starts_no_agent_turn(self, channel, mock_db):
        from app.core.connectors.manager import ConnectorManager

        manager = ConnectorManager.__new__(ConnectorManager)
        manager._message_locks = MagicMock()
        reply = _chat_message(message_id="wamid.5")
        await ConnectorManager.handle_incoming_message(manager, reply)
        await ConnectorManager.handle_incoming_message(manager, reply)
        manager._message_locks.get.assert_not_called()
        channel["respond"].assert_awaited_once()

    def test_claims_keep_no_platform_ids_and_stay_on_this_worker(self, mock_db):
        from app.db import connectors as connectors_db
        from app.db.base import get_db
        from app.network.policy import is_shared_setting_key

        assert connectors_db.claim_answer_message("c1", "+15550100", "wamid.6") is True
        assert connectors_db.claim_answer_message("c1", "+15550100", "wamid.6") is False
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s",
                           (connectors_db.ANSWER_CLAIM_PREFIX + "%",))
            rows = cursor.fetchall()
        assert len(rows) == 1
        assert "15550100" not in rows[0]["key"] and "wamid" not in rows[0]["key"]
        assert not is_shared_setting_key(rows[0]["key"])

    def test_old_claims_are_pruned(self, mock_db):
        from app.db import connectors as connectors_db

        assert connectors_db.claim_answer_message("c1", "chat", "old") is True
        later = time.time() + connectors_db.ANSWER_CLAIM_TTL_SECONDS + 60
        with patch("app.db.connectors.time.time", return_value=later):
            assert connectors_db.claim_answer_message("c1", "chat", "new") is True
        assert connectors_db.claim_answer_message("c1", "chat", "old") is True

    @pytest.mark.asyncio
    async def test_someone_else_saying_yes_is_an_ordinary_message(self, channel):
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(user="someone-else")) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["maybe", "yes if it's safe", "what will it do?", ""])
    async def test_anything_not_firm_is_an_ordinary_message(self, channel, text):
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(text)) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_yes_with_an_attachment_is_an_ordinary_message(self, channel):
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message(attachments=[{"name": "a.png"}])) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_safety_check_cannot_be_answered_in_text(self, channel):
        from app.core.connector_permissions import answer_from_chat

        channel["pending"].return_value = [_open_request(tool="ModelGuardrail", tool_input={})]
        assert await answer_from_chat(_chat_message()) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_chat_with_nothing_open_is_an_ordinary_message(self, channel):
        from app.core.connector_permissions import answer_from_chat

        channel["pending"].return_value = [_open_request(kind="question")]
        assert await answer_from_chat(_chat_message()) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_request_already_settled_leaves_the_message_ordinary(self, channel):
        from app.core.connector_permissions import answer_from_chat

        channel["respond"].return_value = {"resolved": False}
        assert await answer_from_chat(_chat_message()) is False
        channel["instance"].send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failure_leaves_the_request_open_and_never_raises(self, channel):
        from app.core.connector_permissions import answer_from_chat

        channel["respond"].side_effect = RuntimeError("database gone")
        assert await answer_from_chat(_chat_message()) is False

    @pytest.mark.asyncio
    async def test_the_answer_does_not_wait_behind_the_blocked_turn(self):
        """The turn holding the chat's lock is the one waiting for this answer."""
        from app.core.connectors.manager import ConnectorManager

        manager = ConnectorManager.__new__(ConnectorManager)
        manager._message_locks = MagicMock()
        with patch("app.core.connector_permissions.answer_from_chat", new=AsyncMock(return_value=True)):
            await ConnectorManager.handle_incoming_message(manager, _chat_message())
        manager._message_locks.get.assert_not_called()

    def test_only_the_owners_own_chat_is_invited_to_reply(self):
        from app.core.connector_permissions import _prompt_text, waiting_for_owner_text

        cs = {"connector_id": "c1", "platform_user_id": "owner-account"}
        with patch("app.core.connector_service.sender_is_owner", side_effect=lambda _c, user: user == "owner-account"):
            assert "Reply yes or no" in _prompt_text(cs, "Bash", {"command": "ls"})
            assert _prompt_text({**cs, "platform_user_id": "x"}, "Bash", {"command": "ls"}) == waiting_for_owner_text("Bash")
            assert _prompt_text(cs, "ModelGuardrail", {}) == waiting_for_owner_text("ModelGuardrail")
