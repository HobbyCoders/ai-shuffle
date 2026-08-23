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
# 4. The steering prompt block
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
