"""Question cards survive a move without transferring a provider callback."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_handoff as sh
from app.db import database, interactions, session_events, turns
from app.network import handoff
from tests.core.test_session_handoff import _chat, _clean, _settle  # noqa: F401

QUESTIONS = [
    {
        "question": "Which branch?",
        "header": "Branch",
        "multiSelect": False,
        "options": [
            {"label": "Existing", "description": "Keep this branch"},
            {"label": "New", "description": "Start another branch"},
        ],
    }
]
TARGET = {"capabilities": {"move_questions": 1}}


async def _post(*, blocking=False):
    from app.core.user_question_handler import user_question_handler

    if blocking:
        return asyncio.create_task(
            user_question_handler.request_answers(
                "original", "ask-tool", "s1", QUESTIONS, AsyncMock()
            )
        )
    await user_question_handler.post_question(
        request_id="original",
        tool_use_id="ask-tool",
        session_id="s1",
        questions=QUESTIONS,
        broadcast_func=AsyncMock(),
    )


@pytest.mark.parametrize("by", ["owner", "agent"])
async def test_question_only_move_keeps_answerable_card(by):
    _chat()
    await _post()
    move = AsyncMock()
    with (
        patch("app.network.runners.find", return_value=TARGET),
        patch.object(sh, "_record", AsyncMock()),
        patch("app.api.sessions.move_session_to", move),
    ):
        if by == "owner":
            await sh.queue_move("s1", "DesktopWorker")
        else:
            sh.request("s1", "DesktopWorker")
            sh.schedule("s1")
        assert await _settle(lambda: move.await_count)
    [row] = interactions.get_pending_interactions("s1")
    assert row["payload"]["questions"] == QUESTIONS
    assert (
        sh.resume_plan(
            "s1",
            sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time()),
            "dev-gaming",
        )
        is None
    )
    with handoff.get_db() as conn:
        handoff._idle(conn.cursor(), "s1")


async def test_blocking_producer_is_cancelled_and_same_question_reasked():
    _chat()
    callback = await _post(blocking=True)
    try:
        assert await _settle(lambda: interactions.get_pending_interactions("s1"))
        turn = turns.enqueue_turn("s1", "user", prompt="ask me")
        turns.start_turn(turn["id"], None, "runner")
        session_events.append_events(
            "s1",
            [
                {
                    "type": "tool.use",
                    "turn_id": turn["id"],
                    "payload": {
                        "tool_use_id": "ask-tool",
                        "name": "AskUserQuestion",
                        "input": {"questions": QUESTIONS},
                    },
                }
            ],
        )

        async def interrupt(*args, **kwargs):
            turns.settle_turn(turn["id"], "aborted")

        runtime = SimpleNamespace(interrupt=AsyncMock(side_effect=interrupt))
        pending = sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time())
        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch("app.core.session_runtime.get_live_runtime", return_value=runtime),
        ):
            await sh._prepare_questions(
                "s1",
                {"running": True, "tasks": [], "queued": 0, "wake": False},
                pending,
            )
        assert await callback == {"error": "No answers received"}
        runtime.interrupt.assert_awaited_once()
        [replacement] = interactions.get_pending_interactions("s1")
        assert replacement["payload"]["questions"] == QUESTIONS
        assert replacement["payload"]["nonblocking"] is True
        assert replacement["payload"]["request_id"] != "original"
        assert turns.get_turn(turn["id"])["state"] == "aborted"
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


@pytest.mark.parametrize("blocker", ["tasks", "queued", "wake", "tool"])
async def test_question_plus_other_work_never_detaches(blocker):
    _chat()
    callback = await _post(blocking=True)
    try:
        assert await _settle(lambda: interactions.get_pending_interactions("s1"))
        turn = turns.enqueue_turn("s1", "user", prompt="ask me")
        turns.start_turn(turn["id"], None, "runner")
        session_events.append_events(
            "s1",
            [
                {
                    "type": "tool.use",
                    "turn_id": turn["id"],
                    "payload": {"tool_use_id": "shell", "name": "Bash", "input": {}},
                }
            ],
        )
        state = {
            "running": True,
            "tasks": ["Build"] if blocker == "tasks" else [],
            "queued": int(blocker == "queued"),
            "wake": blocker == "wake",
        }
        runtime = SimpleNamespace(interrupt=AsyncMock())
        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch("app.core.session_runtime.get_live_runtime", return_value=runtime),
        ):
            await sh._prepare_questions(
                "s1",
                state,
                sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time()),
            )
        runtime.interrupt.assert_not_awaited()
        [row] = interactions.get_pending_interactions("s1")
        assert row["payload"]["request_id"] == "original"
        assert not callback.done()
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


async def test_history_transfer_card_answer_queues_agent_input():
    from app.core.user_question_handler import user_question_handler

    _chat()
    await _post()
    snapshot = {
        "sdk_session_id": None,
        "history": handoff._history(database.get_session("s1")),
    }
    [original] = interactions.get_pending_interactions("s1")
    # Simulate a receiver with none of the source process's callbacks.
    with handoff.get_db() as conn:
        conn.cursor().execute(
            "DELETE FROM pending_interactions WHERE session_id=%s", ("s1",)
        )
    handoff.install_history("s1", snapshot, ".")
    [received] = interactions.get_pending_interactions("s1")
    assert received["id"] == original["id"]
    assert (
        user_question_handler.get_pending_questions("s1")[0]["questions"] == QUESTIONS
    )
    with (
        patch(
            "app.core.user_question_handler.UserQuestionHandler._retire_native_question",
            AsyncMock(),
        ),
        patch("app.core.session_runtime.try_steer_pending", AsyncMock()),
        patch("app.core.session_runtime._broadcast_queue_after_start", AsyncMock()),
    ):
        assert await user_question_handler._answer_nonblocking(
            received, {"Which branch?": "Existing"}, None, human_input=True
        )
    [answer] = turns.get_queued_turns("s1")
    assert "Which branch?\nExisting" in answer["prompt"]
    assert interactions.get_pending_interactions("s1") == []


async def test_old_receiver_refuses_without_losing_question():
    _chat()
    await _post()
    with (
        patch("app.network.runners.find", return_value={}),
        pytest.raises(ValueError, match="Update the target worker"),
    ):
        await sh._prepare_questions(
            "s1",
            {"running": False, "tasks": []},
            sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time()),
        )
    assert len(interactions.get_pending_interactions("s1")) == 1


async def test_wait_logs_reason_without_question_contents(caplog):
    import logging

    pending = sh.PendingHandoff(
        "dev-gaming", "DesktopWorker", "", time.time(), expires_at=time.time() + 0.02
    )
    with (
        patch.object(
            sh,
            "busy_state",
            return_value={
                "running": True,
                "tasks": ["Build"],
                "queued": 1,
                "wake": True,
            },
        ),
        patch.object(sh, "POLL_S", 0.01),
        caplog.at_level(logging.INFO, logger=sh.__name__),
    ):
        assert not await sh._wait_idle("s1", pending)
    assert "the turn and 1 queued message, Build and a scheduled wake-up" in caplog.text


@pytest.mark.parametrize("draining", [False, True])
async def test_queued_agent_move_runs_after_runtime_releases_lease_without_new_input(
    draining,
):
    from app.core import idle_reader, session_runtime
    from app.db import runs
    from tests.core.test_session_runtime import _runtime_env

    _chat()
    turn = turns.enqueue_turn("s1", "user", prompt="move when done")
    runtime = session_runtime.SessionRuntime(turn)
    sh.request("s1", "DesktopWorker")

    async def pump(**kwargs):
        if draining:
            turns.interrupt_turn("s1", "stop", turn_id=turn["id"])
            turns.escalate_to_draining(turn["id"])
            runtime._drain_after_cancel = True
        yield {"type": "done"}

    async def drain(sid, *, drain_turn_id):
        assert turns.get_turn(drain_turn_id)["state"] == "draining"
        await idle_reader._settle_drained(sid, drain_turn_id)

    async def moved(*args):
        assert runs.get_active_run("s1") is None
        assert turns.get_turn(turn["id"])["state"] in ("completed", "aborted")

    move = AsyncMock(side_effect=moved)
    # The normal turn-finalize hook is stubbed by _runtime_env. This exercises
    # the real post-lease-release hook, including the early draining return.
    with (
        _runtime_env(pump=pump, patch_idle=False),
        patch("app.core.idle_reader.run_idle_reader", side_effect=drain),
        patch("app.api.sessions.move_session_to", move),
        patch.object(sh, "POLL_S", 0.01),
        patch.object(sh, "_record", AsyncMock()),
    ):
        await runtime.execute()
        assert await _settle(lambda: move.await_count)
        assert await _settle(lambda: "s1" not in sh._running)
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    move.assert_awaited_once()


@pytest.mark.parametrize(
    "error,code",
    [
        (ValueError("Answer or dismiss the open prompt first."), 400),
        (PermissionError("Not the owner"), 409),
    ],
)
async def test_peer_control_refusal_logs_reason(error, code, caplog):
    from fastapi import HTTPException

    from app.api.network import peer_control

    request = SimpleNamespace(
        json=AsyncMock(return_value={"session_id": "s1"}),
        url=SimpleNamespace(scheme="https"),
        scope={},
    )
    with (
        patch("app.network.control.handle", AsyncMock(side_effect=error)),
        patch("app.network.transport.note_contact"),
        pytest.raises(HTTPException) as raised,
    ):
        await peer_control("handoff", request, "test-peer")
    assert raised.value.status_code == code
    assert "Refused handoff control for session s1: " + str(error) in caplog.text
