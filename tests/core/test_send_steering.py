"""Mid-turn steering — a message sent to a BUSY session reaches the model at the
earliest safe boundary instead of waiting for the next turn.

Three seams are covered here:

* ``session_runtime.try_steer_pending`` — which queued rows may join the live
  turn, in what order, and how often they may be submitted;
* the pump's ``steer_delivered`` receipt — the user message is recorded at the
  position the model saw it and the queue row is retired;
* the native leg's steering prompt block — the marker the delivery rides in is
  only trusted when the prompt says so.

The turns table is real (pg_shim SQLite); the engine and the pump are stubbed.
"""

import time
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import session_runtime
from app.db import turns as turns_db


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    session_runtime._live_runtimes.clear()
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    yield
    session_runtime._live_runtimes.clear()
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Adapter:
    """Records every steer submitted; optionally refuses like fluid does.

    ``recallable=False`` models the delegated leg (and a delivered native
    steer): ``cancel_steer`` always answers False because the text is already
    the CLI's / the model's.
    """

    def __init__(self, *, refuse=None, recallable=True):
        self.calls = []
        self.cancels = []
        self._refuse = refuse
        self._recallable = recallable

    async def steer(self, text, *, id=None):
        self.calls.append((text, id))
        if self._refuse is not None:
            raise self._refuse

    async def cancel_steer(self, id):
        self.cancels.append(id)
        return self._recallable and id in [c[1] for c in self.calls]


@contextmanager
def _pooled(adapter):
    """Make ``session_pool.peek`` hand back a state carrying ``adapter``."""
    pool = MagicMock(name="session_pool")
    pool.peek.return_value = MagicMock(adapter=adapter) if adapter else None
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        yield pool


def _live_runtime(session_id, live_turn):
    """Register a SessionRuntime for ``live_turn`` as the session's live one."""
    runtime = session_runtime.SessionRuntime(live_turn)
    session_runtime._live_runtimes[session_id] = runtime
    return runtime


def _start_live(session_id, **kwargs):
    turn = turns_db.enqueue_turn(session_id, "user", prompt="the live one", **kwargs)
    turns_db.start_turn(turn["id"])
    return turns_db.get_turn(turn["id"])


# ---------------------------------------------------------------------------
# 1. try_steer_pending — reach and eligibility
# ---------------------------------------------------------------------------

async def test_no_live_runtime_is_a_no_op(mock_db):
    """A session this process does not own falls back to the ordinary drain."""
    _start_live("s1")
    turns_db.enqueue_turn("s1", "user", prompt="hello")

    assert await session_runtime.try_steer_pending("s1") == 0
    assert [t["prompt"] for t in turns_db.get_queued_turns("s1")] == ["hello"]


async def test_queued_messages_are_steered_in_queue_order(mock_db):
    live = _start_live("s1")
    _live_runtime("s1", live)
    a = turns_db.enqueue_turn("s1", "user", prompt="first")
    b = turns_db.enqueue_turn("s1", "queue", prompt="second")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 2

    assert adapter.calls == [("first", a["id"]), ("second", b["id"])]
    # Submitting is not delivering — the rows stay queued until fluid receipts.
    assert len(turns_db.get_queued_turns("s1")) == 2


async def test_internal_sources_are_never_steered(mock_db):
    """An unpark/failover turn is machinery, not someone talking mid-turn."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "unpark", prompt="background work finished")
    user = turns_db.enqueue_turn("s1", "user", prompt="and my message")

    adapter = _Adapter()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")

    assert adapter.calls == [("and my message", user["id"])]


async def test_attachments_block_the_row_and_stop_the_walk(mock_db):
    """Uploads need turn setup a tool result cannot do — and nothing behind an
    ineligible row may overtake it."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn(
        "s1", "user", prompt="look at this", payload={"upload_blob_ids": ["b1"]},
    )
    turns_db.enqueue_turn("s1", "user", prompt="and then this")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []


async def test_a_model_change_blocks_the_row_and_stops_the_walk(mock_db):
    """The live turn cannot switch model mid-flight, so a message that asks for
    a different one waits — and so does everything behind it."""
    live = _start_live("s1", overrides={"model": "sonnet", "fast": False})
    _live_runtime("s1", live)
    turns_db.enqueue_turn(
        "s1", "user", prompt="use opus", overrides={"model": "opus"},
    )
    turns_db.enqueue_turn("s1", "user", prompt="stuck behind it")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []


