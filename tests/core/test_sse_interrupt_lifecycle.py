"""A Stop belongs to one SSE turn, not every turn on its pooled adapter."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_background as qb
import app.core.query_engine as qe
from app.core import query_session_lifecycle as lifecycle
from app.core.provider import session_admission
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from tests.core.engine_harness import collect, make_state, stream_response
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches, scripted_events

SID = "sse-interrupt-lifecycle"


async def _turn():
    return await collect(qe.stream_query("hello", "claude-code", session_id=SID))


@pytest.mark.parametrize("hard_cancel", [False, True])
async def test_stop_then_completed_followup_then_stop_again(tmp_path, hard_cancel):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)

    async def stop():
        assert await lifecycle.interrupt_session(SID)
        if hard_cancel:
            raise asyncio.CancelledError()

    answer = AssistantMessage(content=[TextBlock(text="answer")], model="m")
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        for stopping in (True, False, True):
            state.adapter.receive_events = (
                scripted_events([answer, stop]) if stopping
                else stream_response([answer, ResultMessage(subtype="success")])
            )
            events = await _turn()
            assert events[-1]["type"] == ("interrupted" if stopping else "done")
            assert b.persist.await_args.kwargs["interrupted"] is stopping
            adhoc = b.run_sync_named("record_adhoc_turn")[-1][2]
            assert adhoc["outcome"] == ("aborted" if stopping else "completed")
            assert adhoc["stop_reason"] == ("interrupted" if stopping else None)
            assert pool.state is state
        assert state.adapter.interrupt.await_count == 2
        b.make_adapter.assert_not_called()


@pytest.mark.parametrize("phase", ["session", "options", "model", "send"])
async def test_stop_while_pooled_turn_is_starting_is_not_cleared(tmp_path, phase):
    """Session/options/model precede the marker; send guards a later reset.

    The send case cannot detect a reset at the marker: its Stop is later.
    It instead catches a reset after send_query returns, before event draining.
    These assert host flag ownership, not whether an engine stops pre-submit.
    """
    state = make_state(interrupt=AsyncMock())
    state.adapter.receive_events = stream_response([ResultMessage(subtype="success")])
    pool = RecordingPool(state)

    async def stop(*args, **kwargs):
        assert await lifecycle.interrupt_session(SID)

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        if phase == "session":
            run_sync = qe.run_sync

            async def session_read(fn, *args, **kwargs):
                if fn.__name__ == "get_session":
                    await stop()
                return await run_sync(fn, *args, **kwargs)

            stack.enter_context(patch.object(qe, "run_sync", new=session_read))
        elif phase == "options":
            stack.enter_context(patch("app.core.query_options.ensure_pending_checkout", side_effect=stop))
        elif phase == "model":
            state.connect_options = MagicMock(model="old", permission_mode="default", aih_provider_id=None)
            b.options.model = "new"
            state.adapter.set_model = AsyncMock(side_effect=stop)
        else:
            state.adapter.send_query = AsyncMock(side_effect=stop)
        events = await _turn()
    assert events[-1]["type"] == "interrupted"
    assert b.persist.await_args.kwargs["interrupted"] is True


def _background_patches(stack, boundaries, pool):
    """Share the same pooled state and persistence spy across background/SSE."""
    for name, value in (
        ("run_sync", qe.run_sync),
        ("sync_engine", boundaries.sync_engine),
        ("get_session_pool", MagicMock(return_value=pool)),
        ("is_subprocess_alive", MagicMock(return_value=True)),
        ("requires_reconnect", MagicMock(return_value=False)),
        ("_persist_turn_results", boundaries.persist),
        ("capture_usage_identity", AsyncMock(return_value="background-turn")),
        ("push_memory", AsyncMock()),
        ("schedule_context_usage_capture", MagicMock()),
        ("persist_turn_end_state", AsyncMock()),
        ("_store_background_task", MagicMock()),
    ):
        stack.enter_context(patch.object(qb, name, new=value))
    recorder = AsyncMock()
    stack.enter_context(patch(
        "app.core.turn_recorder.TurnRecorder.maybe_create",
        new=AsyncMock(return_value=recorder),
    ))
    return recorder


async def _background():
    lease = await session_admission.acquire(SID)
    try:
        await qb._run_background_query(
            SID, "hello", {"id": "claude-code"}, None, None, None, None, None, lease=lease,
        )
    finally:
        session_admission.close(lease)


async def _start():
    """``POST /conversation/start`` until its background task has ended."""
    await qb.start_background_query("hello", "claude-code", session_id=SID)
    await qb._pending_background_tasks[SID]


def _assert_saved(boundaries, interrupted):
    assert boundaries.persist.await_args.kwargs["interrupted"] is interrupted
    row = boundaries.run_sync_named("record_adhoc_turn")[-1][2]
    assert row["outcome"] == ("aborted" if interrupted else "completed")
    assert row["stop_reason"] == ("interrupted" if interrupted else None)


async def test_background_stop_then_sse_followup(tmp_path):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)

    async def stop():
        assert await lifecycle.interrupt_session(SID)

    answer = AssistantMessage(content=[TextBlock(text="answer")], model="m")
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        recorder = _background_patches(stack, b, pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        state.adapter.receive_events = scripted_events([answer, stop])
        await _background()
        assert recorder.on_event.await_args.args[0]["type"] == "interrupted"
        _assert_saved(b, True)
        assert state.interrupt_requested is False
        assert state.is_streaming is False
        state.adapter.receive_events = stream_response([answer, ResultMessage(subtype="success")])
        assert (await _turn())[-1]["type"] == "done"
        _assert_saved(b, False)
        assert pool.state is state


@pytest.mark.parametrize("background", [False, True])
async def test_stop_between_turns_then_answered_turn(tmp_path, background):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)
    answer = AssistantMessage(content=[TextBlock(text="answer")], model="m")
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        recorder = _background_patches(stack, b, pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        state.adapter.receive_events = stream_response([answer, ResultMessage(subtype="success")])
        assert (await _turn())[-1]["type"] == "done"
        assert await lifecycle.interrupt_session(SID)  # late/double Stop, idle pool
        if background:
            await _background()
            assert recorder.on_event.await_args.args[0]["type"] == "done"
        else:
            assert (await _turn())[-1]["type"] == "done"
        _assert_saved(b, False)
        assert state.interrupt_requested is False


async def test_stop_in_persist_window_poisons_next(tmp_path):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)
    fired = False

    async def persist(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            assert state.is_streaming is False
            assert await lifecycle.interrupt_session(SID)

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        b.persist.side_effect = persist
        answer = AssistantMessage(content=[TextBlock(text="answer")], model="m")
        state.adapter.receive_events = stream_response([answer, ResultMessage(subtype="success")])
        assert (await _turn())[-1]["type"] == "done"
        _assert_saved(b, False)
        assert (await _turn())[-1]["type"] == "done"
        _assert_saved(b, False)


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("refusal", ["options", "credentials"])
async def test_setup_stop_then_early_return_does_not_poison_followup(tmp_path, background, refusal):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)

    async def stop(*args, **kwargs):
        assert await lifecycle.interrupt_session(SID)

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        recorder = _background_patches(stack, b, pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        with ExitStack() as setup:
            setup.enter_context(patch("app.core.query_options.ensure_pending_checkout", side_effect=stop))
            if refusal == "options":
                setup.enter_context(patch("app.core.sdk_options.build_session_options", side_effect=ValueError("bad options")))
            else:
                setup.enter_context(patch("app.core.credential_gate.refusal_for_options_async", new=AsyncMock(return_value="not ready")))
            if background:
                await _background()
                assert recorder.on_event.await_args.args[0]["type"] == "error"
            else:
                assert (await _turn())[-1]["type"] == "error"
        assert state.is_streaming is False
        b.persist.assert_not_awaited()
        state.adapter.receive_events = stream_response([ResultMessage(subtype="success")])
        if background:
            await _background()
            assert recorder.on_event.await_args.args[0]["type"] == "done"
        else:
            assert (await _turn())[-1]["type"] == "done"
        _assert_saved(b, False)
        assert state.interrupt_requested is False


async def test_background_stop_during_recorder_setup_is_not_cleared(tmp_path):
    """The entry reset must precede even the recording wrapper's first await."""
    state = make_state(interrupt=AsyncMock())
    state.adapter.receive_events = stream_response([ResultMessage(subtype="success")])
    pool = RecordingPool(state)
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        recorder = _background_patches(stack, b, pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))

        async def create(*args):
            assert await lifecycle.interrupt_session(SID)
            return recorder

        stack.enter_context(patch("app.core.turn_recorder.TurnRecorder.maybe_create", side_effect=create))
        await _background()
        assert recorder.on_event.await_args.args[0]["type"] == "interrupted"
        _assert_saved(b, True)


