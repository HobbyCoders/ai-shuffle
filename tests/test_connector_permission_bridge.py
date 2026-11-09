"""
Tests for the connector permission funnel (app/core/connector_permissions.py)
and the permission_handler hook that drives it.
"""

import asyncio
import hashlib
import json
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
    inst.config = {}
    return inst


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
# The owner's coded reply: the only control a connector chat has
# =============================================================================


def _chat_message(text="yes 4821", user="owner-account", attachments=None, message_id=None, chat="chan-9"):
    from app.core.connectors.base import IncomingMessage

    return IncomingMessage(
        connector_id="c1", platform_chat_id=chat, platform_user_id=user,
        platform_user_name="Someone", text=text, attachments=attachments or [],
        platform_message_id=uuid.uuid4().hex if message_id is None else message_id,
    )


def _open_request(code="4821", request_id="req-1", session_id="sess-1", chat="chan-9", tool="Bash"):
    """A real pending row carrying ``code`` in ``chat`` (no code: asked before codes)."""
    from app.db import interactions

    payload = {"request_id": request_id, "tool_name": tool, "tool_input": {"command": "ls"}}
    if code is not None:
        payload[interactions.CHAT_CODES_KEY] = {f"c1:{chat}": code}
    return interactions.create_interaction(session_id, "permission", payload, 300)["id"]


def _state(interaction_id):
    from app.db import interactions

    return interactions.get_interaction(interaction_id)["state"]


def _told(instance):
    return instance.send_message.await_args.args[0].text


@pytest.fixture
def channel(mock_db):
    """A chat on a running connector, answered through a guarded write like the real one."""
    from app.db import interactions

    instance = _fake_instance()

    async def respond(request_id, session_id, decision):
        row = next((r for r in interactions.get_pending_interactions(session_id)
                    if r["payload"].get("request_id") == request_id), None)
        return {"resolved": row is not None and interactions.answer_interaction(row["id"], {"decision": decision})}

    with patch("app.core.connector_service.sender_is_owner", side_effect=lambda _c, user: user == "owner-account"), \
            patch("app.network.ownership.enrolled", return_value=False), \
            patch("app.core.interaction_wait.is_awaited", return_value=True) as awaited, \
            patch("app.core.connectors.manager.connector_manager") as manager, \
            patch("app.core.permission_handler.permission_handler.respond",
                  new=AsyncMock(side_effect=respond)) as responded:
        manager.get_running_instance.return_value = instance
        yield {"respond": responded, "instance": instance, "awaited": awaited}


