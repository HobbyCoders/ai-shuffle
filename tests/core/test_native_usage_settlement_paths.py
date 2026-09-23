"""#1407: one booking per native execution attempt, whichever finalizer wins.

The production duplicates were cancelled native workers booked twice: once by
the worker's own turn-end persistence (the Interrupted terminal's totals) and
once by the kill path's event-log recovery. ``tests/core/test_turn_usage_
settlement.py`` pins the shared claim with hand-built metadata. These tests
drive the real producer instead:

* fluid ``Usage`` / ``Interrupted`` events go through ``HarnessAdapter._translate``;
* the real ``_stream_to_websocket_impl`` pump consumes them, with a real
  ``TurnRecorder`` bound to the live ``turns`` row, exactly as
  ``SessionRuntime._run_pump`` does, so the per-call ``turn.usage`` records
  the kill path recovers from are the ones the recorder actually writes;
* the pump's own ``_persist_turn_results`` runs unpatched;
* the kill path is ``provider_agent_supervisor.settle_killed_worker``.

Only the subprocess/profile boundaries are stubbed (same harness as
``test_subagent_usage_isolation.py``).
"""

import asyncio
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fluid.events.model import Interrupted, TurnStarted, Usage

import app.core.query_engine as qe
from app.core import provider_agent_supervisor
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.session_pool import SessionState
from app.core.turn_recorder import TurnRecorder
from app.db import database
from app.db import runs as runs_db
from app.db import turns as turns_db


ENGINE = "ses_native_worker"
CALLS = (
    {"input_tokens": 1200, "output_tokens": 40, "cache_read": 3000,
     "cache_creation": 0, "cost": 0.011},
    {"input_tokens": 800, "output_tokens": 25, "cache_read": 4200,
     "cache_creation": 64, "cost": 0.009},
)
SUM_IN = sum(c["input_tokens"] for c in CALLS)
SUM_OUT = sum(c["output_tokens"] for c in CALLS)
SUM_CACHE_READ = sum(c["cache_read"] for c in CALLS)
SUM_CACHE_CREATION = sum(c["cache_creation"] for c in CALLS)
SUM_COST = sum(c["cost"] for c in CALLS)


def _fluid_turn(turn_id="ft-1"):
    """What fluid's native engine publishes for a two-call turn stopped mid-way."""
    events = [TurnStarted(session_id=ENGINE, turn_id=turn_id, seq=1,
                          model="gpt-test", provider="openai")]
    for index, call in enumerate(CALLS, start=1):
        events.append(Usage(
            session_id=ENGINE, turn_id=turn_id, seq=1 + index,
            input_tokens=call["input_tokens"],
            output_tokens=call["output_tokens"],
            cache_read_tokens=call["cache_read"],
            cache_creation_tokens=call["cache_creation"],
            raw={"model_call_index": index, "cost_usd": call["cost"]},
        ))
    terminal = Interrupted(
        session_id=ENGINE, turn_id=turn_id, seq=len(events) + 1,
        usage={
            "input_tokens": SUM_IN,
            "output_tokens": SUM_OUT,
            "cache_read_input_tokens": SUM_CACHE_READ,
            "cache_creation_input_tokens": SUM_CACHE_CREATION,
        },
        cost_usd=SUM_COST,
        num_model_calls=len(CALLS),
    )
    return events, terminal


class _StubPool:
    def __init__(self, state):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self, lane="delegated"):
        return None

    async def evict_if_needed(self, incoming_lane="delegated"):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


class _Worker:
    """One live native worker turn: its turns row, run lease, adapter and pump."""

    def __init__(self, stack: ExitStack, tmp_path, *, send_terminal=True):
        self.sid = f"worker-{uuid.uuid4()}"
        database.create_session(session_id=self.sid, profile_id="default")
        self.release = asyncio.Event()
        self.reached_gate = asyncio.Event()
        self.send_terminal = send_terminal
        state = SessionState(adapter=MagicMock(), is_connected=True)
        state.adapter.is_alive = MagicMock(return_value=True)
        state.adapter.send_query = AsyncMock()
        state.adapter.receive_events = MagicMock(side_effect=self._receive)
        _engine_patches(stack, _StubPool(state), str(tmp_path))
        self.task = None

    async def start_turn(self):
        run = runs_db.claim_run(self.sid, "runner-test", 60)
        turn = turns_db.enqueue_turn(self.sid, "provider_agent", prompt="go")
        self.turn = turns_db.start_turn(turn["id"], run["id"], "runner-test")
        self.run = run
        self.fluid_events, self.terminal = _fluid_turn()
        self.recorder = await TurnRecorder.create_for_run(
            self.sid, run_id=run["id"], fence_token=run["fence_token"],
            turn_id=self.turn["id"], runner_id="runner-test",
        )
        await self.recorder.start("go", profile_id="default")

    async def _receive_impl(self):
        translator = HarnessAdapter()
        for event in self.fluid_events:
            for message in translator._translate(event):
                yield message
        # Every per-call record is out; the terminal waits on the test.
        self.reached_gate.set()
        await self.release.wait()
        if self.send_terminal:
            for message in translator._translate(self.terminal):
                yield message

    def _receive(self):
        return self._receive_impl()

    async def _pump(self):
        # SessionRuntime._run_pump: every yielded frame goes to the recorder.
        try:
            async for event in qe._stream_to_websocket_impl(
                prompt="go", session_id=self.sid, profile_id="default",
            ):
                await self.recorder.on_event(event)
        finally:
            await self.recorder.close()

    def launch(self):
        self.task = asyncio.create_task(self._pump())
        return self.task

    async def kill(self):
        await provider_agent_supervisor.settle_killed_worker(
            self.sid, "parent_cancelled",
        )


