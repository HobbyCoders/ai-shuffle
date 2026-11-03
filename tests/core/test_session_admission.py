"""One turn at a time per chat in this process, and no silent success after a takeover.

The database fence stops a replaced engine's writes; these cases stop the
replacement from happening to a live turn in the first place, keep a rewind able
to stop one, and fail a turn that another install took over. The routes' side of
the lease is in ``test_busy_write_guard_engine``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.provider import session_admission as admission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.session_admission import SessionBusyError, SessionReplacedError

SID = "ses_admission"


# ─── the lease ───────────────────────────────────────────────────────────────


async def test_a_second_turn_is_refused_until_the_first_is_over():
    first = await admission.acquire(SID)
    with pytest.raises(SessionBusyError):
        await asyncio.wait_for(admission.acquire(SID), 1)  # at once, not after a wait
    admission.close(first)
    admission.close(await admission.acquire(SID))


async def test_two_requests_cannot_both_pass_because_acquire_never_yields_when_free():
    coroutine = admission.acquire(SID)
    with pytest.raises(StopIteration) as finished:
        coroutine.send(None)  # ran to its end without giving the loop a turn
    with pytest.raises(SessionBusyError):
        await admission.acquire(SID)
    admission.close(finished.value.value)


async def test_chats_do_not_block_each_other():
    admission.close(await admission.acquire("another"))
    held = await admission.acquire(SID)
    admission.close(await admission.acquire("another"))
    admission.close(held)


async def test_a_message_after_a_turns_end_waits_for_its_route_to_finish():
    lease = await admission.acquire(SID)
    admission.turn_ended(lease)
    next_turn = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0.01)
    assert not next_turn.done(), "the old route may still be saving"
    admission.close(lease)
    admission.close(await asyncio.wait_for(next_turn, 5))


async def test_a_route_stuck_after_its_end_refuses_the_next_message_after_a_while(monkeypatch):
    monkeypatch.setattr(admission, "_ENDING_WAIT_SECONDS", 0.01)
    lease = await admission.acquire(SID)
    admission.turn_ended(lease)
    with pytest.raises(SessionBusyError):
        await admission.acquire(SID)
    admission.close(lease)


async def test_closing_twice_cannot_free_a_later_holder():
    lease = await admission.acquire(SID)
    admission.close(lease)
    later = await admission.acquire(SID)
    admission.close(lease)
    with pytest.raises(SessionBusyError):
        await admission.acquire(SID)
    admission.close(later)


async def test_a_rewind_takes_the_chat_and_the_stopped_turn_cannot_free_it():
    turn = await admission.acquire(SID)
    rewind = await admission.begin_rewind(SID)
    assert admission.take_for_rewind(rewind) is turn
    assert turn.stopped is True
    admission.turn_ended(turn)
    admission.close(turn)
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0)
    assert not waiting.done(), "the rewind still holds the chat"
    admission.close(rewind)
    admission.close(await asyncio.wait_for(waiting, 5))


async def test_a_turn_waiting_on_a_rewind_is_refused_if_another_turn_gets_there_first():
    rewind = await admission.begin_rewind(SID)
    admission.take_for_rewind(rewind)
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0)
    admission.close(rewind)
    winner = await admission.acquire(SID)
    with pytest.raises(SessionBusyError):
        await waiting
    admission.close(winner)


async def test_a_turn_waits_for_a_rewind_that_has_not_taken_the_chat_yet():
    rewind = await admission.begin_rewind(SID)
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0.01)
    assert not waiting.done(), "it would be stopped by the rewind it raced"
    admission.close(rewind)
    admission.close(await asyncio.wait_for(waiting, 5))


async def test_session_cleanup_is_a_turns_only_while_no_successor_can_exist():
    turn = await admission.acquire(SID)
    assert admission.holds_chat(turn)
    rewind = await admission.begin_rewind(SID)
    admission.take_for_rewind(rewind)
    assert admission.holds_chat(turn), "the rewind is still running"
    admission.close(rewind)
    assert not admission.holds_chat(turn), "a new turn may start at any moment"
    successor = await admission.acquire(SID)
    assert not admission.holds_chat(turn)
    assert admission.holds_chat(successor)
    admission.close(successor)


async def test_a_later_rewind_gives_an_older_stopped_turn_no_cleanup_rights():
    """The turn outlasted the rewind that took the chat. Another rewind beginning, which
    may yet be rejected, changes nothing: the new turn still holds the chat."""
    turn = await admission.acquire(SID)
    first = await admission.begin_rewind(SID)
    admission.take_for_rewind(first)
    admission.close(first)
    assert not admission.holds_chat(turn)

    successor = await admission.acquire(SID)
    second = await admission.begin_rewind(SID)
    assert not admission.holds_chat(turn), "the second rewind did not displace that turn"
    assert admission.holds_chat(successor)

    admission.take_for_rewind(second)
    assert not admission.holds_chat(turn)
    assert admission.holds_chat(successor), "the rewind that took it is still running"
    admission.close(second)
    assert not admission.holds_chat(successor)
    admission.close(successor)
    admission.close(turn)


@pytest.mark.parametrize("waiter", [admission.acquire, admission.begin_rewind])
async def test_waiting_for_a_rewind_that_never_finishes_ends_in_the_busy_error(monkeypatch, waiter):
    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 0.01)
    rewind = await admission.begin_rewind(SID)
    with pytest.raises(SessionBusyError):
        await waiter(SID)
    assert admission._rewinds[SID] is rewind, "it can still act, so it is not let go"
    assert SID not in admission._holders

    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 5)
    waiting = asyncio.ensure_future(waiter(SID))
    await asyncio.sleep(0.01)
    admission.close(rewind)
    admission.close(await asyncio.wait_for(waiting, 5))


async def _next_rewind(rewind):
    admission.close(rewind)
    return await admission.begin_rewind(SID)


async def _next_ending_turn(turn):
    admission.close(turn)
    turn = await admission.acquire(SID)
    admission.turn_ended(turn)
    return turn


async def _an_ending_turn():
    turn = await admission.acquire(SID)
    admission.turn_ended(turn)
    return turn


@pytest.mark.parametrize("waiter", [admission.acquire, admission.begin_rewind])
async def test_one_limit_covers_every_rewind_a_waiter_waits_behind(monkeypatch, waiter):
    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 0.5)
    rewind = await admission.begin_rewind(SID)
    waiting = asyncio.ensure_future(waiter(SID))
    for _ in range(3):  # each finishes well inside the limit, together well past it
        await asyncio.sleep(0.2)
        rewind = await _next_rewind(rewind)
    assert waiting.done(), "the third holder changed nothing: the limit passed during it"
    with pytest.raises(SessionBusyError):
        await waiting
    assert admission._rewinds[SID] is rewind, "the rewind running can still act, so it stays held"
    assert not rewind.exited.is_set()
    admission.close(rewind)


async def test_one_limit_covers_every_ending_turn_a_message_waits_behind(monkeypatch):
    monkeypatch.setattr(admission, "_ENDING_WAIT_SECONDS", 0.5)
    turn = await _an_ending_turn()
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0.2)
    turn = await _next_ending_turn(turn)
    await asyncio.sleep(0.2)
    turn = await _next_ending_turn(turn)
    await asyncio.sleep(0.2)
    assert waiting.done()
    with pytest.raises(SessionBusyError):
        await waiting
    admission.close(turn)


async def test_a_rewind_in_the_way_gives_a_waiter_the_longer_limit_from_when_it_arrived(monkeypatch):
    monkeypatch.setattr(admission, "_ENDING_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 2.0)
    turn = await _an_ending_turn()
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0.6)
    admission.close(turn)
    rewind = await admission.begin_rewind(SID)
    await asyncio.sleep(0.8)
    assert not waiting.done(), "past the short limit, within the long one"
    await asyncio.sleep(0.8)
    assert waiting.done(), "the long limit counts from arriving, not from meeting the rewind"
    with pytest.raises(SessionBusyError):
        await waiting
    admission.close(rewind)


async def test_a_rewind_beginning_during_a_wait_gives_that_waiter_the_longer_limit(monkeypatch):
    """Without the ending turn leaving first: the waiter must look again when a rewind begins."""
    monkeypatch.setattr(admission, "_ENDING_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 0.8)
    turn = await _an_ending_turn()
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0.05)
    rewind = await admission.begin_rewind(SID)
    await asyncio.sleep(0.3)
    assert not waiting.done(), "past the short limit, with the rewind still holding the chat"
    admission.close(turn)
    admission.close(rewind)
    admission.close(await waiting)


async def test_a_waiter_looks_again_when_the_holder_it_waited_for_is_replaced(monkeypatch):
    """The rewind took the chat and finished, and the turn it took has not left yet."""
    turn = await _an_ending_turn()
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0)
    rewind = await admission.begin_rewind(SID)
    admission.take_for_rewind(rewind)
    admission.close(rewind)
    admission.close(await asyncio.wait_for(waiting, 1))
    admission.close(turn)


async def test_a_cancelled_wait_for_a_rewind_holds_nothing():
    rewind = await admission.begin_rewind(SID)
    admission.take_for_rewind(rewind)
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    admission.close(rewind)
    admission.close(await admission.acquire(SID))


async def _events_of(stream) -> list:
    return [event async for event in stream]


async def test_a_refused_stream_is_the_busy_frame_and_never_starts_its_turn():
    held = await admission.acquire(SID)
    turn = MagicMock(side_effect=AssertionError("must not start"))
    assert await _events_of(admission.stream_turn(SID, turn)) == [
        {"type": "error", "message": str(SessionBusyError())},
    ]
    turn.assert_not_called()
    admission.close(held)


@pytest.mark.parametrize("failure", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_a_stream_that_fails_lets_the_chat_go(failure):
    leases = []

    async def turn(lease):
        leases.append(lease)
        raise failure
        yield  # pragma: no cover

    with pytest.raises(type(failure)):
        await _events_of(admission.stream_turn(SID, turn))
    assert leases[0].exited.is_set()
    admission.close(await admission.acquire(SID))


async def test_a_turn_whose_reader_left_at_a_frame_runs_on_and_holds_the_chat():
    proceed, order = asyncio.Event(), []

    async def turn(_lease):
        yield {"type": "init"}
        await proceed.wait()
        order.append("saved")
        yield {"type": "done"}

    stream = admission.stream_turn(SID, turn)
    assert await stream.__anext__() == {"type": "init"}
    await stream.aclose()

    with pytest.raises(SessionBusyError):
        await admission.acquire(SID)
    proceed.set()
    for _ in range(100):
        if SID not in admission._holders:
            break
        await asyncio.sleep(0.01)
    assert order == ["saved"]
    admission.close(await admission.acquire(SID))


async def test_a_turn_that_fails_with_nobody_reading_still_lets_the_chat_go():
    async def turn(_lease):
        yield {"type": "init"}
        raise RuntimeError("no")

    stream = admission.stream_turn(SID, turn)
    await stream.__anext__()
    await stream.aclose()
    for _ in range(100):
        if SID not in admission._holders:
            break
        await asyncio.sleep(0.01)
    admission.close(await admission.acquire(SID))


async def _relayed(frames):
    for frame in frames:
        if isinstance(frame, Exception):
            raise frame
        if isinstance(frame, asyncio.Event):
            await frame.wait()
        else:
            yield frame


async def test_a_stream_relays_frames_and_failures_to_a_reader_that_stays():
    assert await _events_of(admission.outlive_reader(_relayed([1, 2]))) == [1, 2]
    stream = admission.outlive_reader(_relayed([1, RuntimeError("no")]))
    assert await stream.__anext__() == 1
    with pytest.raises(RuntimeError, match="no"):
        await stream.__anext__()


@pytest.mark.parametrize("leaves", ["cancelled", "closed"])
async def test_a_stream_keeps_running_when_its_reader_leaves(leaves):
    proceed, order = asyncio.Event(), []

    async def frames():
        yield 1
        await proceed.wait()
        order.append("reached the end")

    stream = admission.outlive_reader(frames())
    assert await stream.__anext__() == 1
    if leaves == "closed":
        await stream.aclose()
    else:
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    proceed.set()
    await asyncio.sleep(0.01)
    assert order == ["reached the end"]


@pytest.fixture
def relays(monkeypatch):
    """Every relay made, to look at what it still holds."""
    made = []

    class Watched(admission._Relay):
        def __init__(self):
            super().__init__()
            made.append(self)

    monkeypatch.setattr(admission, "_Relay", Watched)
    return made


def _held(relay) -> list:
    return list(relay._queue._queue)


async def _a_turn(lengths, proceed, order):
    yield "x"
    await proceed.wait()
    for length in lengths:
        yield "y" * length
        order.append("produced")
        await asyncio.sleep(0)
    order.append("reached the end")


@pytest.mark.parametrize("leaves", ["cancelled", "closed"])
async def test_a_reader_that_left_is_forgotten_and_nothing_more_is_kept(relays, leaves):
    proceed, order = asyncio.Event(), []
    stream = admission.outlive_reader(_a_turn([10] * 2048, proceed, order))
    assert await stream.__anext__() == "x"
    if leaves == "closed":
        await stream.aclose()
    else:
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    proceed.set()
    await _until(lambda: "reached the end" in order)
    assert order.count("produced") == 2048
    assert _held(relays[0]) == []


async def test_a_reader_leaving_drops_what_it_had_not_read(relays):
    proceed, order = asyncio.Event(), []
    stream = admission.outlive_reader(_a_turn([10] * 5, proceed, order))
    assert await stream.__anext__() == "x"
    proceed.set()
    await _until(lambda: "reached the end" in order)
    assert len(_held(relays[0])) == 6
    await stream.aclose()
    assert _held(relays[0]) == []


async def test_a_reader_that_stopped_reading_cannot_stall_or_bloat_the_turn(relays, monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 10_000)
    proceed, order, peak = asyncio.Event(), [], [0]
    stream = admission.outlive_reader(_a_turn([1000] * 200, proceed, order))
    assert await stream.__anext__() == "x"
    proceed.set()

    async def watch():
        while "reached the end" not in order:
            peak[0] = max(peak[0], relays[0]._size)
            await asyncio.sleep(0)

    await asyncio.wait_for(watch(), 5)  # it never waited for this reader
    assert order.count("produced") == 200
    assert 0 < peak[0] <= 10_000
    assert len(_held(relays[0])) == 1, "only the news that it was cut off"

    assert (await stream.__anext__()) == {
        "type": "error", "message": admission._FELL_BEHIND_MESSAGE,
    }
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_a_reader_cut_off_does_not_wait_for_a_turn_still_running(monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 1000)
    never = asyncio.Event()

    async def turn():
        yield "x"
        await asyncio.sleep(0)
        for _ in range(10):
            yield "y" * 500
        await never.wait()

    stream = admission.outlive_reader(turn())
    assert await stream.__anext__() == "x"
    await asyncio.sleep(0.01)
    assert (await stream.__anext__())["type"] == "error"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), 1)
    never.set()


async def test_a_reader_cut_off_leaves_the_turn_holding_the_chat_until_it_ends(monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 1000)
    proceed, saved = asyncio.Event(), []

    async def turn(_lease):
        yield "x"
        await asyncio.sleep(0)
        for _ in range(10):
            yield "y" * 500
        await proceed.wait()
        saved.append(True)

    stream = admission.outlive_reader(admission.stream_turn(SID, turn))
    assert await stream.__anext__() == "x"
    await asyncio.sleep(0.01)
    assert (await stream.__anext__())["type"] == "error"
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    await asyncio.sleep(0.01)
    assert SID in admission._holders
    proceed.set()
    await _until(lambda: SID not in admission._holders)
    assert saved == [True]


async def test_many_tiny_frames_count_toward_the_limit(relays, monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 10_000)
    proceed, order = asyncio.Event(), []
    stream = admission.outlive_reader(_a_turn([0] * 1000, proceed, order))
    assert await stream.__anext__() == "x"
    proceed.set()
    await _until(lambda: "reached the end" in order)
    assert (await stream.__anext__())["type"] == "error"


async def test_a_slow_reader_within_the_limit_gets_every_frame_in_order(monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 100_000)
    wanted = [f"frame {n}" for n in range(100)]
    stream = admission.outlive_reader(_relayed(wanted))
    assert await stream.__anext__() == wanted[0]
    await asyncio.sleep(0.01)  # it fell behind for a moment
    assert [frame async for frame in stream] == wanted[1:]


async def test_reading_makes_room_again(relays, monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 2000)
    gate = asyncio.Event()

    async def turn():
        for _ in range(3):
            yield "y" * 800
            await gate.wait()
            gate.clear()

    stream = admission.outlive_reader(turn())
    for _ in range(3):
        assert await stream.__anext__() == "y" * 800
        gate.set()
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_a_frame_larger_than_the_limit_reaches_a_reader_that_has_caught_up(monkeypatch):
    monkeypatch.setattr(admission, "_READER_BUFFER_BYTES", 100)
    big = "y" * 10_000
    gate = asyncio.Event()

    async def turn():
        yield "x"
        await gate.wait()
        yield big

    stream = admission.outlive_reader(turn())
    assert await stream.__anext__() == "x"
    gate.set()
    assert await _events_of(stream) == [big]


async def test_a_frame_that_cannot_be_measured_does_not_stop_the_turn():
    circular = {}
    circular["self"] = circular
    assert await _events_of(admission.outlive_reader(_relayed([circular, {(1, 2): "tuple key"}, 3]))) == [
        circular, {(1, 2): "tuple key"}, 3,
    ]


def test_a_stopping_app_ends_those_turns_while_it_can_still_save():
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert source.index("session_admission.stop_unattended()") < source.index("await shutdown_services()")


async def test_a_stopping_app_ends_turns_nobody_is_reading_so_they_save():
    saved = []

    async def turn(_lease):
        try:
            yield {"type": "init"}
            await asyncio.Event().wait()
        finally:
            saved.append(True)

    stream = admission.outlive_reader(admission.stream_turn(SID, turn))
    await stream.__anext__()
    await stream.aclose()
    await admission.stop_unattended()
    assert saved == [True]
    assert SID not in admission._holders


# ─── a turn another install takes over ───────────────────────────────────────


def _result_event():
    from fluid.events.model import TurnComplete

    return TurnComplete(session_id=SID, turn_id="t1", seq=3, stop_reason="end_turn", num_model_calls=1)


def _delta_event():
    from fluid.events.model import TextDelta

    return TextDelta(session_id=SID, turn_id="t1", seq=2, index=0, text="partial")


class _Store:
    """The ownership surface of ``PgFluidStore``."""

    def __init__(self, *, lost=False, at_end=False):
        self.lost, self.at_end = lost, at_end

    def lost_ownership(self):
        return self.lost

    async def check_ownership(self):
        return self.lost or self.at_end

    async def release_resume_memory(self):
        return None


async def _adapter_streaming(store, events):
    adapter = HarnessAdapter()

    async def stream(since_seq=0):
        for event in events:
            yield event

    adapter._engine = SimpleNamespace(events=stream, submit=AsyncMock())
    adapter._store, adapter._connected = store, True
    adapter._sdk_session_id = SID
    adapter._turn_floor = 0
    return adapter


async def _drain(adapter) -> list:
    return [message async for message in adapter.receive_events()]


async def test_write_refused_mid_turn_fails_the_turn_and_stops_it():
    adapter = await _adapter_streaming(_Store(lost=True), [_delta_event(), _result_event()])

    with pytest.raises(SessionReplacedError):
        await _drain(adapter)

    adapter._engine.submit.assert_awaited_once()


async def test_turn_stops_at_the_first_event_after_a_refused_write():
    """It does not run on to its end, spending tools and tokens on unsaved work."""

    def more():
        raise AssertionError("the turn ran on after losing the session")

    class Events(list):
        def __iter__(self):
            yield _delta_event()
            more()

    adapter = await _adapter_streaming(_Store(lost=True), Events())
    with pytest.raises(SessionReplacedError):
        await _drain(adapter)


async def test_takeover_seen_only_at_the_end_fails_the_turn_instead_of_success():
    """The last writes may not have been refused yet: this asks the database."""
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(at_end=True), [_result_event()])
    seen: list = []
    with pytest.raises(SessionReplacedError):
        async for message in adapter.receive_events():
            seen.append(message)
    assert not any(isinstance(message, ResultMessage) for message in seen)


async def test_the_failure_tells_the_user_what_happened():
    assert "another running copy" in str(SessionReplacedError())
    assert "not saved" in str(SessionReplacedError())


async def test_a_stop_that_does_not_get_through_still_fails_the_turn():
    adapter = await _adapter_streaming(_Store(lost=True), [_result_event()])
    adapter._engine.submit = AsyncMock(side_effect=RuntimeError("closed"))
    with pytest.raises(SessionReplacedError):
        await _drain(adapter)


async def test_owner_finishes_normally():
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(), [_result_event()])
    assert isinstance((await _drain(adapter))[-1], ResultMessage)
    adapter._engine.submit.assert_not_awaited()


async def test_a_client_this_process_closed_has_no_takeover_to_report():
    """A rewind or a forced stop ended it on purpose; its stream just finishes."""
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(lost=True), [_result_event()])
    adapter._store = None
    assert isinstance((await _drain(adapter))[-1], ResultMessage)


async def test_a_client_closed_while_its_check_ran_has_no_takeover_to_report():
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(), [_result_event()])

    async def closed_meanwhile():
        adapter._store = None
        return True

    adapter._store.check_ownership = closed_meanwhile
    assert isinstance((await _drain(adapter))[-1], ResultMessage)


async def test_a_failed_ownership_check_does_not_fail_a_finished_turn():
    from app.core.provider.events import ResultMessage

    store = _Store()
    store.check_ownership = AsyncMock(side_effect=RuntimeError("database down"))
    adapter = await _adapter_streaming(store, [_result_event()])
    assert isinstance((await _drain(adapter))[-1], ResultMessage)


# ─── rewind ──────────────────────────────────────────────────────────────────


class _Rewinding:
    """``execute_rewind`` with storage stubbed, recording what it did and when."""

    def __init__(self, monkeypatch, *, pooled=True, rewind=None):
        from app.api import commands
        from app.core.jsonl_rewind import RewindResult
        from app.core.provider import fluid_transcript

        self.commands, self.steps = commands, []
        self.stop = None

        async def remove(session_id):
            self.steps.append("evicted")

        self.pool = MagicMock(contains=MagicMock(return_value=pooled), remove=remove)

        def default_rewind(*_args, stop, **_kwargs):
            self.stop = stop
            self.steps.append("rewound")
            return RewindResult(
                success=True, message="moved", writer_generation=7, messages_removed=1,
            )

        # Once, for the whole test: rewinds here overlap, so a scope per call would
        # end out of order and leave the real function replaced for later tests.
        monkeypatch.setattr("app.api.rewind_owner.forward_rewind", AsyncMock(return_value=None))
        monkeypatch.setattr(commands, "get_session_pool", lambda: self.pool)
        monkeypatch.setattr(commands, "database", MagicMock(get_session=MagicMock(return_value={
            "id": "app-id", "sdk_session_id": SID, "project_id": None, "turn_count": 0,
        })))
        monkeypatch.setattr(commands, "get_working_dir_for_project", lambda *_ids: "/tmp")
        monkeypatch.setattr(commands, "invalidate_parse_cache", lambda _id: None, raising=False)
        monkeypatch.setattr(fluid_transcript, "is_fluid_session", lambda _id: True)
        monkeypatch.setattr(fluid_transcript, "rewind_fluid_session", rewind or default_rewind)
        self.reseed = MagicMock(return_value=fluid_transcript.ReseedResult(status="skipped"))
        monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", self.reseed)

    async def run(self, target="n1"):
        from app.api.commands import RewindRequestV2

        return await self.commands.execute_rewind(
            "app-id", RewindRequestV2(target_uuid=target, restore_chat=True),
        )

    def start(self):
        return asyncio.ensure_future(self.run())


async def _until(condition, *, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never came true")


async def test_rewind_stops_a_running_reply_and_waits_for_it_to_save(monkeypatch):
    rewinding = _Rewinding(monkeypatch)
    running = await admission.acquire("app-id")

    rewind = rewinding.start()
    await _until(lambda: rewinding.steps == ["evicted"])
    await asyncio.sleep(0.05)
    assert rewinding.steps == ["evicted"], "the stopped turn is still saving"
    assert running.stopped is True

    admission.turn_ended(running)
    await asyncio.sleep(0.05)
    assert rewinding.steps == ["evicted"], "ending the turn is not the end of its save"

    admission.close(running)
    response = await asyncio.wait_for(rewind, 5)
    assert response.success is True and response.chat_rewound is True
    assert rewinding.steps == ["evicted", "rewound"]
    admission.close(await admission.acquire("app-id"))


async def test_a_message_sent_during_a_rewind_lands_on_the_rewound_chat(monkeypatch):
    rewinding = _Rewinding(monkeypatch)
    running = await admission.acquire("app-id")
    rewind = rewinding.start()
    await _until(lambda: rewinding.steps == ["evicted"])

    message = asyncio.ensure_future(admission.acquire("app-id"))
    admission.close(running)
    await asyncio.wait_for(rewind, 5)
    admission.close(await asyncio.wait_for(message, 5))
    assert rewinding.steps == ["evicted", "rewound"]


async def test_rewind_goes_on_when_the_stopped_turn_never_ends(monkeypatch):
    """A client not reading its stream must not be able to block a rewind forever."""
    rewinding = _Rewinding(monkeypatch)
    monkeypatch.setattr(rewinding.commands, "_REWIND_STOP_WAIT_SECONDS", 0.05)
    stuck = await admission.acquire("app-id")
    assert (await rewinding.run()).success is True
    assert rewinding.steps == ["evicted", "rewound"]
    admission.close(await admission.acquire("app-id"))
    admission.close(stuck)


async def test_rewind_of_an_idle_chat_does_not_wait(monkeypatch):
    rewinding = _Rewinding(monkeypatch, pooled=False)
    assert (await asyncio.wait_for(rewinding.run(), 2)).success is True
    assert rewinding.steps == ["rewound"]
    rewinding.reseed.assert_called_once()
    assert rewinding.reseed.call_args.kwargs["generation"] == 7


async def test_rewind_holds_the_chat_until_its_reseed_is_done(monkeypatch):
    rewinding = _Rewinding(monkeypatch, pooled=False)
    seen = []

    def reseed(*_args, **_kwargs):
        from app.core.provider.fluid_transcript import ReseedResult

        seen.append("app-id" in admission._rewinds)
        return ReseedResult(status="skipped")

    rewinding.reseed.side_effect = reseed
    await rewinding.run()
    assert seen == [True]
    assert "app-id" not in admission._holders


async def test_a_rewind_that_is_rejected_does_not_touch_the_running_reply(monkeypatch):
    rewinding = _Rewinding(monkeypatch)
    running = await admission.acquire("app-id")
    response = await rewinding.run(target="db-1")
    assert response.success is False
    assert running.stopped is False and rewinding.steps == []
    assert admission._holders["app-id"] is running
    admission.close(running)


async def test_a_failing_rewind_still_lets_the_chat_go(monkeypatch):
    rewinding = _Rewinding(monkeypatch, pooled=False, rewind=MagicMock(side_effect=OSError("disk")))
    with pytest.raises(OSError):
        await rewinding.run()
    admission.close(await admission.acquire("app-id"))


async def test_a_rewind_refused_by_one_that_hangs_says_so_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(admission, "_REWIND_WAIT_SECONDS", 0.01)
    rewinding = _Rewinding(monkeypatch)
    hung = await admission.begin_rewind("app-id")
    result = await rewinding.run()
    assert result.success is False and "still running" in result.error
    assert rewinding.steps == []
    assert admission._rewinds["app-id"] is hung
    admission.close(hung)


async def test_a_rewind_whose_caller_left_finishes_before_anything_else_touches_the_chat(monkeypatch):
    entered, proceed = threading.Event(), threading.Event()

    def slow_rewind(*_args, stop, **_kwargs):
        from app.core.jsonl_rewind import RewindResult

        entered.set()
        proceed.wait(5)
        assert not stop.is_set(), "the rewind is not abandoned with its caller"
        return RewindResult(success=True, message="moved", writer_generation=7, messages_removed=1)

    rewinding = _Rewinding(monkeypatch, pooled=False, rewind=slow_rewind)
    rewind = rewinding.start()
    await asyncio.to_thread(entered.wait, 5)
    rewind.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rewind

    next_turn = asyncio.ensure_future(admission.acquire("app-id"))
    await asyncio.sleep(0.01)
    assert not next_turn.done(), "it holds the chat until its claim, cut and re-seed are done"
    rewinding.reseed.assert_not_called()

    proceed.set()
    admission.close(await asyncio.wait_for(next_turn, 5))
    rewinding.reseed.assert_called_once()


async def test_overlapping_rewinds_run_one_after_the_other_and_a_new_turn_comes_last(monkeypatch):
    """The second validates against what the first left, and nothing of the first is
    left to run once the second and a new turn have started."""
    from app.core.jsonl_rewind import RewindResult

    entered, proceed = threading.Event(), threading.Event()
    order = []

    def rewind_step(_sdk_id, target, *_args, stop, **_kwargs):
        order.append(f"{target} started")
        if target == "first":
            entered.set()
            proceed.wait(5)
        order.append(f"{target} done")
        return RewindResult(success=True, message="moved", writer_generation=7, messages_removed=1)

    rewinding = _Rewinding(monkeypatch, pooled=False, rewind=rewind_step)
    # The second's wait for a stopped holder has run out: only taking turns holds it back.
    monkeypatch.setattr("app.api.commands._REWIND_STOP_WAIT_SECONDS", 0.001)
    first = asyncio.ensure_future(rewinding.run("first"))
    await asyncio.to_thread(entered.wait, 5)
    second = asyncio.ensure_future(rewinding.run("second"))
    await asyncio.sleep(0.01)
    next_turn = asyncio.ensure_future(admission.acquire("app-id"))
    await asyncio.sleep(0.01)
    assert not second.done() and not next_turn.done()
    assert order == ["first started"], "the second has not even validated"

    proceed.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert order == ["first started", "first done", "second started", "second done"]
    admission.close(await asyncio.wait_for(next_turn, 5))
    assert rewinding.reseed.call_count == 2


async def test_a_stopping_app_abandons_a_rewind_before_its_claim(monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    stops = []

    def stalled_rewind(*_args, stop, **_kwargs):
        stops.append(stop)
        entered.set()
        proceed.wait(5)

    rewinding = _Rewinding(monkeypatch, pooled=False, rewind=stalled_rewind)
    rewind = rewinding.start()
    await asyncio.to_thread(entered.wait, 5)
    stopping = asyncio.ensure_future(admission.stop_unattended())
    with pytest.raises(asyncio.CancelledError):
        await rewind
    await asyncio.wait_for(stopping, 7)
    assert stops[0].is_set(), "its thread must not claim the chat after this"
    proceed.set()