async def test_fast_and_context_window_differences_do_not_block(mock_db):
    """Only model / effort / permission mode bind the run; throughput knobs the
    composer always sends must not turn every send into a queued turn."""
    live = _start_live("s1", overrides={"model": "sonnet", "fast": False})
    _live_runtime("s1", live)
    row = turns_db.enqueue_turn(
        "s1", "user", prompt="carry on",
        overrides={"model": "sonnet", "fast": True, "context_window": 1000000},
    )

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 1

    assert adapter.calls == [("carry on", row["id"])]


async def test_a_different_profile_blocks_the_row(mock_db):
    """A message addressed to another agent is not this turn's to answer."""
    live = _start_live("s1", profile_id="researcher")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hey", profile_id="coder")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []


async def test_a_row_is_never_submitted_twice(mock_db):
    """Two sends in a row must not hand the first message over twice while it
    is still waiting for its receipt."""
    live = _start_live("s1")
    runtime = _live_runtime("s1", live)
    first = turns_db.enqueue_turn("s1", "user", prompt="first")

    adapter = _Adapter()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        second = turns_db.enqueue_turn("s1", "user", prompt="second")
        await session_runtime.try_steer_pending("s1")

    assert adapter.calls == [("first", first["id"]), ("second", second["id"])]
    # The guard is per-turn: the next turn is free to try the leftovers again.
    runtime._steer_attempted.clear()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
    assert len(adapter.calls) == 4


async def test_a_refused_steer_leaves_every_row_queued(mock_db):
    """fluid refuses when there is nothing live to steer into (the turn is
    ending or interrupting). The walk stops and the drain takes over."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="first")
    turns_db.enqueue_turn("s1", "user", prompt="second")

    adapter = _Adapter(refuse=RuntimeError("no turn is running"))
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert len(adapter.calls) == 1  # stopped at the refusal, never tried #2
    assert [t["prompt"] for t in turns_db.get_queued_turns("s1")] == [
        "first", "second",
    ]


async def test_an_adapter_without_steering_is_inert(mock_db):
    """A non-fluid backend says NotImplementedError; the queue is unharmed."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hello")

    adapter = _Adapter(refuse=NotImplementedError("no steering here"))
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert len(turns_db.get_queued_turns("s1")) == 1