def _engine_patches(stack: ExitStack, pool: _StubPool, cwd: str) -> None:
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "default", "config": {}}
    ))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = None
    options.aih_provider_id = None
    stack.enter_context(patch(
        "app.core.sdk_options.build_options_from_profile",
        return_value=(options, {}),
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch(
        "app.core.turn_recorder._notify_run_change", new=MagicMock()
    ))


def _assert_booked_once(mock_db, sid, *, times=1):
    row = database.get_session(sid)
    assert row["total_tokens_in"] == times * SUM_IN
    assert row["total_tokens_out"] == times * SUM_OUT
    assert row["total_cache_read_tokens"] == times * SUM_CACHE_READ
    assert row["total_cache_creation_tokens"] == times * SUM_CACHE_CREATION
    assert row["total_model_calls"] == times * len(CALLS)
    assert row["total_cost_usd"] == pytest.approx(times * SUM_COST)
    ledger = mock_db.execute(
        "SELECT tokens_in, tokens_out, cache_read_tokens, settlement_key "
        "FROM usage_log WHERE session_id=?", (sid,),
    ).fetchall()
    assert len(ledger) == times
    for tokens_in, tokens_out, cache_read, key in ledger:
        assert (tokens_in, tokens_out, cache_read) == (SUM_IN, SUM_OUT, SUM_CACHE_READ)
        assert key


def _recorded_call_indices(sid, turn_id):
    from app.core.turn_outcome import load_session_events
    return sorted(
        (e.get("payload") or {}).get("usage", {}).get("model_call_index")
        for e in load_session_events(sid)
        if e.get("event_type") == "turn.usage" and e.get("turn_id") == turn_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering", ["finalize_first", "kill_first", "concurrent"])
async def test_interrupted_native_worker_books_once(mock_db, tmp_path, ordering):
    with ExitStack() as stack:
        worker = _Worker(stack, tmp_path)
        await worker.start_turn()
        task = worker.launch()
        await asyncio.wait_for(worker.reached_gate.wait(), 5)
        # The kill path's evidence is what the real recorder wrote.
        assert _recorded_call_indices(worker.sid, worker.turn["id"]) == [1, 2]

        if ordering == "finalize_first":
            # The historical interleaving: the pump booked the Interrupted
            # totals, then the kill won the (still live) turn row and
            # recovered the same calls from the event log.
            worker.release.set()
            await asyncio.wait_for(task, 5)
            await worker.kill()
        elif ordering == "kill_first":
            await worker.kill()
            worker.release.set()
            await asyncio.wait_for(task, 5)
        else:
            async def finish():
                worker.release.set()
                await task
            await asyncio.wait_for(asyncio.gather(worker.kill(), finish()), 5)

    _assert_booked_once(mock_db, worker.sid)
    assert turns_db.get_turn(worker.turn["id"])["state"] == "aborted"


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_first", [False, True])
async def test_cancelled_pump_without_terminal_still_books_once(
    mock_db, tmp_path, kill_first,
):
    """A pump cancelled before the provider's terminal has no totals in its
    metadata. Booking those zeros used to win the claim and lock out the kill
    path's recovery, so a cancelled worker's calls were never booked."""
    with ExitStack() as stack:
        worker = _Worker(stack, tmp_path, send_terminal=False)
        await worker.start_turn()
        task = worker.launch()
        await asyncio.wait_for(worker.reached_gate.wait(), 5)
        if kill_first:
            await worker.kill()
        task.cancel()
        # The impl absorbs the cancellation and still persists the turn.
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        if not kill_first:
            await worker.kill()

    _assert_booked_once(mock_db, worker.sid)


@pytest.mark.asyncio
async def test_a_second_attempt_with_identical_usage_books_again(mock_db, tmp_path):
    """Equal totals are not duplicates: each attempt has its own identity."""
    with ExitStack() as stack:
        worker = _Worker(stack, tmp_path)
        for _ in range(2):
            await worker.start_turn()
            task = worker.launch()
            await asyncio.wait_for(worker.reached_gate.wait(), 5)
            worker.release.set()
            await asyncio.wait_for(task, 5)
            await worker.kill()
            runs_db.release_run(worker.run["id"], worker.run["fence_token"])
            worker.release.clear()
            worker.reached_gate.clear()

    _assert_booked_once(mock_db, worker.sid, times=2)
