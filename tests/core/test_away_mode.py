"""Away mode: recommended defaults instead of parked questions; approvals deferred, never granted."""
from unittest.mock import AsyncMock, patch

import pytest

from app.core import away_mode
from app.core.agent_types import PermissionResultDeny


QUESTIONS = [
    {"question": "Which track?", "header": "Track",
     "options": [{"label": "Production"}, {"label": "Internal testing (Recommended)"}]},
    {"question": "Tag version?", "header": "Version", "options": ["v1.2.4", "Custom"]},
    {"question": "Which areas?", "header": "Areas", "multiSelect": True,
     "options": [{"label": "Backend (recommended)"}, {"label": "Frontend (Recommended)"}, {"label": "Docs"}]},
    {"question": "Anything else?", "header": "Notes"},
]


def test_recommended_option_else_first_else_open_ended():
    answers = away_mode.auto_answers(QUESTIONS)
    assert answers == {
        "Which track?": "Internal testing (Recommended)",
        "Tag version?": "v1.2.4",
        "Which areas?": "Backend (recommended), Frontend (Recommended)",
        "Anything else?": away_mode.OPEN_ENDED_ANSWER,
    }


def test_formatted_result_tells_the_model_to_report_decisions():
    text = away_mode.format_answers({"Which track?": "Internal testing"})
    assert "Decisions made for you" in text
    assert '"Which track?" → Internal testing' in text


async def test_answer_if_away_records_and_answers():
    with patch.object(away_mode, "is_away_async", AsyncMock(return_value=True)), \
            patch.object(away_mode, "record_decision", AsyncMock()) as record:
        result = await away_mode.answer_if_away("s1", QUESTIONS[:1])
    assert result and "Internal testing (Recommended)" in result
    record.assert_awaited_once()
    assert record.await_args.args[1]["decisions"][0]["choice"] == "Internal testing (Recommended)"


async def test_present_owner_is_asked_as_usual():
    with patch.object(away_mode, "is_away_async", AsyncMock(return_value=False)):
        assert await away_mode.answer_if_away("s1", QUESTIONS) is None
        assert await away_mode.defer_if_away("s1", "anything") is None


async def test_defer_is_recorded_and_never_allows():
    with patch.object(away_mode, "is_away_async", AsyncMock(return_value=True)), \
            patch.object(away_mode, "record_decision", AsyncMock()) as record:
        message = await away_mode.defer_if_away("s1", "permission for Bash")
    assert message == away_mode.DEFERRED_MESSAGE
    assert record.await_args.args[1] == {"kind": "deferred", "action": "permission for Bash"}


async def test_auto_answer_hook_falls_back_to_posting_on_error():
    hook = away_mode.make_question_auto_answer(lambda: "s1")
    with patch.object(away_mode, "answer_if_away", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await hook(QUESTIONS) is None


async def test_permission_prompt_is_deferred_while_away():
    from app.core.permission_handler import PermissionHandler

    handler = PermissionHandler()
    with patch.object(handler, "_check_rules", return_value=None), \
            patch.object(away_mode, "is_away_async", AsyncMock(return_value=True)), \
            patch.object(away_mode, "record_decision", AsyncMock()), \
            patch("app.db.interactions.create_interaction") as create:
        result = await handler.request_permission(
            request_id="r1", session_id="s1", profile_id="p", tool_name="Bash",
            tool_input={"command": "ls"}, broadcast_func=AsyncMock(),
        )
    assert isinstance(result, PermissionResultDeny)
    assert "Owner away; deferred" in result.message
    create.assert_not_called()


async def test_private_access_is_refused_at_once_while_away():
    pytest.importorskip("asyncssh")
    from app.core import chat_access

    with patch.object(away_mode, "is_away_async", AsyncMock(return_value=True)), \
            patch.object(away_mode, "record_decision", AsyncMock()):
        with pytest.raises(chat_access.AccessDenied, match="Owner away; deferred"):
            await chat_access._defer_if_away("s1", "private folder access request")


async def test_release_pending_questions_answers_waiting_requests():
    from app.core import user_question_handler as uqh

    pending = [{"request_id": "q1", "questions": QUESTIONS[:2]}, {"request_id": None, "questions": QUESTIONS}]
    with patch.object(uqh.user_question_handler, "get_pending_questions", return_value=pending), \
            patch.object(uqh.user_question_handler, "respond", AsyncMock(return_value=True)) as respond, \
            patch.object(away_mode, "record_decision", AsyncMock()):
        released = await away_mode.release_pending_questions("s1")
    assert released == 1
    assert respond.await_args.args[2] == {"Which track?": "Internal testing (Recommended)", "Tag version?": "v1.2.4"}


@pytest.fixture
def mode_chat(mock_db):
    from app.db import database

    database.create_profile("away-agent", "Agent", None, {"model": "sonnet"})
    database.create_session("away-chat", "away-agent")
    return "away-chat"


def test_never_delivered_away_does_not_get_a_cancellation(mode_chat):
    from app.db import database

    assert away_mode.turn_note(mode_chat) is None
    database.set_session_run_binding(mode_chat, away_mode=True)
    assert away_mode.turn_note(mode_chat) == away_mode.CONTEXT_NOTE
    # A diagnostic preview is not delivery. Neither is toggling twice while idle.
    database.set_session_run_binding(mode_chat, away_mode=False)
    assert away_mode.turn_note(mode_chat) is None


def test_receipts_are_idempotent_across_replay_and_repeated_transitions(mode_chat):
    from app.db import database, session_events

    for n in range(2):
        database.set_session_run_binding(mode_chat, away_mode=True)
        away_mode.record_context_delivery(mode_chat, away_mode.turn_note(mode_chat), f"on-{n}")
        database.set_session_run_binding(mode_chat, away_mode=False)
        note = away_mode.turn_note(mode_chat)
        assert away_mode.OWNER_BACK_NOTE in note
        away_mode.record_context_delivery(mode_chat, note, f"off-{n}")
        # Late duplicate output from an old turn must not resurrect its mode.
        away_mode.record_context_delivery(mode_chat, away_mode.CONTEXT_NOTE, f"on-{n}")
        away_mode.record_context_delivery(mode_chat, note, f"off-{n}")
        assert away_mode.turn_note(mode_chat) is None
    assert len([e for e in session_events.get_events(mode_chat)
                if e["event_type"] == away_mode._CONTEXT_EVENT]) == 4


def test_worker_inherited_away_has_its_own_cancellation_receipt(mode_chat):
    from app.db import database

    database.create_session("away-worker", "away-agent", parent_session_id=mode_chat, is_worker=True)
    database.set_session_run_binding(mode_chat, away_mode=True)
    note = away_mode.turn_note("away-worker")
    assert note == away_mode.CONTEXT_NOTE
    away_mode.record_context_delivery("away-worker", note, "worker-on")
    database.set_session_run_binding(mode_chat, away_mode=False)
    note = away_mode.turn_note("away-worker")
    assert note == away_mode.OWNER_BACK_NOTE
    assert away_mode.turn_note(mode_chat) is None, "parent never received the Away instruction"
    away_mode.record_context_delivery("away-worker", note, "worker-off")
    assert away_mode.turn_note("away-worker") is None