async def test_a_steer_that_raises_never_escapes(mock_db):
    """try_steer_pending is an optimization over a durable queue — it may fail,
    but it may not break the send."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hello")

    adapter = _Adapter(refuse=ValueError("boom"))
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0


# ---------------------------------------------------------------------------
# 2. The delivery receipt in the pump
# ---------------------------------------------------------------------------

def _pump(events):
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl


@contextmanager
def _pump_env(pump):
    """Everything the runtime touches around the pump, stubbed."""
    registry = MagicMock(name="task_registry")
    registry.get_active_background_work.return_value = []
    registry.get_background_launches.return_value = set()
    registry.has_unresolved_background_launches.return_value = False
    registry.has_active_background_work.return_value = False

    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock()
    recorder.close = AsyncMock()
    recorder.record_steered_user_message = AsyncMock(return_value=True)

    broadcaster = AsyncMock(name="broadcaster")
    session_runtime.register_broadcaster(broadcaster)
    sync = MagicMock(name="sync_engine")
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.query_engine._stream_to_websocket_impl", new=pump,
        ))
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run",
            new=AsyncMock(return_value=recorder),
        ))
        stack.enter_context(patch(
            "app.core.session_runtime.record_parked", new=AsyncMock(),
        ))
        stack.enter_context(patch(
            "app.core.session_runtime.start_idle_reader", new=MagicMock(),
        ))
        stack.enter_context(patch(
            "app.core.session_runtime.stop_idle_reader", new=AsyncMock(),
        ))
        stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
        stack.enter_context(patch(
            "app.core.turn_finalize.persist_turn_end_state", new=AsyncMock(),
        ))
        stack.enter_context(patch("app.core.sync_engine.sync_engine", new=sync))
        yield {"recorder": recorder, "broadcaster": broadcaster, "sync": sync}


def _seed_session(session_id):
    """``session_messages`` has an FK to ``sessions``, so the row has to exist
    before anything can record a message against it. No profile: ``profile_id``
    is itself an FK (agents) and these tests seed no agent rows."""
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None, title="t")
    return session_id


def _messages(mock_db, session_id):
    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return [(r["role"], r["content"]) for r in rows]


async def test_receipt_records_the_message_and_absorbs_the_row(mock_db):
    """The user message lands at the stream position the model read it, the
    queue row retires, and the pending bubble is cleared."""
    _seed_session("s5")
    live = turns_db.enqueue_turn("s5", "user", prompt="the live one")
    queued = turns_db.enqueue_turn(
        "s5", "user", prompt="also check the logs", profile_id="coder",
        command_id="req-7",
    )
    events = [
        {"type": "tool_use", "name": "Bash"},
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "also check the logs"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        outcome = await session_runtime.SessionRuntime(live).execute()

    assert outcome == "completed"
    # Recorded on the LIVE turn's recorder, stamped with the QUEUED row's handle
    # so the frontend collapses its pending bubble onto this message.
    env["recorder"].record_steered_user_message.assert_awaited_once_with(
        "also check the logs", profile_id="coder", command_id="req-7",
    )
    # ...and in the legacy store, exactly once.
    assert _messages(mock_db, "s5") == [("user", "also check the logs")]
    # The row is retired: out of the queue, kept as history.
    assert turns_db.get_queued_turns("s5") == []
    assert turns_db.get_turn(queued["id"])["stop_reason"] == "steered"
    env["sync"].broadcast_queue_updated.assert_awaited()
    # The receipt itself is internal — it never reaches the recorder or the WS.
    assert "steer_delivered" not in [
        c.args[0].get("type") for c in env["recorder"].on_event.await_args_list
    ]
    assert "steer_delivered" not in [
        c.args[1]["type"] for c in env["broadcaster"].await_args_list
    ]


async def test_a_slash_command_steer_records_as_a_command_row(mock_db):
    _seed_session("s6")
    live = turns_db.enqueue_turn("s6", "user", prompt="the live one")
    queued = turns_db.enqueue_turn("s6", "user", prompt="/compact now")
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "/compact now"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)):
        await session_runtime.SessionRuntime(live).execute()

    assert _messages(mock_db, "s6") == [("command", "/compact now")]


async def test_a_receipt_for_an_unknown_id_is_skipped(mock_db):
    """A steer with no id, or one naming a row from another session, records
    nothing — but must not derail the turn."""
    live = turns_db.enqueue_turn("s7", "user", prompt="the live one")
    stranger = turns_db.enqueue_turn("other", "user", prompt="not ours")
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": None, "text": "pushed straight onto the queue"},
            {"id": "turn-nope", "text": "gone"},
            {"id": stranger["id"], "text": "not ours"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        outcome = await session_runtime.SessionRuntime(live).execute()

    assert outcome == "completed"
    assert _messages(mock_db, "s7") == []
    env["recorder"].record_steered_user_message.assert_not_awaited()
    assert turns_db.get_turn(stranger["id"])["state"] == "queued"


async def test_a_deferred_turn_records_its_message_when_it_finally_runs(mock_db):
    """The message the composer deferred (sent while a turn was live) is written
    to the legacy store by the turn it becomes — not before."""
    _seed_session("s8")
    turn = turns_db.enqueue_turn(
        "s8", "user", prompt="the deferred one",
        payload={"deferred_user_record": True},
    )

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()

    assert _messages(mock_db, "s8") == [("user", "the deferred one")]
    env["sync"].broadcast_message_added.assert_awaited_once()


async def test_an_ordinary_turn_does_not_re_record_its_message(mock_db):
    """An idle send already persisted at query time — the turn must not add a
    second copy."""
    _seed_session("s9")
    turn = turns_db.enqueue_turn("s9", "user", prompt="sent while idle")

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()

    assert _messages(mock_db, "s9") == []
    env["sync"].broadcast_message_added.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Un-sending a steered message (the × on a pending chip)
# ---------------------------------------------------------------------------
#
# Steering submits the moment the message is sent, so × has a race to settle:
# either the steer is still pending in the engine (recall it, then remove the
# row) or the model already has the words (leave the row alone — the delivery
# receipt owns it now). The engine adjudicates, atomically against its own
# delivery boundary, so the two outcomes are mutually exclusive.


async def test_cancel_is_not_submitted_without_a_live_runtime(mock_db):
    _start_live("s1")
    row = turns_db.enqueue_turn("s1", "user", prompt="hello")

    assert await session_runtime.try_cancel_steer("s1", row["id"]) == "not_submitted"


async def test_cancel_is_not_submitted_for_a_row_never_steered(mock_db):
    """A chip sitting behind an ineligible row was never handed to the engine,
    so there is nothing to recall and nothing to ask about."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="never went out")

    adapter = _Adapter()
    with _pooled(adapter):
        verdict = await session_runtime.try_cancel_steer("s1", row["id"])

    assert verdict == "not_submitted"
    assert adapter.cancels == []