class TestAnswerFromChat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,decision", [
        ("yes 4821", "allow"), ("no 4821", "deny"), ("YES 4821", "allow"), ("  No   4821 ", "deny"),
        ("yes4821", "allow"), ("nO\t4821", "deny"),
    ])
    async def test_a_reply_naming_the_code_settles_that_request(self, channel, text, decision):
        from app.core.connector_permissions import answer_from_chat

        _open_request()
        assert await answer_from_chat(_chat_message(text)) is True
        channel["respond"].assert_awaited_once_with("req-1", "sess-1", decision)
        assert _told(channel["instance"]).startswith("Approved" if decision == "allow" else "Declined")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [
        "yes 482", "yes 48211", "yes, 4821", "yes 4821.", "yes please 4821", "approve 4821", "4821",
        "yes 4821 now", "maybe 4821", "yes ٤٨٢١", "what will it do?", "maybe", "",
    ])
    async def test_anything_looser_is_an_ordinary_message(self, channel, text):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        assert await answer_from_chat(_chat_message(text)) is False
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"

    @pytest.mark.asyncio
    async def test_each_code_answers_only_its_own_request_in_any_order(self, channel):
        from app.core.connector_permissions import answer_from_chat

        older = _open_request("1111", "req-older")
        newer = _open_request("2222", "req-newer")

        assert await answer_from_chat(_chat_message("yes 2222")) is True
        channel["respond"].assert_awaited_once_with("req-newer", "sess-1", "allow")
        assert (_state(newer), _state(older)) == ("answered", "pending")

        assert await answer_from_chat(_chat_message("no 1111")) is True
        channel["respond"].assert_awaited_with("req-older", "sess-1", "deny")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["yes", "No.", "go ahead"])
    async def test_a_bare_answer_approves_nothing_and_shows_the_reply_to_send(self, channel, text):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        assert await answer_from_chat(_chat_message(text)) is True  # not an agent turn
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"
        assert _told(channel["instance"]) == "To answer, reply yes 4821 or no 4821."

    @pytest.mark.asyncio
    async def test_a_bare_answer_with_several_open_lists_their_codes(self, channel):
        from app.core.connector_permissions import answer_from_chat

        _open_request("1111", "req-a")
        _open_request("2222", "req-b")
        assert await answer_from_chat(_chat_message("yes")) is True
        channel["respond"].assert_not_awaited()
        assert "1111, 2222" in _told(channel["instance"])

    @pytest.mark.asyncio
    async def test_a_bare_answer_with_nothing_open_is_an_ordinary_message(self, channel):
        from app.core.connector_permissions import answer_from_chat

        assert await answer_from_chat(_chat_message("yes")) is False
        channel["instance"].send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bare_answer_counts_only_requests_asked_in_this_chat(self, channel):
        from app.core.connector_permissions import answer_from_chat

        _open_request(chat="another-chat")
        assert await answer_from_chat(_chat_message("yes")) is False

    @pytest.mark.asyncio
    async def test_a_code_not_open_is_consumed_and_says_so(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request("4821")
        assert await answer_from_chat(_chat_message("yes 9999")) is True
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"
        assert _told(channel["instance"]) == "Request 9999 is no longer open."

    @pytest.mark.asyncio
    async def test_a_code_of_a_closed_request_says_so(self, channel):
        from app.core.connector_permissions import answer_from_chat
        from app.db import interactions

        assert interactions.answer_interaction(_open_request(), {"decision": "deny"})
        assert await answer_from_chat(_chat_message("yes 4821")) is True
        channel["respond"].assert_not_awaited()
        assert _told(channel["instance"]) == "Request 4821 is no longer open."

    @pytest.mark.asyncio
    async def test_a_request_closed_between_lookup_and_answer_says_so(self, channel):
        from app.core.connector_permissions import answer_from_chat

        _open_request()
        channel["respond"].side_effect = None
        channel["respond"].return_value = {"error": "Request not found"}
        assert await answer_from_chat(_chat_message("yes 4821")) is True
        assert _told(channel["instance"]) == "Request 4821 is no longer open."

    @pytest.mark.asyncio
    async def test_a_code_in_another_chat_is_not_open_here(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request("4821", chat="another-chat")
        assert await answer_from_chat(_chat_message("yes 4821")) is True
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"

    @pytest.mark.asyncio
    async def test_a_request_from_before_codes_stays_for_the_app(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request(code=None)
        assert await answer_from_chat(_chat_message("yes")) is False  # nothing with a code is open
        assert await answer_from_chat(_chat_message("yes 4821")) is True
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["yes 4821", "yes"])
    async def test_nobody_else_can_answer_even_with_the_code(self, channel, text):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        assert await answer_from_chat(_chat_message(text, user="someone-else")) is False
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"

    @pytest.mark.asyncio
    async def test_a_worker_that_does_not_own_the_connector_answers_nothing(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        with patch("app.network.ownership.enrolled", return_value=True), \
                patch("app.network.ownership.owns", return_value=False), \
                patch("app.db.database.get_connector", return_value={"id": "c1"}):
            assert await answer_from_chat(_chat_message("yes 4821")) is False
        assert _state(request) == "pending"

    @pytest.mark.asyncio
    async def test_a_code_with_an_attachment_is_an_ordinary_message(self, channel):
        from app.core.connector_permissions import answer_from_chat

        _open_request()
        assert await answer_from_chat(_chat_message(attachments=[{"name": "a.png"}])) is False
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_message_cannot_approve_two_requests(self, channel):
        """The platform repeats the owner's reply while another request is open."""
        from app.core.connector_permissions import answer_from_chat

        first = _open_request("1111", "req-a")
        second = _open_request("2222", "req-b")
        reply = _chat_message("yes 1111", message_id="wamid.1")
        assert await answer_from_chat(reply) is True
        assert await answer_from_chat(reply) is True  # consumed, not an agent turn
        channel["respond"].assert_awaited_once_with("req-a", "sess-1", "allow")
        assert (_state(first), _state(second)) == ("answered", "pending")

    @pytest.mark.asyncio
    async def test_a_repeat_cannot_answer_a_later_request_that_drew_the_same_code(self, channel):
        """Why the per-message claim stays: a code returns to the pool when it closes."""
        from app.core.connector_permissions import answer_from_chat

        _open_request("4821", "req-a")
        reply = _chat_message("yes 4821", message_id="wamid.reuse")
        assert await answer_from_chat(reply) is True
        later = _open_request("4821", "req-later")
        channel["instance"].send_message.reset_mock()

        assert await answer_from_chat(reply) is True
        channel["respond"].assert_awaited_once_with("req-a", "sess-1", "allow")
        assert _state(later) == "pending"
        channel["instance"].send_message.assert_not_awaited()  # a repeat is told nothing

    @pytest.mark.asyncio
    async def test_concurrent_deliveries_answer_at_most_once(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        reply = _chat_message("yes 4821", message_id="wamid.2")
        assert await asyncio.gather(*(answer_from_chat(reply) for _ in range(8))) == [True] * 8
        channel["respond"].assert_awaited_once_with("req-1", "sess-1", "allow")
        assert _state(request) == "answered"

    @pytest.mark.asyncio
    async def test_the_answer_applies_once_even_without_the_claim(self, channel):
        """The guarded write on the request is its own one-winner step."""
        from app.core.connector_permissions import answer_from_chat

        _open_request()
        with patch("app.db.connectors.claim_answer_message", return_value=True):
            assert await asyncio.gather(*(answer_from_chat(_chat_message("yes 4821")) for _ in range(8))) == [True] * 8
        assert sum(call.args[2] == "allow" for call in channel["respond"].await_args_list) == 8  # all tried
        told = [call.args[0].text for call in channel["instance"].send_message.await_args_list]
        assert sum(text.startswith("Approved") for text in told) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message_id", ["", "   "])
    async def test_a_message_with_no_platform_id_answers_nothing(self, channel, message_id):
        """Nothing identifies a repeat of it, so the owner is sent to the app."""
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        assert await answer_from_chat(_chat_message("yes 4821", message_id=message_id)) is True
        channel["respond"].assert_not_awaited()
        assert _state(request) == "pending"
        assert "Answer it in the app." in _told(channel["instance"])

    @pytest.mark.asyncio
    async def test_the_same_id_in_another_chat_is_another_message(self, channel):
        """Telegram numbers messages per chat."""
        from app.core.connector_permissions import answer_from_chat

        _open_request("1111", "req-a", chat="chan-1")
        _open_request("2222", "req-b", chat="chan-2")
        assert await answer_from_chat(_chat_message("yes 1111", message_id="7", chat="chan-1")) is True
        assert await answer_from_chat(_chat_message("yes 2222", message_id="7", chat="chan-2")) is True
        assert channel["respond"].await_count == 2

    @pytest.mark.asyncio
    async def test_a_consumed_reply_starts_no_agent_turn(self, channel):
        from app.core.connectors.manager import ConnectorManager

        _open_request()
        manager = ConnectorManager.__new__(ConnectorManager)
        manager._message_locks = MagicMock()
        for text, message_id in (("yes", "m0"), ("yes 4821", "m1"), ("yes 4821", "m1"), ("yes 9999", "m2")):
            await ConnectorManager.handle_incoming_message(manager, _chat_message(text, message_id=message_id))
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
    async def test_a_failure_before_the_claim_does_not_consume_the_reply(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        reply = _chat_message(message_id="owner-yes")
        with patch("app.db.connectors.claim_answer_message", side_effect=RuntimeError("database gone")):
            assert await answer_from_chat(reply) is False
        channel["instance"].send_message.assert_not_awaited()

        assert await answer_from_chat(reply) is True
        assert _state(request) == "answered"

    @pytest.mark.asyncio
    async def test_a_failed_answer_stays_consumed_and_asks_for_a_new_reply(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        reply = _chat_message(message_id="owner-yes")
        real_respond = channel["respond"].side_effect
        channel["respond"].side_effect = RuntimeError("database gone")

        assert await answer_from_chat(reply) is True
        assert "new reply" in _told(channel["instance"])
        assert _state(request) == "pending"

        channel["respond"].side_effect = real_respond
        channel["instance"].send_message.reset_mock()
        assert await answer_from_chat(reply) is True
        channel["instance"].send_message.assert_not_awaited()
        assert _state(request) == "pending"

        assert await answer_from_chat(_chat_message(message_id="owner-yes-again")) is True
        assert _state(request) == "answered"

    @pytest.mark.asyncio
    async def test_a_lookup_that_fails_after_the_claim_stays_consumed(self, channel):
        from app.core.connector_permissions import answer_from_chat

        request = _open_request()
        with patch("app.db.interactions.open_chat_codes", side_effect=RuntimeError("database gone")):
            assert await answer_from_chat(_chat_message()) is True
        assert "new reply" in _told(channel["instance"])
        assert _state(request) == "pending"
        channel["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_answer_does_not_wait_behind_the_blocked_turn(self):
        """The turn holding the chat's lock is the one waiting for this answer."""
        from app.core.connectors.manager import ConnectorManager

        manager = ConnectorManager.__new__(ConnectorManager)
        manager._message_locks = MagicMock()
        with patch("app.core.connector_permissions.answer_from_chat", new=AsyncMock(return_value=True)):
            await ConnectorManager.handle_incoming_message(manager, _chat_message())
        manager._message_locks.get.assert_not_called()

    def test_only_the_owners_own_chat_is_told_the_action(self):
        from app.core.connector_permissions import _action_to_offer

        cs = {"connector_id": "c1", "platform_user_id": "owner-account"}
        with patch("app.core.connector_service.sender_is_owner", side_effect=lambda _c, user: user == "owner-account"):
            assert "ls" in _action_to_offer(cs, "Bash", {"command": "ls"})
            assert _action_to_offer({**cs, "platform_user_id": "x"}, "Bash", {"command": "ls"}) is None
            assert _action_to_offer(cs, "ModelGuardrail", {}) is None


# =============================================================================
# The code on the request, in the chat and across workers
# =============================================================================


class TestOnlyARequestThisWorkerCanActOnIsAnswered:
    """A row can look open here with nothing behind it: a read-only copy synced
    from another worker, or a request whose waiter died in a restart."""

    @pytest.mark.asyncio
    async def test_a_synced_copy_is_left_alone_and_sent_to_the_app(self, channel):
        from app.core.connector_permissions import answer_from_chat

        channel["awaited"].return_value = False
        copy = _open_request(session_id="session-on-another-worker")

        assert await answer_from_chat(_chat_message()) is True

        assert "in the app" in _told(channel["instance"])
        assert "Approved" not in _told(channel["instance"])
        channel["respond"].assert_not_awaited()
        assert _state(copy) == "pending"

    @pytest.mark.asyncio
    async def test_after_a_restart_the_code_resolves_but_nothing_is_approved(self, mock_db):
        """The code is on the row, so it still names the request; with no waiter
        an approval would be recorded and never run, so the chat is sent to the app."""
        from app.core.connector_permissions import answer_from_chat
        from app.core.permission_handler import PermissionHandler

        request = _open_request()
        instance = _fake_instance()
        with patch("app.core.connector_service.sender_is_owner", return_value=True), \
                patch("app.network.ownership.enrolled", return_value=False), \
                patch("app.core.connectors.manager.connector_manager") as manager, \
                patch("app.core.permission_handler.permission_handler", PermissionHandler()):
            manager.get_running_instance.return_value = instance
            assert await answer_from_chat(_chat_message("no 4821")) is True
        assert "in the app" in _told(instance)
        assert _state(request) == "pending"

    @staticmethod
    def _released_approval():
        from app.db import interactions
        from app.db.base import get_db

        request = _open_request()
        payload = {**interactions.get_interaction(request)["payload"], "owner_approval": {"subtype": "x"}, "released": True}
        with get_db() as conn:
            conn.cursor().execute("UPDATE pending_interactions SET payload=%s WHERE id=%s",
                                  (json.dumps(payload), request))
        return request

    @pytest.mark.asyncio
    @pytest.mark.parametrize("runner,answered", [(None, True), ("another-worker", False)])
    async def test_a_call_already_let_go_is_answered_only_where_the_chat_runs(self, channel, runner, answered):
        from app.core.connector_permissions import answer_from_chat

        channel["awaited"].return_value = False
        request = self._released_approval()
        with patch("app.db.database.get_session", return_value={"id": "sess-1"}), \
                patch("app.network.control.owner_of", return_value=runner):
            assert await answer_from_chat(_chat_message()) is True
        assert _state(request) == ("answered" if answered else "pending")

    @pytest.mark.asyncio
    async def test_a_real_waiter_is_what_counts(self, mock_db):
        from app.core import interaction_wait
        from app.db import interactions

        request = _open_request()
        with patch("app.core.interaction_wait._ensure_listener"), \
                patch("app.core.interaction_wait._set_work_paused", new=AsyncMock()):
            waiter = asyncio.create_task(interaction_wait.await_interaction(request, "sess-1", 10, poll_seconds=0.01))
            while not interaction_wait.is_awaited(request):
                await asyncio.sleep(0.01)
            assert interactions.answer_interaction(request, {"decision": "allow"})
            await waiter
        assert not interaction_wait.is_awaited(request)


class TestCodesOnRequests:
    @pytest.fixture
    def chat(self, channel):
        from app.db import interactions

        request = interactions.create_interaction(
            "sess-1", "permission", {"request_id": "req-1", "tool_name": "Bash", "tool_input": {"command": "ls"}}, 300)
        link = {"connector_id": "c1", "platform_chat_id": "42", "platform_user_id": "owner-account"}
        with patch("app.db.database.get_connector_sessions_for_ai_session", return_value=[link]):
            yield {**channel, "request": request["id"]}

    async def _ask(self, chat, tool="Bash"):
        await deliver_permission_prompt("sess-1", "req-1", tool, {"command": "ls"}, "d",
                                        interaction_id=chat["request"])

    @staticmethod
    def _code(chat):
        from app.db import interactions

        return interactions.get_interaction(chat["request"])["payload"].get(interactions.CHAT_CODES_KEY, {}).get("c1:42")

    @pytest.mark.asyncio
    async def test_the_prompt_shows_the_code_that_answers_it(self, chat):
        from app.core.connector_permissions import answer_from_chat

        await self._ask(chat)
        code = self._code(chat)
        assert len(code) == 4 and code.isascii() and code.isdigit()
        chat["instance"].send_message.assert_awaited_once()
        assert _told(chat["instance"]) == f"The agent wants to run `ls`. Reply yes {code} or no {code}."

        assert await answer_from_chat(_chat_message(f"yes {code}", chat="42")) is True
        chat["respond"].assert_awaited_once_with("req-1", "sess-1", "allow")

    @pytest.mark.asyncio
    async def test_a_request_already_settled_gets_no_code(self, chat):
        from app.db import interactions

        assert interactions.answer_interaction(chat["request"], {"decision": "allow"})
        await self._ask(chat)
        assert _told(chat["instance"]) == "The agent wants to run `ls`. Answer it in the app."

    @pytest.mark.asyncio
    async def test_a_connector_with_a_command_prefix_is_not_invited_to_reply(self, chat):
        """Its adapter drops a reply without the prefix, and keeps it on one with it."""
        chat["instance"].config = {"command_prefix": "!"}
        await self._ask(chat)
        assert _told(chat["instance"]) == "The agent wants to run `ls`. Answer it in the app."
        assert self._code(chat) is None

    @pytest.mark.asyncio
    async def test_a_safety_check_gets_no_code(self, chat):
        from app.core.connector_permissions import answer_from_chat

        await self._ask(chat, tool="ModelGuardrail")
        assert self._code(chat) is None
        assert _told(chat["instance"]) == "Waiting for the owner to approve ModelGuardrail."
        assert await answer_from_chat(_chat_message("yes", chat="42")) is False
        chat["respond"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_someone_elses_chat_only_hears_that_the_agent_waits(self, chat):
        with patch("app.core.connector_service.sender_is_owner", return_value=False):
            await self._ask(chat)
        assert self._code(chat) is None
        assert _told(chat["instance"]) == "Waiting for the owner to approve Bash."

    def test_a_code_is_never_drawn_while_it_is_open_in_that_chat(self, mock_db):
        from app.db import interactions

        taken = interactions.assign_chat_code(_open_request(code=None, request_id="a"), "c1:42")
        with patch("app.db.interactions.secrets.choice", side_effect=lambda pool: pool[0]) as choice:
            second = interactions.assign_chat_code(_open_request(code=None, request_id="b"), "c1:42")
        pool = choice.call_args.args[0]
        assert taken not in pool and len(pool) == 9999 and second != taken

    @staticmethod
    def _next_pool(chat="c1:42"):
        from app.db import interactions

        with patch("app.db.interactions.secrets.choice", side_effect=lambda pool: pool[0]) as choice:
            interactions.assign_chat_code(_open_request(code=None, request_id=uuid.uuid4().hex), chat)
        return choice.call_args.args[0]

    def test_a_closed_requests_code_is_not_drawn_again(self, mock_db):
        from app.db import interactions

        request = _open_request(code=None, request_id="a")
        taken = interactions.assign_chat_code(request, "c1:42")
        assert interactions.answer_interaction(request, {"decision": "deny"})
        assert taken not in self._next_pool()

    def test_a_used_code_stays_retired_when_its_row_is_deleted(self, mock_db):
        from app.db import interactions
        from app.db.base import get_db

        request = _open_request(code=None, request_id="a")
        taken = interactions.assign_chat_code(request, "c1:42")
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM pending_interactions WHERE id=%s", (request,))
        assert taken not in self._next_pool()

    def test_a_used_code_returns_only_after_the_claim_window_and_the_longest_open_time(self, mock_db):
        from app.db import interactions
        from app.db.connectors import ANSWER_CLAIM_TTL_SECONDS

        request = _open_request(code=None, request_id="a")
        taken = interactions.assign_chat_code(request, "c1:42")
        assert interactions.answer_interaction(request, {"decision": "deny"})
        now = time.time()
        with patch("app.db.interactions.time.time", return_value=now + ANSWER_CLAIM_TTL_SECONDS + 15 * 60):
            assert taken not in self._next_pool()
        with patch("app.db.interactions.time.time", return_value=now + ANSWER_CLAIM_TTL_SECONDS + 3700):
            assert taken in self._next_pool()

    def test_retired_codes_are_kept_per_chat(self, mock_db):
        from app.db import interactions

        taken = interactions.assign_chat_code(_open_request(code=None, request_id="a"), "c1:42")
        assert taken in self._next_pool("c1:43")

    def test_the_retired_list_stays_on_this_worker(self, mock_db):
        from app.db import interactions
        from app.network import policy

        assert not policy.is_shared_setting_key(interactions.CHAT_CODES_USED_PREFIX + "abc")

    @pytest.mark.asyncio
    async def test_a_chat_with_no_code_left_is_sent_to_the_app(self, chat):
        from app.db import interactions
        from app.db.base import get_db

        chat_key = "c1:42"
        used = {f"{n:04d}": int(time.time()) + 3600 for n in range(10000)}
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO system_settings (key,value) VALUES (%s,%s)",
                (interactions.CHAT_CODES_USED_PREFIX + hashlib.sha256(chat_key.encode()).hexdigest(), json.dumps(used)))

        await self._ask(chat)

        assert self._code(chat) is None
        assert "in the app" in _told(chat["instance"])
        assert "Reply yes" not in _told(chat["instance"])

    @pytest.mark.asyncio
    async def test_a_late_first_delivery_cannot_land_on_a_newer_request(self, channel):
        """The reply to request A is delayed until A has closed and B has opened."""
        from app.core.connector_permissions import answer_from_chat
        from app.db import interactions

        first = _open_request(code=None, request_id="a")
        code = interactions.assign_chat_code(first, "c1:chan-9")
        assert interactions.answer_interaction(first, {"decision": "deny"})
        later = _open_request(code=None, request_id="b")
        with patch("app.db.interactions.secrets.choice", side_effect=lambda pool: code if code in pool else pool[0]):
            assert interactions.assign_chat_code(later, "c1:chan-9") != code

        assert await answer_from_chat(_chat_message(f"yes {code}", message_id="late")) is True

        assert _state(later) == "pending"
        assert "no longer open" in _told(channel["instance"])
        channel["respond"].assert_not_awaited()

    def test_every_chat_keeps_its_own_code(self, mock_db):
        from app.db import interactions

        request = _open_request(code=None)
        first = interactions.assign_chat_code(request, "c1:a")
        second = interactions.assign_chat_code(request, "c2:b")
        payload = interactions.get_interaction(request)["payload"]
        assert payload[interactions.CHAT_CODES_KEY] == {"c1:a": first, "c2:b": second}
        assert payload["request_id"] == "req-1"

    def test_a_code_never_reaches_the_tool_input_or_description(self, mock_db):
        """It stays beside the request: the agent's own view of its call is unchanged."""
        from app.db import interactions

        request = _open_request(code=None)
        code = interactions.assign_chat_code(request, "c1:42")
        payload = interactions.get_interaction(request)["payload"]
        assert code not in repr({k: v for k, v in payload.items() if k != interactions.CHAT_CODES_KEY})


class TestAConnectorMovesBetweenWorkers:
    """One database stands in for two workers: each worker sees only its own
    requests, and per-worker claims are dropped on a move. A controlled check of
    the rule, not two real workers."""

    @pytest.fixture
    def network(self, channel):
        from app.db import connectors as connectors_db
        from app.db import interactions
        from app.db.base import get_db

        here = {"worker": "A"}

        def worker_view(chat):
            rows = interactions.get_pending_interactions(f"session-{here['worker']}")
            return {row["payload"][interactions.CHAT_CODES_KEY][chat]: row
                    for row in rows if chat in row["payload"].get(interactions.CHAT_CODES_KEY, {})}

        def move_to(worker):
            here["worker"] = worker
            with get_db() as conn:
                conn.cursor().execute("DELETE FROM system_settings WHERE key LIKE %s",
                                      (connectors_db.ANSWER_CLAIM_PREFIX + "%",))

        with patch("app.db.interactions.open_chat_codes", side_effect=worker_view):
            yield {**channel, "move_to": move_to}

    @pytest.mark.asyncio
    async def test_a_replayed_reply_answers_only_the_request_it_names(self, network):
        """A to B and back, with the reply repeated on each side."""
        from app.core.connector_permissions import answer_from_chat

        on_a = _open_request("1111", "req-A", "session-A")
        on_b = _open_request("2222", "req-B", "session-B")
        reply = _chat_message("yes 2222", message_id="owner-yes")

        network["move_to"]("B")
        assert await answer_from_chat(reply) is True
        network["respond"].assert_awaited_once_with("req-B", "session-B", "allow")

        network["move_to"]("A")
        assert await answer_from_chat(reply) is True  # same message, A has no claim for it
        network["respond"].assert_awaited_once_with("req-B", "session-B", "allow")
        assert (_state(on_a), _state(on_b)) == ("pending", "answered")
        assert _told(network["instance"]) == "Request 2222 is no longer open."

        # A's own request still takes its own reply.
        assert await answer_from_chat(_chat_message("no 1111")) is True
        network["respond"].assert_awaited_with("req-A", "session-A", "deny")

    @pytest.mark.asyncio
    async def test_a_reply_replayed_after_a_move_answers_nothing_new(self, network):
        from app.core.connector_permissions import answer_from_chat

        _open_request("1111", "req-A", "session-A")
        reply = _chat_message("yes 1111", message_id="owner-yes")
        assert await answer_from_chat(reply) is True

        network["move_to"]("B")
        on_b = _open_request("2222", "req-B", "session-B")
        assert await answer_from_chat(reply) is True
        network["respond"].assert_awaited_once_with("req-A", "session-A", "allow")
        assert _state(on_b) == "pending"

    @pytest.mark.asyncio
    async def test_the_new_owner_answers_its_own_request(self, network):
        from app.core.connector_permissions import answer_from_chat

        network["move_to"]("B")
        _open_request("2222", "req-B", "session-B")
        assert await answer_from_chat(_chat_message("yes 2222")) is True
        network["respond"].assert_awaited_once_with("req-B", "session-B", "allow")

    @pytest.mark.asyncio
    async def test_the_remaining_limit_is_the_same_code_open_on_a_second_worker(self, network):
        """Workers do not share codes. If a platform delivers one reply to two
        workers that each hold the same code open in that chat, each answers its
        own: about 1 in 10,000, and only around a move."""
        from app.core.connector_permissions import answer_from_chat

        on_a = _open_request("4821", "req-A", "session-A")
        on_b = _open_request("4821", "req-B", "session-B")
        reply = _chat_message("yes 4821", message_id="owner-yes")

        assert await answer_from_chat(reply) is True
        network["move_to"]("B")
        assert await answer_from_chat(reply) is True
        assert (_state(on_a), _state(on_b)) == ("answered", "answered")


# =============================================================================
# The whole path: a real adapter's receive loop into the real manager
# =============================================================================

@pytest.mark.parametrize("kind", ["line", "msteams", "bluebubbles"])
@pytest.mark.asyncio
async def test_channels_that_cannot_verify_a_sender_are_sent_to_the_app(mock_db, kind):
    """The real owner gate: their ids are refused, so no chat can reach a code."""
    from app.core.connector_permissions import answer_from_chat
    from app.db import interactions

    connector = {"id": "c1", "type": kind, "config": {"owner_sender_ids": ["owner-account"]}}
    link = {"connector_id": "c1", "platform_chat_id": "42", "platform_user_id": "owner-account"}
    request = interactions.create_interaction(
        "sess-1", "permission", {"request_id": "req-1", "tool_name": "Bash", "tool_input": {"command": "ls"}}, 300)["id"]
    instance = _fake_instance()
    with patch("app.db.database.get_connector", return_value=connector), \
            patch("app.db.database.get_connector_sessions_for_ai_session", return_value=[link]), \
            patch("app.network.ownership.enrolled", return_value=False), \
            patch("app.core.connectors.manager.connector_manager") as manager:
        manager.get_running_instance.return_value = instance
        await deliver_permission_prompt("sess-1", "req-1", "Bash", {"command": "ls"}, "d", interaction_id=request)
        told = _told(instance)
        assert await answer_from_chat(_chat_message("yes 4821", chat="42")) is False
    assert "Answer it in the app." in told and "Reply yes" not in told
    assert interactions.CHAT_CODES_KEY not in interactions.get_interaction(request)["payload"]


class TestSignalReadsTheReplyWhileItsTurnWaits:
    """Real: SignalConnector's SSE loop, ConnectorManager's entry, the owner gate,
    the code draw, the waiter and the responder. Fake: signal-cli's HTTP stream
    and the agent turn, which is held open the way a turn waiting on the request is."""

    OWNER, STRANGER, GROUP = "+15550100", "+15550111", "group-1"

    @pytest.fixture
    async def signal(self, mock_db):
        import httpx

        from app.core.connectors.manager import ConnectorManager
        from app.core.connectors.signal_connector import SignalConnector
        from app.core.permission_handler import PermissionHandler

        manager = ConnectorManager()
        events: asyncio.Queue = asyncio.Queue()
        turns, release = [], asyncio.Event()

        async def hold_turn(message):
            turns.append(message.text)
            await release.wait()

        manager._handle_incoming_message = hold_turn
        adapter = SignalConnector("c1", "signal", {"account": "+15550199"},
                                  manager.handle_incoming_message, AsyncMock())
        adapter.send_message = AsyncMock(return_value=True)
        manager._connectors["c1"] = adapter

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_lines(self):
                while True:
                    yield await events.get()

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.stream = MagicMock(return_value=Stream())
        connector = {"id": "c1", "type": "signal", "config": {"owner_sender_ids": [self.OWNER]}}
        link = {"connector_id": "c1", "platform_chat_id": self.GROUP, "platform_user_id": self.OWNER}
        with patch.object(httpx, "AsyncClient", return_value=client), \
                patch("app.db.database.get_connector", return_value=connector), \
                patch("app.db.database.get_connector_sessions_for_ai_session", return_value=[link]), \
                patch("app.network.ownership.enrolled", return_value=False), \
                patch("app.core.connectors.manager.connector_manager", manager), \
                patch("app.core.permission_handler.permission_handler", PermissionHandler()), \
                patch("app.core.interaction_wait._ensure_listener"), \
                patch("app.core.interaction_wait._set_work_paused", new=AsyncMock()):
            listener = asyncio.create_task(adapter._sse_listener())
            try:
                yield {"adapter": adapter, "events": events, "turns": turns, "release": release}
            finally:
                release.set()
                await adapter.disconnect()
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

    @classmethod
    def _event(cls, sender, text, stamp):
        envelope = {"sourceNumber": sender, "timestamp": stamp, "dataMessage": {"message": text, "timestamp": stamp,
                                                                                 "groupInfo": {"groupId": cls.GROUP}}}
        return "data:" + json.dumps({"envelope": envelope})

    @staticmethod
    async def _until(condition):
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never happened")

    @pytest.mark.asyncio
    async def test_the_reply_is_read_and_answered_while_the_turn_waits(self, signal):
        from app.core import interaction_wait
        from app.db import interactions

        signal["events"].put_nowait(self._event(self.OWNER, "tidy the folder", 1))
        await self._until(lambda: signal["turns"] == ["tidy the folder"])

        request = interactions.create_interaction(
            "sess-1", "permission", {"request_id": "req-1", "tool_name": "Bash", "tool_input": {"command": "ls"}}, 300)["id"]
        waiter = asyncio.create_task(interaction_wait.await_interaction(request, "sess-1", 30, poll_seconds=0.01))
        await self._until(lambda: interaction_wait.is_awaited(request))
        await deliver_permission_prompt("sess-1", "req-1", "Bash", {"command": "ls"}, "d", interaction_id=request)
        code = interactions.get_interaction(request)["payload"][interactions.CHAT_CODES_KEY][f"c1:{self.GROUP}"]
        assert f"Reply yes {code} or no {code}" in signal["adapter"].send_message.await_args.args[0].text

        signal["events"].put_nowait(self._event(self.STRANGER, f"yes {code}", 2))
        await self._until(lambda: len(signal["adapter"]._chat_tasks) == 2)
        await asyncio.sleep(0.05)
        assert interactions.get_interaction(request)["state"] == "pending"

        signal["events"].put_nowait(self._event(self.OWNER, f"yes {code}", 3))
        row = await asyncio.wait_for(waiter, timeout=5)

        assert row["state"] == "answered" and row["answer"]["decision"] == "allow"
        assert not signal["release"].is_set() and signal["turns"] == ["tidy the folder"]
        await self._until(lambda: signal["adapter"].send_message.await_args.args[0].text.startswith("Approved"))

    @pytest.mark.asyncio
    async def test_disconnect_cancels_a_waiting_turn(self, signal):
        signal["events"].put_nowait(self._event(self.OWNER, "tidy the folder", 1))
        await self._until(lambda: signal["turns"] == ["tidy the folder"])
        task = next(iter(signal["adapter"]._chat_tasks))

        await signal["adapter"].disconnect()

        assert task.cancelled() and not signal["adapter"]._chat_tasks

    @pytest.mark.asyncio
    async def test_a_full_house_says_so_without_stopping_the_stream(self, signal):
        signal["adapter"].MAX_CHAT_TASKS = 1
        signal["events"].put_nowait(self._event(self.OWNER, "first", 1))
        await self._until(lambda: signal["turns"] == ["first"])

        signal["events"].put_nowait(self._event(self.STRANGER, "second", 2))
        await self._until(lambda: signal["adapter"].send_message.await_count == 1)

        assert "Too many chats" in signal["adapter"].send_message.await_args.args[0].text
        assert signal["turns"] == ["first"]


# =============================================================================
# Every adapter carries the question out and the coded reply back
# =============================================================================


def _ok_response():
    return MagicMock(status_code=200, text="", json=MagicMock(return_value={}))


def _adapter(platform):
    """A real adapter with only its transport replaced: ``(adapter, feed the
    reply in, what it posted)``."""
    from types import SimpleNamespace as NS

    if platform == "telegram":
        from app.core.connectors.telegram import TelegramConnector

        adapter = TelegramConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        adapter._on_trigger = AsyncMock()
        adapter._bot = MagicMock(send_message=AsyncMock(return_value=NS(message_id=1)))
        sent = adapter._bot.send_message

        def feed(text):
            return adapter._handle_message(MagicMock(
                chat=NS(id=42, type="private"), from_user=NS(id=7, full_name="Owner", username="owner"), text=text, message_id=5,
                caption=None, photo=None, document=None, voice=None, audio=None, video=None, sticker=None,
                reply_to_message=None))
    elif platform == "discord":
        from app.core.connectors.discord_connector import DiscordConnector

        adapter = DiscordConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        channel = MagicMock(send=AsyncMock(return_value=NS(id=1)))
        adapter._client = MagicMock(get_channel=MagicMock(return_value=channel))
        sent = channel.send

        def feed(text):
            return adapter._on_message_handler(MagicMock(
                author=MagicMock(bot=False, id=7), guild=None, channel=MagicMock(id=42), content=text, id=5,
                attachments=[]))
    elif platform == "slack":
        from app.core.connectors.slack import SlackConnector

        adapter = SlackConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        adapter._web_client = MagicMock(chat_postMessage=AsyncMock(return_value={"ts": "1"}),
                                        users_info=AsyncMock(return_value={}))
        sent = adapter._web_client.chat_postMessage

        def feed(text):
            return adapter._handle_message_event({"channel": "42", "user": "U7", "text": text, "ts": "1.5"})
    elif platform == "whatsapp":
        from app.core.connectors.whatsapp import WhatsAppConnector

        adapter = WhatsAppConnector("c1", platform, {"phone_number_id": "1", "access_token": "x"}, AsyncMock(), AsyncMock())
        sent = None

        def feed(text):
            return adapter._process_webhook_message(
                {"type": "text", "from": "15550100", "id": "wamid.1", "text": {"body": text}}, {})
    elif platform == "line":
        from app.core.connectors.line import LINEConnector

        adapter = LINEConnector("c1", platform, {"channel_access_token": "x", "channel_secret": "x"}, AsyncMock(), AsyncMock())
        sent = None

        def feed(text):
            return adapter._process_message_event({
                "source": {"type": "user", "userId": "U7"}, "replyToken": "t",
                "message": {"type": "text", "id": "m1", "text": text}})
    elif platform == "msteams":
        from app.core.connectors.msteams import MSTeamsConnector

        adapter = MSTeamsConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        adapter._get_ref = MagicMock(return_value={"conversation": {"id": "42"}})
        adapter._send_activity = AsyncMock(return_value=True)
        sent = adapter._send_activity

        def feed(text):
            return adapter._handle_message_activity({
                "id": "a1", "from": {"id": "U7"}, "conversation": {"id": "42"}, "text": text})
    elif platform == "signal":
        from app.core.connectors.signal_connector import SignalConnector

        adapter = SignalConnector("c1", platform, {"account": "+15550199"}, AsyncMock(), AsyncMock())
        sent = None

        def feed(text):
            return adapter._handle_data_message(
                "+15550100", "Owner", {"message": text, "timestamp": 1700000000000}, {"timestamp": 1700000000000})
    elif platform == "imessage":
        from app.core.connectors.imessage import IMessageConnector

        adapter = IMessageConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        adapter._rpc = MagicMock(request=AsyncMock(return_value={}))
        sent = adapter._rpc.request

        def feed(text):
            return adapter._on_rpc_notification({"sender": "+15550100", "chat_id": "42", "text": text, "guid": "g1"})
    else:
        from app.core.connectors.bluebubbles import BlueBubblesConnector

        adapter = BlueBubblesConnector("c1", platform, {}, AsyncMock(), AsyncMock())
        adapter._resolve_chat_guid = AsyncMock(return_value="any;-;42")
        adapter._api_post = AsyncMock(return_value=_ok_response())
        sent = adapter._api_post

        def feed(text):
            return adapter._handle_new_message({
                "handle": {"address": "+15550100"}, "text": text, "guid": "g1", "chats": [{"guid": "any;-;42"}]})
    return adapter, feed, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [
    "telegram", "discord", "slack", "whatsapp", "line", "msteams", "signal", "imessage", "bluebubbles",
])
async def test_every_adapter_carries_the_question_and_the_coded_reply(platform):
    """Real adapters with a stand-in transport: not the real platforms."""
    question = "The agent wants to run `ls`. Reply yes 4821 or no 4821."
    adapter, feed, sent = _adapter(platform)
    client = MagicMock(post=AsyncMock(return_value=_ok_response()), get=AsyncMock(return_value=_ok_response()))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        assert await adapter.send_message(OutgoingMessage(platform_chat_id="42", text=question)) is True
        posted = sent or client.post
        assert question in repr(posted.await_args)

        await feed("yes 4821")
        await asyncio.gather(*getattr(adapter, "_chat_tasks", ()))  # Telegram hands a message to a task

    delivered = adapter._on_incoming_message.await_args.args[0]
    assert delivered.text == "yes 4821"
    assert delivered.platform_message_id