@pytest.mark.parametrize("background", [False, True])
async def test_busy_turn_keeps_its_stop(tmp_path, background):
    state = make_state(interrupt=AsyncMock())
    state.is_streaming = True
    state.interrupt_requested = True
    pool = RecordingPool(state)
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        recorder = _background_patches(stack, b, pool)
        if background:
            await _background()
            assert recorder.on_event.await_args.args[0]["type"] == "error"
        else:
            assert (await _turn())[-1]["type"] == "error"
        assert state.interrupt_requested is True
        assert state.is_streaming is True
        state.adapter.send_query.assert_not_awaited()


# ── Whose Stop is it? The chat's lease decides ────────────────────────────────

ANSWER = AssistantMessage(content=[TextBlock(text="answer")], model="m")


@pytest.fixture
def chat(tmp_path):
    """An idle pooled chat with both routes ready, and a clean turn to answer."""
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.recorder = _background_patches(stack, b, pool)
        stack.enter_context(patch.object(qb, "get_profile", return_value={"id": "claude-code"}))
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        b.state = state
        state.adapter.receive_events = stream_response([ANSWER, ResultMessage(subtype="success")])
        yield b


def _ended_as(b, route, events=None):
    return events[-1]["type"] if route == "stream" else b.recorder.on_event.await_args.args[0]["type"]