async def test_a_submitted_steer_is_recalled_before_delivery(mock_db):
    live = _start_live("s1")
    runtime = _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="on second thought, no")

    adapter = _Adapter()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        assert row["id"] in runtime._steer_attempted
        verdict = await session_runtime.try_cancel_steer("s1", row["id"])

    assert verdict == "cancelled"
    assert adapter.cancels == [row["id"]]
    # Off the attempted list again: the row is genuinely un-submitted, so a
    # later pass must be free to steer it (or the drain to run it).
    assert row["id"] not in runtime._steer_attempted


async def test_a_delivered_steer_cannot_be_recalled(mock_db):
    """The engine says False once the text is in front of the model."""
    live = _start_live("s1")
    runtime = _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="too late")

    adapter = _Adapter(recallable=False)
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        verdict = await session_runtime.try_cancel_steer("s1", row["id"])

    assert verdict == "delivered"
    # Still attempted — it is on its way to the receipt, not back in the queue.
    assert row["id"] in runtime._steer_attempted


async def test_the_delegated_leg_always_reports_delivered(mock_db):
    """Written-to-stdin IS delivery: DelegatedLoop.cancel_steer never recalls,
    so × on a delegated session can only ever land on 'delivered'."""
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    assert await DelegatedLoop(store=MemoryStore(), model="m").cancel_steer("x") is False

    live = _start_live("s1")
    _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="already on stdin")

    adapter = _Adapter(recallable=False)
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        assert await session_runtime.try_cancel_steer("s1", row["id"]) == "delivered"


def _ws_handler():
    """A ChatWebSocketHandler with only its WS-side collaborators stubbed —
    turn_manager / turns stay real so removal is read off the durable row."""
    import app.api.chat_ws_handler as cwh

    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="dev-1", user_id="admin", api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    handler._v2_broadcast_queue = AsyncMock()
    return handler


async def test_removing_a_recalled_message_aborts_the_row(mock_db):
    live = _start_live("s1")
    _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="unsend me")

    adapter = _Adapter()
    handler = _ws_handler()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        await handler.handle_remove_queued_message(
            {"session_id": "s1", "message_id": row["id"]}
        )

    assert adapter.cancels == [row["id"]]
    assert turns_db.get_turn(row["id"])["state"] == "aborted"
    assert turns_db.get_turn(row["id"])["stop_reason"] == "removed_from_queue"
    handler._v2_broadcast_queue.assert_awaited_with("s1")


async def test_removing_a_delivered_message_leaves_the_row_to_absorb(mock_db):
    """Once the model has the words the chip is not the queue's to drop: the
    row stays queued so the delivery receipt can record and absorb it."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    row = turns_db.enqueue_turn("s1", "user", prompt="already read")

    adapter = _Adapter(recallable=False)
    handler = _ws_handler()
    with _pooled(adapter):
        await session_runtime.try_steer_pending("s1")
        await handler.handle_remove_queued_message(
            {"session_id": "s1", "message_id": row["id"]}
        )

    assert turns_db.get_turn(row["id"])["state"] == "queued"
    assert turns_db.get_turn(row["id"])["stop_reason"] is None
    # The client still gets the truth about what the queue holds.
    handler._v2_broadcast_queue.assert_awaited_with("s1")


async def test_removing_a_never_steered_message_still_aborts_the_row(mock_db):
    """The pre-steering path, unchanged: no live turn, no adjudication, remove."""
    row = turns_db.enqueue_turn("s1", "user", prompt="plain removal")

    handler = _ws_handler()
    await handler.handle_remove_queued_message(
        {"session_id": "s1", "message_id": row["id"]}
    )

    assert turns_db.get_turn(row["id"])["state"] == "aborted"


async def test_a_receipt_for_a_row_that_left_the_queue_records_nothing(
    mock_db, caplog,
):
    """Belt to the cancel's braces. Cancellation is atomic against delivery, so
    a receipt naming a removed row is a protocol violation — and the safe
    reading is silence: a message the user took back must never come back."""
    import logging

    _seed_session("s10")
    live = turns_db.enqueue_turn("s10", "user", prompt="the live one")
    ghost = turns_db.enqueue_turn("s10", "user", prompt="removed, yet receipted")
    turns_db.remove_queued_turn(ghost["id"])
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": ghost["id"], "text": "removed, yet receipted"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        with caplog.at_level(logging.ERROR):
            outcome = await session_runtime.SessionRuntime(live).execute()

    assert outcome == "completed"
    assert _messages(mock_db, "s10") == []
    env["recorder"].record_steered_user_message.assert_not_awaited()
    # The removal stands — nothing re-opened or re-settled the row.
    assert turns_db.get_turn(ghost["id"])["stop_reason"] == "removed_from_queue"
    assert any(
        "steer receipt for turn" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 4. A steer racing the stop button
# ---------------------------------------------------------------------------
#
# Send and Interrupt can land in the same breath. The steer is then written into
# a turn that is already dying: the engine takes the text, receipts it (so the
# row retires and the message joins the transcript) and then aborts without
# answering — the one outcome this feature promises cannot happen. Two layers
# here: don't aim at a dying turn, and put back anything it swallowed anyway.


async def test_an_interrupting_turn_is_never_steered_into(mock_db):
    """The durable flip happens at the command layer before the runtime hears
    about it, so the row's own state is what the walk has to read."""
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hello?")
    turns_db.interrupt_turn("s1", "stop")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []
    # Untouched — the abort-drain runs it as a turn of its own.
    assert [t["prompt"] for t in turns_db.get_queued_turns("s1")] == ["hello?"]


