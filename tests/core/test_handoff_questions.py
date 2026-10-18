"""Question cards survive a move without transferring a provider callback."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_handoff as sh
from app.db import database, interactions, session_events, turns
from app.network import handoff
from tests.core.test_session_handoff import _chat, _clean  # noqa: F401
from tests.core.test_session_handoff import _settle as _poll_until


async def _settle(predicate):
    return await asyncio.wait_for(_poll_until(predicate), timeout=3)


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


async def _post(*, blocking=False, tool_use_id="ask-tool", questions=QUESTIONS):
    from app.core.user_question_handler import user_question_handler

    if blocking:
        return asyncio.create_task(
            user_question_handler.request_answers(
                "original", tool_use_id or "original", "s1", questions, AsyncMock()
            )
        )
    await user_question_handler.post_question(
        request_id="original",
        tool_use_id="ask-tool",
        session_id="s1",
        questions=questions,
        broadcast_func=AsyncMock(),
    )


@pytest.mark.parametrize("by", ["owner", "agent"])
async def test_question_only_move_keeps_answerable_card(by):
    _chat()
    await _post()
    move = AsyncMock()
    deliver = AsyncMock()
    with (
        patch("app.network.runners.find", return_value=TARGET),
        patch("app.network.handoff.resume_carry", return_value=True),
        patch.object(sh, "_record", AsyncMock()),
        patch.object(sh, "deliver_held", deliver),
        patch("app.api.sessions.move_session_to", move),
    ):
        if by == "owner":
            await sh.queue_move("s1", "DesktopWorker")
        else:
            sh.request("s1", "DesktopWorker", "Keep implementing the reviewed change")
            sh.schedule("s1")
        assert await _settle(lambda: deliver.await_count)
    move.assert_awaited_once()
    resume = deliver.await_args.args[3]
    if by == "agent":
        assert (
            "Your hand-off note: Keep implementing the reviewed change"
            in resume["prompt"]
        )
        assert "Continue the work" in resume["prompt"]
    else:
        assert resume is None
    [row] = interactions.get_pending_interactions("s1")
    assert row["payload"]["questions"] == QUESTIONS
    assert not row["payload"].get("handoff_detached")
    with handoff.get_db() as conn:
        handoff._idle(conn.cursor(), "s1")


@pytest.mark.parametrize("missing_tool_id", [False, True])
async def test_blocking_producer_is_cancelled_and_same_question_reasked(
    missing_tool_id,
):
    _chat()
    callback = await _post(
        blocking=True, tool_use_id=None if missing_tool_id else "ask-tool"
    )
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
                        "name": "AskUser",
                        "input": {"questions": QUESTIONS},
                    },
                }
            ],
        )

        notifications = []

        async def interrupt(*args, **kwargs):
            assert not notifications  # Stop the provider before waking its callback.
            turns.settle_turn(turn["id"], "aborted")

        runtime = SimpleNamespace(interrupt=AsyncMock(side_effect=interrupt))
        pending = sh.PendingHandoff(
            "dev-gaming", "DesktopWorker", "Finish the regression tests", time.time()
        )
        from app.core import interaction_wait

        notify = interaction_wait.notify_changed

        def notified(sid):
            notifications.append(sid)
            notify(sid)

        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch("app.core.session_runtime.get_live_runtime", return_value=runtime),
            patch("app.core.interaction_wait.notify_changed", side_effect=notified),
        ):
            await sh._prepare_questions(
                "s1",
                {"running": True, "tasks": [], "queued": 0, "wake": False},
                pending,
            )
        assert await asyncio.wait_for(callback, timeout=3) == {
            "error": "No answers received"
        }
        runtime.interrupt.assert_awaited_once()
        [replacement] = interactions.get_pending_interactions("s1")
        assert replacement["payload"]["questions"] == QUESTIONS
        assert replacement["payload"]["nonblocking"] is True
        assert replacement["payload"]["request_id"] != "original"
        assert turns.get_turn(turn["id"])["state"] == "aborted"
        assert replacement["payload"]["handoff_detached"] is True
        assert replacement["payload"]["handoff_id"] == pending.handoff_id
        assert sh.resume_plan("s1", pending, "dev-gaming") == "question"

        async def record_moved(sid, owner, content, data):
            await sh._record(sid, content, data)
            return True

        with (
            patch.object(sh, "_tell", side_effect=record_moved),
            patch.object(sh, "deliver_held", AsyncMock()) as deliver,
        ):
            assert "question-card answer" in await sh._moved(
                "s1", pending, "dev-gaming"
            )
            assert deliver.await_args.args[3] is None
        # Rehydrate on a receiver with no callback, then answer the card. The
        # hand-off context/note must reach that fresh answer turn, not vanish.
        from app.core.user_question_handler import user_question_handler

        snapshot = {
            "sdk_session_id": None,
            "history": handoff._history(database.get_session("s1")),
        }
        handoff.install_history("s1", snapshot, ".")
        handoff._accept_owner("s1", pending.target_id)
        [received] = interactions.get_pending_interactions("s1")
        assert received["payload"]["handoff_target_id"] == pending.target_id
        with (
            patch(
                "app.network.engine.context",
                return_value=SimpleNamespace(device_id=pending.target_id),
            ),
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
        assert answer["prompt"].startswith("[hand-off] Moved from ")
        assert "Your hand-off note: Finish the regression tests" in answer["prompt"]
        assert "Answer to your question:\nWhich branch?\nExisting" in answer["prompt"]
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


@pytest.mark.parametrize("state", ["pending", "answered", "cancelled", "expired"])
def test_detached_card_owns_resume_in_every_state(state):
    from app.db.base import get_db

    _chat()
    pending = sh.PendingHandoff("dev-gaming", "DesktopWorker", "keep once", time.time())
    row = interactions.create_interaction(
        "s1",
        "question",
        {
            "handoff_detached": True,
            "handoff_id": pending.handoff_id,
            "questions": QUESTIONS,
        },
        None,
    )
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE pending_interactions SET state=%s WHERE id=%s", (state, row["id"])
        )
    assert sh.resume_plan("s1", pending, pending.target_id) == "question"
    new = sh.PendingHandoff(
        "dev-gaming", "DesktopWorker", "different note", time.time()
    )
    with patch("app.network.handoff.resume_carry", return_value=True):
        assert sh.resume_plan("s1", new, new.target_id) == "send"


async def test_legacy_detached_card_keeps_note_once_matching_moved_record_arrives():
    from app.core.user_question_handler import (
        _confirmed_handoff_context,
        user_question_handler,
    )

    _chat()
    context = "[hand-off] Moved from Source to Target. Your hand-off note: Keep the chosen branch"
    payload = {
        "handoff_detached": True,
        "handoff_id": "legacy-move",
        "handoff_context": context,
        "nonblocking": True,
        "request_id": "legacy-card",
        "tool_use_id": "legacy-card",
        "questions": QUESTIONS,
    }
    card = interactions.create_interaction("s1", "question", payload, None)
    assert "handoff_target_id" not in card["payload"]
    assert _confirmed_handoff_context("s1", payload) is None
    await sh._record(
        "s1", "Different move", {"handoff_id": "other-move", "status": "moved"}
    )
    assert _confirmed_handoff_context("s1", payload) is None
    await sh._record(
        "s1", "Attempt failed", {"handoff_id": "legacy-move", "status": "failed"}
    )
    assert _confirmed_handoff_context("s1", payload) is None
    await sh._record(
        "s1", "Move complete", {"handoff_id": "legacy-move", "status": "moved"}
    )
    assert _confirmed_handoff_context("s1", payload) == context
    with (
        patch(
            "app.core.user_question_handler.UserQuestionHandler._retire_native_question",
            AsyncMock(),
        ),
        patch("app.core.session_runtime.try_steer_pending", AsyncMock()),
        patch("app.core.session_runtime._broadcast_queue_after_start", AsyncMock()),
    ):
        assert await user_question_handler._answer_nonblocking(
            card, {"Which branch?": "Existing"}, None, human_input=True
        )
    [answer] = turns.get_queued_turns("s1")
    assert answer["prompt"].startswith(context + "\n\n")
    assert answer["prompt"].count("Your hand-off note:") == 1


@pytest.mark.parametrize("worker", ["source", "dev-gaming"])
@pytest.mark.parametrize("owner", ["source", "dev-gaming"])
def test_question_context_requires_designated_target_and_local_ownership(worker, owner):
    from app.core.user_question_handler import _confirmed_handoff_context

    _chat()
    handoff._accept_owner("s1", owner)
    payload = {
        "handoff_detached": True,
        "handoff_id": "this-move",
        "handoff_target_id": "dev-gaming",
        "handoff_context": "[hand-off] Moved with this note",
    }
    with patch(
        "app.network.engine.context", return_value=SimpleNamespace(device_id=worker)
    ):
        result = _confirmed_handoff_context("s1", payload)
    assert result == (
        payload["handoff_context"] if worker == owner == "dev-gaming" else None
    )


async def test_new_handoff_does_not_retry_interrupt_for_old_detached_card():
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
                        "name": "AskUser",
                        "input": {"questions": QUESTIONS},
                    },
                }
            ],
        )
        runtime = SimpleNamespace(
            interrupt=AsyncMock(side_effect=RuntimeError("interrupt not finished"))
        )
        old = sh.PendingHandoff("dev-gaming", "DesktopWorker", "old note", time.time())
        new = sh.PendingHandoff("dev-gaming", "DesktopWorker", "new note", time.time())
        state = {"running": True, "tasks": [], "queued": 0, "wake": False}
        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch("app.core.session_runtime.get_live_runtime", return_value=runtime),
        ):
            assert not await asyncio.wait_for(
                sh._prepare_questions("s1", state, old), timeout=3
            )
            assert turns.get_turn(turn["id"])["state"] == "interrupting"
            [card] = interactions.get_pending_interactions("s1")
            assert card["payload"]["handoff_id"] == old.handoff_id != new.handoff_id
            assert card["payload"]["handoff_turn_id"] == turn["id"]
            runtime.interrupt.assert_awaited_once()
            runtime.interrupt.reset_mock()
            # Same captured live turn and target, but a DIFFERENT hand-off.
            assert await asyncio.wait_for(
                sh._prepare_questions("s1", state, new), timeout=3
            )
            runtime.interrupt.assert_not_awaited()
    finally:
        callback.cancel()
        await asyncio.wait_for(
            asyncio.gather(callback, return_exceptions=True), timeout=3
        )


@pytest.mark.parametrize("outcome", ["failure", "timeout"])
@pytest.mark.parametrize("next_action", ["answer", "retry"])
async def test_failed_detach_does_not_claim_move_or_suppress_a_later_resume(
    outcome, next_action
):
    from app.core.user_question_handler import (
        _confirmed_handoff_context,
        user_question_handler,
    )

    _chat()
    callback = await _post(blocking=True)
    try:
        assert await _settle(lambda: interactions.get_pending_interactions("s1"))
        first = sh.request("s1", "DesktopWorker", "first move note")
        pending = sh._pending["s1"]
        if outcome == "timeout":
            pending.expires_at = time.time() + 0.04
        move = AsyncMock(side_effect=RuntimeError("target unavailable"))
        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch.object(sh, "_owner_now", return_value=None),
            patch.object(sh, "_busy", return_value=outcome == "timeout"),
            patch.object(sh, "POLL_S", 0.01),
            patch("app.api.sessions.move_session_to", move),
        ):
            result = await asyncio.wait_for(sh.run_pending("s1"), timeout=3)
        assert ("failed" if outcome == "failure" else "cancelled") in result
        assert await asyncio.wait_for(callback, timeout=3) == {
            "error": "No answers received"
        }
        [card] = interactions.get_pending_interactions("s1")
        assert card["payload"]["handoff_id"] == first["handoff_id"]
        assert card["payload"]["handoff_detached"] is True
        assert _confirmed_handoff_context("s1", card["payload"]) is None
        if next_action == "answer":
            with (
                patch(
                    "app.core.user_question_handler.UserQuestionHandler._retire_native_question",
                    AsyncMock(),
                ),
                patch("app.core.session_runtime.try_steer_pending", AsyncMock()),
                patch(
                    "app.core.session_runtime._broadcast_queue_after_start", AsyncMock()
                ),
            ):
                assert await user_question_handler._answer_nonblocking(
                    card, {"Which branch?": "Existing"}, None, human_input=True
                )
            [answer] = turns.get_queued_turns("s1")
            assert (
                answer["prompt"] == "Answer to your question:\nWhich branch?\nExisting"
            )
        else:
            second = sh.request("s1", "DesktopWorker", "second move note must arrive")
            assert second["handoff_id"] != first["handoff_id"]

            async def record_moved(sid, owner, content, data):
                await sh._record(sid, content, data)
                return True

            with (
                patch("app.network.runners.find", return_value=TARGET),
                patch("app.network.handoff.resume_carry", return_value=True),
                patch("app.api.sessions.move_session_to", AsyncMock()),
                patch.object(sh, "_tell", side_effect=record_moved),
                patch.object(sh, "deliver_held", AsyncMock()) as deliver,
            ):
                result = await asyncio.wait_for(sh.run_pending("s1"), timeout=3)
            assert "continues there" in result
            resume = deliver.await_args.args[3]
            assert (
                "Your hand-off note: second move note must arrive" in resume["prompt"]
            )
            # Even a later moved record must not confirm the earlier attempt.
            assert _confirmed_handoff_context("s1", card["payload"]) is None
    finally:
        callback.cancel()
        await asyncio.wait_for(
            asyncio.gather(callback, return_exceptions=True), timeout=3
        )


@pytest.mark.parametrize("difference", [None, "question", "options"])
async def test_defensive_fallback_uses_real_single_question_producer_shape(difference):
    from fluid.tools.builtins.ask_user import _render_message

    from app.core.user_question_handler import questions_from_tool_input

    _chat()
    # harness_adapter._translate builds exactly this input from QuestionRequest.
    # The blocking normalizer replaces the schema's question with rendered text.
    producer_input = {"question": _render_message(QUESTIONS[0]), "schema": QUESTIONS[0]}
    card_questions = questions_from_tool_input(producer_input)
    assert card_questions != QUESTIONS
    callback = await _post(blocking=True, tool_use_id=None, questions=card_questions)
    try:
        assert await _settle(lambda: interactions.get_pending_interactions("s1"))
        raw = {
            **QUESTIONS[0],
            "options": [dict(option) for option in QUESTIONS[0]["options"]],
        }
        if difference == "question":
            raw["question"] = "A different question?"
        elif difference == "options":
            raw["options"][0]["description"] = "A different option"
        turn = turns.enqueue_turn("s1", "user", prompt="ask me")
        turns.start_turn(turn["id"], None, "runner")
        session_events.append_events(
            "s1",
            [
                {
                    "type": "tool.use",
                    "turn_id": turn["id"],
                    "payload": {
                        "tool_use_id": "real-fluid-tool-id",
                        "name": "AskUser",
                        "input": {"questions": [raw]},
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
            await asyncio.wait_for(
                sh._prepare_questions(
                    "s1",
                    {"running": True, "tasks": [], "queued": 0, "wake": False},
                    pending,
                ),
                timeout=3,
            )
        [row] = interactions.get_pending_interactions("s1")
        assert row["payload"]["questions"] == card_questions
        if difference is None:
            runtime.interrupt.assert_awaited_once()
            assert row["payload"]["handoff_id"] == pending.handoff_id
            assert await asyncio.wait_for(callback, timeout=3) == {
                "error": "No answers received"
            }
        else:
            runtime.interrupt.assert_not_awaited()
            assert row["payload"]["request_id"] == "original"
            assert not callback.done()
    finally:
        callback.cancel()
        await asyncio.wait_for(
            asyncio.gather(callback, return_exceptions=True), timeout=3
        )


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
        patch(
            "app.network.runners.find", return_value={"capabilities": {"move_wakes": 3}}
        ),
        pytest.raises(sh.QuestionMoveUnsupported, match="Update the target worker"),
    ):
        await sh._prepare_questions(
            "s1",
            {"running": False, "tasks": []},
            sh.PendingHandoff("dev-gaming", "DesktopWorker", "", time.time()),
        )
    assert len(interactions.get_pending_interactions("s1")) == 1


async def test_unknown_receiver_capabilities_wait_then_move_without_new_input(caplog):
    _chat()
    await _post()
    sh.request("s1", "DesktopWorker")
    move = AsyncMock()
    with (
        patch("app.network.runners.find", side_effect=[{}, TARGET]) as find,
        patch("app.api.sessions.move_session_to", move),
        patch.object(sh, "_moved", AsyncMock(return_value="moved")),
        patch.object(sh, "_record", AsyncMock()),
        patch.object(sh, "POLL_S", 0.01),
    ):
        assert await asyncio.wait_for(sh.run_pending("s1"), timeout=3) == "moved"
    assert find.call_count == 2
    move.assert_awaited_once()
    assert "Update the target worker" not in caplog.text


@pytest.mark.parametrize(
    "failure", ["read", "before_detach", "after_detach", "interrupt", "publication"]
)
async def test_transient_question_failure_keeps_move_and_retries(failure, caplog):
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
                        "name": "AskUser",
                        "input": {"questions": QUESTIONS},
                    },
                }
            ],
        )
        sh.request("s1", "DesktopWorker", "retain this note")
        detach = sh._detach_question_rows
        detach_calls = 0

        def flaky_detach(*args, **kwargs):
            nonlocal detach_calls
            detach_calls += 1
            if detach_calls == 1 and failure == "before_detach":
                raise RuntimeError("transient before detach")
            replacements = detach(*args, **kwargs)
            if detach_calls == 1 and failure == "after_detach":
                raise RuntimeError("transient after detach committed")
            return replacements

        attempts = 0

        async def interrupt(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1 and failure == "interrupt":
                raise RuntimeError("transient provider interrupt")
            turns.settle_turn(turn["id"], "aborted")

        runtime = SimpleNamespace(interrupt=AsyncMock(side_effect=interrupt))
        from app.core.user_question_handler import user_question_handler

        post = user_question_handler.post_question

        async def flaky_post(**kwargs):
            if failure == "publication":
                raise RuntimeError("transient publication after detach committed")
            return await post(**kwargs)

        read_pending = interactions.get_pending_interactions
        reads = 0

        def flaky_read(sid):
            nonlocal reads
            reads += 1
            if reads == 1 and failure == "read":
                raise ValueError("transient pending-interaction read")
            return read_pending(sid)

        move = AsyncMock()
        with (
            patch("app.network.runners.find", return_value=TARGET),
            patch(
                "app.db.interactions.get_pending_interactions", side_effect=flaky_read
            ),
            patch("app.core.session_runtime.get_live_runtime", return_value=runtime),
            patch.object(sh, "_detach_question_rows", side_effect=flaky_detach),
            patch.object(
                user_question_handler, "post_question", side_effect=flaky_post
            ),
            patch.object(sh, "_record", AsyncMock()),
            patch.object(sh, "_tell", AsyncMock()),
            patch.object(sh, "deliver_held", AsyncMock()),
            patch.object(sh, "POLL_S", 0.01),
            patch("app.api.sessions.move_session_to", move),
        ):
            assert "question-card answer" in await asyncio.wait_for(
                sh.run_pending("s1"), timeout=3
            )
        move.assert_awaited_once()
        assert await asyncio.wait_for(callback, timeout=3) == {
            "error": "No answers received"
        }
        [replacement] = interactions.get_pending_interactions("s1")
        assert replacement["payload"]["handoff_detached"] is True
        assert "retain this note" in replacement["payload"]["handoff_context"]
        assert turns.get_turn(turn["id"])["state"] == "aborted"
        assert "will retry" in caplog.text
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


async def test_begin_refusal_logs_phase_and_reason(caplog):
    reason = (
        "Wait for this session's queued or active turn to finish before resuming here."
    )
    with (
        patch.object(handoff, "begin", side_effect=ValueError(reason)),
        patch.object(handoff, "_wake_queued"),
        pytest.raises(ValueError, match="queued or active turn"),
    ):
        await handoff._handle({"phase": "begin", "session_id": "s1"}, "test-peer")
    assert "Refused hand-off begin for session s1: " + reason in caplog.text


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