async def _run(route):
    return await _turn() if route == "stream" else await _start()


async def _arrived(count=1):
    while session_admission._arriving.get(SID, 0) < count:
        await asyncio.sleep(0)


async def _a_holder(waiting_on):
    """Something a turn has to wait for, and how to let it go."""
    if waiting_on == "turn-ending":
        lease = await session_admission.acquire(SID)
        session_admission.turn_ended(lease)
    else:
        lease = await session_admission.begin_rewind(SID)
    return lease


@pytest.mark.parametrize("route", ["stream", "start"])
@pytest.mark.parametrize("waiting_on", ["turn-ending", "rewind"])
async def test_a_stop_while_a_turn_waits_for_the_chat_is_that_turns(chat, route, waiting_on):
    holder = await _a_holder(waiting_on)
    turn = asyncio.create_task(_run(route))
    await _arrived()
    assert await lifecycle.interrupt_session(SID)
    session_admission.close(holder)
    assert _ended_as(chat, route, await turn) == "interrupted"
    _assert_saved(chat, True)
    assert chat.state.interrupt_requested is False


@pytest.mark.parametrize("route", ["stream", "start"])
async def test_a_stop_before_a_waiting_turn_arrived_is_not_its_stop(chat, route):
    holder = await _a_holder("turn-ending")
    assert await lifecycle.interrupt_session(SID)
    turn = asyncio.create_task(_run(route))
    await _arrived()
    assert chat.state.interrupt_requested is False
    session_admission.close(holder)
    assert _ended_as(chat, route, await turn) == "done"
    _assert_saved(chat, False)