async def test_a_draining_turn_is_never_steered_into(mock_db):
    live = _start_live("s1")
    _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hello?")
    turns_db.interrupt_turn("s1", "stop")
    turns_db.escalate_to_draining(live["id"])

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []


async def test_the_in_process_interrupt_latch_stops_the_walk(mock_db):
    """Once THIS runtime is driving the kill, no DB read is needed to know the
    turn will not answer anything else."""
    live = _start_live("s1")
    runtime = _live_runtime("s1", live)
    turns_db.enqueue_turn("s1", "user", prompt="hello?")
    runtime._interrupt_driving = True

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("s1") == 0

    assert adapter.calls == []


async def test_interrupt_stamps_when_the_kill_was_asked_for(mock_db):
    """``_interrupt_requested_at`` is the line the re-queue decision is drawn
    against, so ``interrupt()`` must set it before it does anything else."""
    live = _start_live("s1")
    runtime = _live_runtime("s1", live)
    assert runtime._interrupt_requested_at is None
    turns_db.interrupt_turn("s1", "stop")
    turns_db.settle_turn(live["id"], "aborted", "interrupted")

    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.session_runtime.record_interrupting", new=AsyncMock(),
        ))
        stack.enter_context(patch(
            "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock(),
        ))
        stack.enter_context(patch.object(
            session_runtime.permission_handler, "cancel_all_requests",
            new=AsyncMock(),
        ))
        assert await runtime.interrupt("stop") == "aborted"

    assert runtime._interrupt_requested_at is not None


def _pump_interrupted_around_a_steer(queued_id, *, interrupt_first, box):
    """A turn that takes a delivery receipt and then dies from an interrupt.

    ``interrupt_first`` decides which side of the interrupt REQUEST the receipt
    lands on — the whole question the re-queue turns on. The stamp is written
    here directly; ``interrupt()`` is what writes it in production, covered by
    ``test_interrupt_stamps_when_the_kill_was_asked_for``.
    """
    async def _impl(**kwargs):
        runtime = box["runtime"]
        if interrupt_first:
            runtime._interrupt_requested_at = time.perf_counter()
        yield {"type": "steer_delivered", "steers": [
            {"id": queued_id, "text": "wait, also check the logs"},
        ]}
        if not interrupt_first:
            runtime._interrupt_requested_at = time.perf_counter()
        yield {"type": "interrupted", "metadata": {}}
    return _impl


async def test_a_steer_swallowed_after_the_stop_goes_back_on_the_queue(mock_db):
    """The live bug: the message reached the transcript and nothing ever
    answered it. It runs as its own turn instead, first in line."""
    _seed_session("s11")
    live = turns_db.enqueue_turn("s11", "user", prompt="the live one")
    queued = turns_db.enqueue_turn(
        "s11", "user", prompt="wait, also check the logs",
        command_id="req-9", payload={"deferred_user_record": True},
    )
    turns_db.enqueue_turn("s11", "user", prompt="typed while it was dying")

    box = {}
    pump = _pump_interrupted_around_a_steer(
        queued["id"], interrupt_first=True, box=box,
    )
    with _pump_env(pump) as env:
        box["runtime"] = session_runtime.SessionRuntime(live)
        outcome = await box["runtime"].execute()

    assert outcome == "aborted"
    # The original row stays absorbed — its message really is in the transcript.
    assert turns_db.get_turn(queued["id"])["stop_reason"] == "steered"

    rows = turns_db.get_queued_turns("s11")
    assert [r["prompt"] for r in rows] == [
        "wait, also check the logs", "typed while it was dying",
    ]
    replacement = rows[0]
    assert replacement["id"] != queued["id"]
    assert replacement["command_id"] == "req-9"
    # Recording is suppressed on the re-run and the deferred marker is gone —
    # between them they would have written the message a second time.
    assert replacement["payload"]["user_message_already_recorded"] is True
    assert replacement["payload"]["requeued_from_turn_id"] == queued["id"]
    assert "deferred_user_record" not in replacement["payload"]
    # The pending bubble comes back on every watching device.
    env["sync"].broadcast_queue_updated.assert_awaited()


