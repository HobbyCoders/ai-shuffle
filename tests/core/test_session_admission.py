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
        await admission.acquire(SID)
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


async def test_a_turns_end_frees_the_chat_while_its_route_is_still_saving():
    lease = await admission.acquire(SID)
    admission.turn_ended(lease)
    assert not lease.exited.is_set()
    next_turn = await admission.acquire(SID)
    admission.close(lease)  # its save finishes during the next turn
    assert lease.exited.is_set()
    with pytest.raises(SessionBusyError):
        await admission.acquire(SID)
    admission.close(next_turn)


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
    rewind = admission.Lease(SID, is_rewind=True)
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
    rewind = admission.Lease(SID, is_rewind=True)
    admission.take_for_rewind(rewind)
    waiting = asyncio.ensure_future(admission.acquire(SID))
    await asyncio.sleep(0)
    admission.close(rewind)
    winner = await admission.acquire(SID)
    with pytest.raises(SessionBusyError):
        await waiting
    admission.close(winner)


async def test_a_cancelled_wait_for_a_rewind_holds_nothing():
    rewind = admission.Lease(SID, is_rewind=True)
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


async def test_a_client_that_walks_away_closes_the_turn_and_then_the_lease():
    order = []

    async def turn(lease):
        try:
            yield {"type": "init"}
            yield {"type": "never reached"}
        finally:
            order.append(("turn saved", lease.exited.is_set()))

    stream = admission.stream_turn(SID, turn)
    assert await stream.__anext__() == {"type": "init"}
    await stream.aclose()
    assert order == [("turn saved", False)], "the lease outlives everything under it"
    admission.close(await admission.acquire(SID))


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

        def default_rewind(*_args, stop):
            self.stop = stop
            self.steps.append("rewound")
            return RewindResult(
                success=True, message="moved", writer_generation=7, messages_removed=1,
            )

        monkeypatch.setattr(commands, "get_session_pool", lambda: self.pool)
        monkeypatch.setattr(commands, "database", MagicMock(get_session=MagicMock(return_value={
            "id": "app-id", "sdk_session_id": SID, "project_id": None, "turn_count": 0,
        })))
        monkeypatch.setattr(commands, "get_working_dir_for_project", lambda _id: "/tmp")
        monkeypatch.setattr(commands, "invalidate_parse_cache", lambda _id: None, raising=False)
        monkeypatch.setattr(fluid_transcript, "is_fluid_session", lambda _id: True)
        monkeypatch.setattr(fluid_transcript, "rewind_fluid_session", rewind or default_rewind)
        self.reseed = MagicMock(return_value=fluid_transcript.ReseedResult(status="skipped"))
        monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", self.reseed)

    async def run(self, target="n1"):
        from app.api.commands import RewindRequestV2

        with patch("app.api.rewind_owner.forward_rewind", AsyncMock(return_value=None)):
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

        seen.append(admission._holders["app-id"].is_rewind)
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


async def test_a_cancelled_rewind_tells_its_thread_before_it_frees_the_chat(monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    stops = []

    def slow_rewind(*_args, stop):
        stops.append(stop)
        entered.set()
        proceed.wait(5)

    rewinding = _Rewinding(monkeypatch, pooled=False, rewind=slow_rewind)
    rewind = rewinding.start()
    await asyncio.to_thread(entered.wait, 5)
    rewind.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rewind
    assert stops[0].is_set(), "the abandoned thread's claim must not land"
    proceed.set()
    admission.close(await admission.acquire("app-id"))