async def test_a_later_arrival_leaves_a_waiting_turns_stop(chat):
    holder = await _a_holder("turn-ending")
    first = asyncio.create_task(_turn())
    await _arrived()
    assert await lifecycle.interrupt_session(SID)
    second = asyncio.create_task(_turn())
    await _arrived(2)
    assert chat.state.interrupt_requested is True
    session_admission.close(holder)
    assert (await first)[-1]["type"] == "interrupted"
    assert [e["message"] for e in await second] == [session_admission.SessionBusyError.MESSAGE]


@pytest.mark.parametrize("left_by", ["cancelled", "timed-out"])
async def test_a_turn_that_stopped_waiting_owns_no_stop(chat, monkeypatch, left_by):
    monkeypatch.setattr(session_admission, "_ENDING_WAIT_SECONDS", 0.05)
    holder = await _a_holder("turn-ending")
    waiter = asyncio.create_task(session_admission.acquire(SID))
    await _arrived()
    if left_by == "cancelled":
        waiter.cancel()
    with pytest.raises(asyncio.CancelledError if left_by == "cancelled" else session_admission.SessionBusyError):
        await waiter
    session_admission.close(holder)
    assert await lifecycle.interrupt_session(SID)
    assert (await _turn())[-1]["type"] == "done"
    _assert_saved(chat, False)


async def test_a_rewind_holding_the_chat_owns_no_stop(chat):
    rewind = await _a_holder("rewind")
    session_admission.take_for_rewind(rewind)
    assert await lifecycle.interrupt_session(SID)
    turn = asyncio.create_task(_turn())
    await _arrived()
    session_admission.close(rewind)
    assert (await turn)[-1]["type"] == "done"
    _assert_saved(chat, False)


async def test_a_stop_after_the_background_route_took_the_chat_is_its_own(chat):
    """Between the lease and the task's first step, where a reset at the task's top erases it."""
    reads = qb.run_sync

    async def read(fn, *args, **kwargs):
        if fn.__name__ == "get_session" and not chat.state.interrupt_requested:
            assert await lifecycle.interrupt_session(SID)
        return await reads(fn, *args, **kwargs)

    with patch.object(qb, "run_sync", new=read):
        await _start()
    assert _ended_as(chat, "start") == "interrupted"
    _assert_saved(chat, True)


@pytest.mark.parametrize("first", ["stream", "start"])
@pytest.mark.parametrize("second", ["stream", "start"])
async def test_a_second_turn_during_setup_leaves_the_first_turns_stop(chat, first, second):
    """Issue 1107's related case: the lease refuses the second turn before any reset."""
    in_setup, go_on = asyncio.Event(), asyncio.Event()

    async def setup(*args, **kwargs):
        in_setup.set()
        await go_on.wait()

    with patch("app.core.query_options.ensure_pending_checkout", side_effect=setup):
        turn = asyncio.create_task(_run(first))
        await in_setup.wait()
        assert chat.state.is_streaming is False
        assert await lifecycle.interrupt_session(SID)

        if second == "stream":
            assert [e["message"] for e in await _turn()] == [session_admission.SessionBusyError.MESSAGE]
        else:
            with pytest.raises(session_admission.SessionBusyError):
                await qb.start_background_query("hello", "claude-code", session_id=SID)
        assert chat.state.interrupt_requested is True

        go_on.set()
        assert _ended_as(chat, first, await turn) == "interrupted"
    _assert_saved(chat, True)
    assert chat.state.interrupt_requested is False

    # The Stop went with its turn: the next one answers.
    assert (await _turn())[-1]["type"] == "done"
    _assert_saved(chat, False)


async def test_a_stop_goes_with_the_engine_a_rewind_removes(chat):
    """The flag lives on the engine, and that engine never runs the waiting turn."""
    rewind = await _a_holder("rewind")
    turn = asyncio.create_task(_turn())
    await _arrived()
    assert await lifecycle.interrupt_session(SID)
    await chat.pool.remove(SID)
    chat.make_adapter.return_value.receive_events = stream_response(
        [ANSWER, ResultMessage(subtype="success")]
    )
    session_admission.close(rewind)
    assert (await turn)[-1]["type"] == "done"
    _assert_saved(chat, False)
    chat.make_adapter.assert_called_once()