async def test_several_swallowed_steers_come_back_in_the_order_they_were_sent(
    mock_db,
):
    """Front-of-queue placement must not reverse them — a later message may not
    overtake an earlier one just because both were rescued."""
    _seed_session("s16")
    live = turns_db.enqueue_turn("s16", "user", prompt="the live one")
    first = turns_db.enqueue_turn("s16", "user", prompt="one")
    second = turns_db.enqueue_turn("s16", "user", prompt="two")
    turns_db.enqueue_turn("s16", "user", prompt="three")

    box = {}

    async def _impl(**kwargs):
        box["runtime"]._interrupt_requested_at = time.perf_counter()
        yield {"type": "steer_delivered", "steers": [
            {"id": first["id"], "text": "one"},
        ]}
        yield {"type": "steer_delivered", "steers": [
            {"id": second["id"], "text": "two"},
        ]}
        yield {"type": "interrupted", "metadata": {}}

    with _pump_env(_impl):
        box["runtime"] = session_runtime.SessionRuntime(live)
        assert await box["runtime"].execute() == "aborted"

    assert [r["prompt"] for r in turns_db.get_queued_turns("s16")] == [
        "one", "two", "three",
    ]


async def test_a_steer_the_model_already_had_is_not_re_run(mock_db):
    """The user watched the model start on it and chose to stop — re-running it
    would answer a question they withdrew."""
    _seed_session("s12")
    live = turns_db.enqueue_turn("s12", "user", prompt="the live one")
    queued = turns_db.enqueue_turn(
        "s12", "user", prompt="wait, also check the logs",
    )

    box = {}
    pump = _pump_interrupted_around_a_steer(
        queued["id"], interrupt_first=False, box=box,
    )
    with _pump_env(pump):
        box["runtime"] = session_runtime.SessionRuntime(live)
        outcome = await box["runtime"].execute()

    assert outcome == "aborted"
    assert turns_db.get_queued_turns("s12") == []


async def test_a_steer_swallowed_by_an_errored_turn_goes_back_on_the_queue(
    mock_db,
):
    """The live bug: a provider 429 killed a turn that had already absorbed a
    steer. Nobody pressed stop, so nothing was withdrawn — and had the message
    stayed queued the drain would simply have run it after the abort. Steering
    must not be worse than queueing, so it comes back."""
    _seed_session("s13")
    live = turns_db.enqueue_turn("s13", "user", prompt="the live one")
    queued = turns_db.enqueue_turn(
        "s13", "user", prompt="also check the logs",
        command_id="req-13", payload={"deferred_user_record": True},
    )
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "also check the logs"},
        ]},
        {"type": "error", "error": "boom"},
    ]

    box = {}
    with _pump_env(_pump(events)) as env:
        box["runtime"] = session_runtime.SessionRuntime(live)
        assert await box["runtime"].execute() == "aborted"

    # No stop was ever asked for — this is the state the old gate bailed on.
    assert box["runtime"]._interrupt_requested_at is None
    assert turns_db.get_turn(queued["id"])["stop_reason"] == "steered"

    rows = turns_db.get_queued_turns("s13")
    assert [r["prompt"] for r in rows] == ["also check the logs"]
    replacement = rows[0]
    assert replacement["id"] != queued["id"]
    assert replacement["command_id"] == "req-13"
    assert replacement["payload"]["user_message_already_recorded"] is True
    assert replacement["payload"]["requeued_from_turn_id"] == queued["id"]
    assert "deferred_user_record" not in replacement["payload"]
    env["sync"].broadcast_queue_updated.assert_awaited()


async def test_an_errored_turn_re_queues_every_steer_it_swallowed(mock_db):
    """No request time means no line to draw: an error abort strands them all,
    and front-of-queue placement still preserves the order they were sent."""
    _seed_session("s17")
    live = turns_db.enqueue_turn("s17", "user", prompt="the live one")
    first = turns_db.enqueue_turn("s17", "user", prompt="one")
    second = turns_db.enqueue_turn("s17", "user", prompt="two")
    turns_db.enqueue_turn("s17", "user", prompt="three")
    events = [
        {"type": "steer_delivered", "steers": [{"id": first["id"], "text": "one"}]},
        {"type": "steer_delivered", "steers": [{"id": second["id"], "text": "two"}]},
        {"type": "error", "error": "boom"},
    ]

    with _pump_env(_pump(events)):
        assert await session_runtime.SessionRuntime(live).execute() == "aborted"

    assert [r["prompt"] for r in turns_db.get_queued_turns("s17")] == [
        "one", "two", "three",
    ]


async def test_a_plain_run_does_log_its_message_user(mock_db):
    """Why the suppress marker has to be explicit: only the LEGACY store write
    is conditional — an ordinary run always appends ``message.user`` to the
    event log, which is the transcript authority."""
    _seed_session("s14")
    turn = turns_db.enqueue_turn("s14", "user", prompt="sent while idle")

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()

    env["recorder"].start.assert_awaited_once_with(
        "sent while idle", profile_id=None, record_user=True,
    )


async def test_a_requeued_steer_records_nothing_when_it_runs(mock_db):
    """Its message is already in both stores, so the re-run contributes only
    ``turn.started`` and the answer that was missing."""
    _seed_session("s15")
    turn = turns_db.enqueue_turn(
        "s15", "user", prompt="wait, also check the logs",
        payload={"user_message_already_recorded": True},
    )

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        assert await session_runtime.SessionRuntime(turn).execute() == "completed"

    env["recorder"].start.assert_awaited_once_with(
        "wait, also check the logs", profile_id=None, record_user=False,
    )
    assert _messages(mock_db, "s15") == []
    env["sync"].broadcast_message_added.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4b. A steer the model actually read is never replayed
# ---------------------------------------------------------------------------
#
# The re-queue rescues a message that reached the transcript and got no answer.
# It must not rescue one the model DID answer: a steer arrives folded into a
# tool result, so model output after the delivery is proof the model was
# round-tripped with the interjection in context. The audit watched exactly that
# go wrong — an interjection appended to a tool result, then again as a
# standalone user turn minutes later, because a long turn that eventually errored
# replayed every steer it had ever absorbed.


async def test_a_steer_the_model_worked_on_is_not_replayed_by_an_error(mock_db):
    """The error-abort branch has no interrupt time to draw a line against, so
    without the consumed check it re-queues everything — including a steer the
    model demonstrably answered."""
    _seed_session("s18")
    live = turns_db.enqueue_turn("s18", "user", prompt="the live one")
    queued = turns_db.enqueue_turn("s18", "user", prompt="wait, check the logs")
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "wait, check the logs"},
        ]},
        # The model read it and kept working.
        {"type": "tool_use", "id": "t1", "name": "Bash"},
        {"type": "chunk", "content": "checking the logs now"},
        {"type": "error", "error": "boom"},
    ]

    with _pump_env(_pump(events)):
        assert await session_runtime.SessionRuntime(live).execute() == "aborted"

    assert turns_db.get_turn(queued["id"])["stop_reason"] == "steered"
    assert turns_db.get_queued_turns("s18") == []


async def test_a_steer_delivered_after_the_last_model_output_still_comes_back(
    mock_db,
):
    """The rescue itself is intact: model output BEFORE the delivery says
    nothing about whether the model saw the interjection."""
    _seed_session("s19")
    live = turns_db.enqueue_turn("s19", "user", prompt="the live one")
    queued = turns_db.enqueue_turn("s19", "user", prompt="one more thing")
    events = [
        {"type": "chunk", "content": "working"},
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "one more thing"},
        ]},
        {"type": "error", "error": "boom"},
    ]

    with _pump_env(_pump(events)):
        assert await session_runtime.SessionRuntime(live).execute() == "aborted"

    rows = turns_db.get_queued_turns("s19")
    assert [r["prompt"] for r in rows] == ["one more thing"]
    assert rows[0]["payload"]["user_message_already_recorded"] is True


async def test_only_the_steers_the_model_never_reached_come_back(mock_db):
    """Two steers, one boundary: the first was worked on, the second landed
    after the model went quiet."""
    _seed_session("s20")
    live = turns_db.enqueue_turn("s20", "user", prompt="the live one")
    first = turns_db.enqueue_turn("s20", "user", prompt="one")
    second = turns_db.enqueue_turn("s20", "user", prompt="two")
    events = [
        {"type": "steer_delivered", "steers": [{"id": first["id"], "text": "one"}]},
        {"type": "chunk", "content": "on it"},
        {"type": "steer_delivered", "steers": [{"id": second["id"], "text": "two"}]},
        {"type": "error", "error": "boom"},
    ]

    with _pump_env(_pump(events)):
        assert await session_runtime.SessionRuntime(live).execute() == "aborted"

    assert [r["prompt"] for r in turns_db.get_queued_turns("s20")] == ["two"]


# ---------------------------------------------------------------------------
# 4c. The absorb is retried, and an unavoidable re-run says so
# ---------------------------------------------------------------------------
#
# The absorb is the only thing between a delivered interjection and a SECOND
# delivery: a row left ``queued`` runs again as an ordinary turn and the model
# reads the same sentence twice.


async def test_a_failing_absorb_is_retried(mock_db):
    """A transient DB error clears — so try again before giving up on the row."""
    _seed_session("s21")
    live = turns_db.enqueue_turn("s21", "user", prompt="the live one")
    queued = turns_db.enqueue_turn("s21", "user", prompt="also check the logs")
    real = turns_db.absorb_queued_turn
    attempts = []

    def flaky(turn_id):
        attempts.append(turn_id)
        if len(attempts) == 1:
            raise RuntimeError("connection reset")
        return real(turn_id)

    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "also check the logs"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)):
        with patch.object(session_runtime, "_ABSORB_BACKOFF", 0), \
             patch.object(turns_db, "absorb_queued_turn", flaky):
            assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert len(attempts) == 2
    assert turns_db.get_turn(queued["id"])["stop_reason"] == "steered"
    assert turns_db.get_queued_turns("s21") == []


async def test_an_unabsorbable_row_is_flagged_as_already_delivered(mock_db):
    """Nothing can take the first delivery back, so the re-run the still-queued
    row will cause is marked — ``_run_pump`` turns the flag into a one-line note
    in front of the prompt it sends."""
    _seed_session("s22")
    live = turns_db.enqueue_turn("s22", "user", prompt="the live one")
    queued = turns_db.enqueue_turn("s22", "user", prompt="also check the logs")
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "also check the logs"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)):
        with patch.object(session_runtime, "_ABSORB_BACKOFF", 0), \
             patch.object(
                 turns_db, "absorb_queued_turn",
                 MagicMock(side_effect=RuntimeError("database is gone")),
             ):
            assert await session_runtime.SessionRuntime(live).execute() == "completed"

    (row,) = turns_db.get_queued_turns("s22")
    assert row["id"] == queued["id"]
    # The user's own words are untouched — only the payload carries the marker.
    assert row["prompt"] == "also check the logs"
    assert row["payload"]["steer_already_delivered"] is True


async def test_the_flagged_re_run_tells_the_model_about_the_duplicate(mock_db):
    """The engine gets the note; the transcript keeps the user's words."""
    _seed_session("s23")
    turn = turns_db.enqueue_turn(
        "s23", "user", prompt="also check the logs",
        payload={"steer_already_delivered": True},
    )

    seen = {}

    async def _impl(**kwargs):
        seen["prompt"] = kwargs.get("prompt")
        yield {"type": "done", "metadata": {}}

    with _pump_env(_impl) as env:
        assert await session_runtime.SessionRuntime(turn).execute() == "completed"

    assert session_runtime._REDELIVERY_NOTE in seen["prompt"]
    assert seen["prompt"].endswith("also check the logs")
    env["recorder"].start.assert_awaited_once_with(
        "also check the logs", profile_id=None, record_user=True,
    )


# ---------------------------------------------------------------------------
# 5. The steering prompt block
# ---------------------------------------------------------------------------

def _compiled_text(*, native):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    compiled = HarnessAdapter._compile_prompt_sections(
        "You are a helpful agent.", None,
        SessionOptions(model="m", cwd="."),
        native=native,
    )
    assert compiled is not None
    return compiled.text or ""


def test_the_native_leg_prompt_teaches_the_steering_marker():
    """A steer arrives inside a tool result — the channel the base prompt says
    to distrust — so the marker only works if the prompt vouches for it."""
    from fluid.context.steering import PROMPT_BLOCK

    assert PROMPT_BLOCK in _compiled_text(native=True)


def test_the_delegated_leg_prompt_leaves_steering_alone():
    """Delegated steers are real user frames on the CLI's stdin — no marker, so
    nothing to explain."""
    from fluid.context.steering import PROMPT_BLOCK

    assert PROMPT_BLOCK not in _compiled_text(native=False)
