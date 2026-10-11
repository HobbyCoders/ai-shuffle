"""Mid-turn steering, end to end: what the user sees for a message sent into a
busy session, from the send to the frame that clears (or keeps) its chip.

``test_send_steering.py`` pins the individual seams (eligibility, the receipt,
the stop race). This module drives them as flows and covers what that file
leaves open:

* delivered vs carried over: a steer that is receipted retires its row; one the
  turn ends without delivering runs as the next turn, exactly once;
* ordering: the user message lands between the tool result it rode in on and
  the model's reply to it;
* recall: the "already delivered, cannot be removed" rule on ``×`` and on
  "clear all", and the chip of a backend that cannot steer at all;
* the two fluid legs behind the real ``HarnessAdapter``: the native leg holds a
  steer until a tool boundary (recallable), the delegated leg writes it to the
  CLI at once (never recallable);
* park / unpark: nothing steers into a parked session, a wake turn takes a
  user's steer, and a swallowed steer comes back when the turn parks;
* terminal task notifications steered into a live turn;
* failure paths: the adapter raising, the DB raising, the socket gone.

The turns table is real (pg_shim SQLite); the engine and pump are stubbed.
"""

import asyncio
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import session_runtime
from app.db import turns as turns_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    def _reset():
        session_runtime._live_runtimes.clear()
        session_runtime._reader_locks.clear()
        session_runtime._idle_readers.clear()
        session_runtime.register_scheduler(None)
        session_runtime.register_broadcaster(None)

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Adapter:
    """A provider adapter that records what it is handed.

    ``refuse`` is raised from every ``steer`` (``refuse_once`` only from the
    first). ``delivered`` is the set of ids the "engine" already put in front
    of the model: ``cancel_steer`` recalls anything submitted and not in it.
    """

    def __init__(self, *, refuse=None, refuse_once=None, cancel_raises=None):
        self.calls = []
        self.origins = []
        self.cancels = []
        self.delivered = set()
        self._refuse = refuse
        self._refuse_once = refuse_once
        self._cancel_raises = cancel_raises

    async def steer(self, text, *, id=None, origin="user"):
        if self._refuse_once is not None:
            exc, self._refuse_once = self._refuse_once, None
            raise exc
        if self._refuse is not None:
            raise self._refuse
        self.calls.append((text, id))
        self.origins.append(origin)

    async def cancel_steer(self, id):
        self.cancels.append(id)
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return id in [c[1] for c in self.calls] and id not in self.delivered


class _NoSteeringAdapter:
    """A backend with no mid-turn channel — exactly the protocol defaults."""

    def __init__(self):
        from app.core.provider.protocol import ProviderAdapter

        self.steer = lambda *a, **k: ProviderAdapter.steer(self, *a, **k)
        self.cancel_steer = lambda id: ProviderAdapter.cancel_steer(self, id)


@contextmanager
def _pooled(adapter):
    """``session_pool.peek`` hands back a state carrying ``adapter``."""
    pool = MagicMock(name="session_pool")
    pool.peek.return_value = MagicMock(adapter=adapter) if adapter else None
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        yield pool


def _start_live(session_id, source="user", **kwargs):
    turn = turns_db.enqueue_turn(session_id, source, prompt="the live one", **kwargs)
    turns_db.start_turn(turn["id"])
    return turns_db.get_turn(turn["id"])


def _live_runtime(session_id, live_turn):
    runtime = session_runtime.SessionRuntime(live_turn)
    session_runtime._live_runtimes[session_id] = runtime
    return runtime


def _seed_session(session_id):
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None, title="t")
    return session_id


def _messages(mock_db, session_id):
    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id=? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    return [(r["role"], r["content"]) for r in rows]


def _pump(events):
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl


@contextmanager
def _pump_env(pump, *, background_work=(), broadcaster=None):
    """Everything the runtime touches around the pump, stubbed.

    The recorder writes every call into ``env["log"]`` so a test can read the
    order the transcript was built in. ``sync_engine.broadcast_queue_updated``
    keeps the REAL queue view it was handed, so a test sees exactly what every
    other device was told the queue holds. ``queue_views`` leaves out the fan-out
    every turn makes as it starts (its row just left the queue) and keeps the
    ones steering causes.
    """
    registry = MagicMock(name="task_registry")
    registry.is_user_stopped.return_value = False
    registry.stop_generation.return_value = 0
    registry.stop_invalidated.return_value = False
    registry.get_parking_background_work.return_value = [
        {"id": t} for t in background_work
    ]
    registry.get_background_launches.return_value = set()
    registry.has_unresolved_background_launches.return_value = False
    registry.has_active_background_work.return_value = bool(background_work)

    log = []

    async def _on_event(event):
        log.append(("event", event.get("type"), event))

    async def _record_steered(content, **kwargs):
        log.append(("steered_user", content, kwargs))
        return True

    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock(side_effect=_on_event)
    recorder.close = AsyncMock()
    recorder.settle = AsyncMock()
    recorder.record_steered_user_message = AsyncMock(side_effect=_record_steered)
    recorder.record_task_terminal = AsyncMock()
    recorder.recording_failed = False

    bc = broadcaster if broadcaster is not None else AsyncMock(name="broadcaster")
    session_runtime.register_broadcaster(bc)
    queue_views = []

    async def _queue_updated(session_id, queue, *, pending_peer_messages=None):
        queue_views.append((session_id, [q["content"] for q in queue]))

    sync = MagicMock(name="sync_engine")
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_queue_updated = AsyncMock(side_effect=_queue_updated)
    sync._broadcast_user_message = AsyncMock()

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
        # The durable park re-check reads task rows these tests never write.
        stack.enter_context(patch(
            "app.core.pending_reconcile.settled_task_ids", return_value=set(),
        ))
        stack.enter_context(patch("app.core.sync_engine.sync_engine", new=sync))
        stack.enter_context(patch(
            "app.core.session_runtime._broadcast_queue_after_start",
            new=_steering_fanout(session_runtime._broadcast_queue_after_start),
        ))
        yield {
            "recorder": recorder, "broadcaster": bc, "sync": sync,
            "log": log, "queue_views": queue_views,
        }


def _steering_fanout(real):
    """Run the real queue fan-out, except the one ``execute`` makes as the turn
    starts — turn bookkeeping, not a steering outcome. That one is spawned as a
    task of its own (so it is this coroutine at the task's root); the steering
    fan-outs are awaited inline by the pump."""
    async def _fanout(session_id):
        task = asyncio.current_task()
        root = task.get_coro() if task is not None else None
        if getattr(root, "cr_code", None) is _fanout.__code__:
            return
        await real(session_id)
    return _fanout


def _ws_handler(frames):
    """A real ChatWebSocketHandler whose socket writes land in ``frames``.

    ``_v2_broadcast_queue`` is left REAL, so the queue_updated frame the socket
    receives is re-sourced from the durable turns table exactly as in
    production; only the cross-device sync fan-out is stubbed.
    """
    import app.api.chat_ws_handler as cwh

    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="dev-1", user_id="admin", api_user_id=None,
    )

    async def _send(frame):
        frames.append(frame)
        return True

    handler.send_json = AsyncMock(side_effect=_send)
    return handler


@contextmanager
def _ws_sync():
    sync = MagicMock(name="ws_sync_engine")
    sync.broadcast_queue_updated = AsyncMock()
    with patch("app.api.chat_ws_handler.sync_engine", new=sync):
        yield sync


def _queue_frames(frames):
    return [
        [m["content"] for m in f["data"]["queued_messages"]]
        for f in frames
        if f.get("type") == "sync" and f.get("event_type") == "queue_updated"
    ]


# ---------------------------------------------------------------------------
# 1. Delivered now vs carried into the next turn
# ---------------------------------------------------------------------------

async def test_the_steered_message_lands_between_its_tool_result_and_the_reply(mock_db):
    """A steer rides the NEXT tool result, so the transcript must read: tool
    call, tool result, the user's interjection, then the model's answer."""
    sid = _seed_session("order-1")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    queued = turns_db.enqueue_turn(sid, "user", prompt="use the staging db")
    events = [
        {"type": "tool_use", "id": "t1", "name": "Bash"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "use the staging db"},
        ]},
        {"type": "chunk", "content": "switching to staging"},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    order = [entry[1] for entry in env["log"]]
    assert order == [
        "tool_use", "tool_result", "use the staging db", "chunk", "done",
    ]
    assert _messages(mock_db, sid) == [("user", "use the staging db")]


async def test_one_receipt_carrying_two_steers_records_both_in_order(mock_db):
    """Two messages delivered at the same boundary keep their send order, and
    every device is told the queue now holds only what is left."""
    sid = _seed_session("order-2")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    a = turns_db.enqueue_turn(sid, "user", prompt="first thought")
    b = turns_db.enqueue_turn(sid, "queue", prompt="second thought")
    turns_db.enqueue_turn(sid, "user", prompt="look at this", payload={
        "upload_blob_ids": ["blob-1"],
    })
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": a["id"], "text": "first thought"},
            {"id": b["id"], "text": "second thought"},
        ]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert [e[1] for e in env["log"] if e[0] == "steered_user"] == [
        "first thought", "second thought",
    ]
    assert _messages(mock_db, sid) == [
        ("user", "first thought"), ("user", "second thought"),
    ]
    # One fan-out for the whole receipt, showing only the undelivered chip.
    assert env["queue_views"] == [(sid, ["look at this"])]


async def test_a_steer_the_turn_never_delivered_runs_as_the_next_turn_once(mock_db):
    """Submitted is not delivered. fluid drops an unreceipted steer at turn
    end, so its row must still be queued — and run exactly once, recording
    the user's message exactly once. The receipted sibling never runs."""
    sid = _seed_session("carry-1")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    delivered = turns_db.enqueue_turn(
        sid, "user", prompt="delivered in time",
        payload={"deferred_user_record": True},
    )
    late = turns_db.enqueue_turn(
        sid, "user", prompt="sent too late", payload={"deferred_user_record": True},
    )
    adapter = _Adapter()
    box = {}

    async def _impl(**kwargs):
        # The send path: the WS handler asks the live runtime to steer.
        assert await session_runtime.try_steer_pending(sid) == 2
        box["attempted"] = set(box["runtime"]._steer_attempted)
        yield {"type": "steer_delivered", "steers": [
            {"id": delivered["id"], "text": "delivered in time"},
        ]}
        yield {"type": "chunk", "content": "on it"}
        # ...and the turn ends before the second one reaches a tool boundary.
        yield {"type": "done", "metadata": {}}

    with _pooled(adapter), _pump_env(_impl):
        box["runtime"] = session_runtime.SessionRuntime(live)
        assert await box["runtime"].execute() == "completed"

    assert box["attempted"] == {delivered["id"], late["id"]}
    # finalize forgets the attempt, so the next turn is free to try it again.
    assert box["runtime"]._steer_attempted == set()
    assert [r["id"] for r in turns_db.get_queued_turns(sid)] == [late["id"]]
    assert turns_db.get_turn(delivered["id"])["stop_reason"] == "steered"

    # The carried-over row runs as its own turn and records itself — once.
    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        nxt = session_runtime.SessionRuntime(turns_db.get_turn(late["id"]))
        assert await nxt.execute() == "completed"
    env["recorder"].start.assert_awaited_once()
    assert env["recorder"].start.await_args.args[0] == "sent too late"
    assert turns_db.get_queued_turns(sid) == []
    assert _messages(mock_db, sid) == [
        ("user", "delivered in time"), ("user", "sent too late"),
    ]
    assert adapter.calls == [
        ("delivered in time", delivered["id"]), ("sent too late", late["id"]),
    ]


async def test_a_second_send_mid_turn_submits_only_the_new_message(mock_db):
    """Each send calls try_steer_pending. The earlier message is still waiting
    for its receipt and must not be handed to the engine a second time."""
    sid = "order-3"
    live = _start_live(sid)
    _live_runtime(sid, live)
    first = turns_db.enqueue_turn(sid, "user", prompt="one")
    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 1
        second = turns_db.enqueue_turn(sid, "user", prompt="two")
        assert await session_runtime.try_steer_pending(sid) == 1
        assert await session_runtime.try_steer_pending(sid) == 0

    assert adapter.calls == [("one", first["id"]), ("two", second["id"])]


async def test_a_row_with_no_prompt_stops_the_walk(mock_db):
    """An empty row cannot be steered, and nothing behind it may overtake it."""
    sid = "order-4"
    live = _start_live(sid)
    _live_runtime(sid, live)
    turns_db.enqueue_turn(sid, "user", prompt="")
    turns_db.enqueue_turn(sid, "user", prompt="behind the empty one")
    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
    assert adapter.calls == []


# ---------------------------------------------------------------------------
# 2. Recall: × and "clear all"
# ---------------------------------------------------------------------------

async def test_the_chip_of_a_backend_that_cannot_steer_can_still_be_removed(mock_db):
    """Regression. A backend with no mid-turn channel answers the submit with
    NotImplementedError — nothing was sent. The row used to stay in the
    attempted set, so × asked the engine to recall it, the protocol default
    said False, the handler read that as "delivered" and left the chip in
    place: the message the user removed then ran as its own turn."""
    sid = "recall-1"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="never mind")
    frames = []
    handler = _ws_handler(frames)

    with _pooled(_NoSteeringAdapter()), _ws_sync():
        assert await session_runtime.try_steer_pending(sid) == 0
        assert row["id"] not in runtime._steer_attempted
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"
        await handler.handle_remove_queued_message(
            {"session_id": sid, "message_id": row["id"]}
        )

    assert turns_db.get_turn(row["id"])["state"] == "aborted"
    assert turns_db.get_turn(row["id"])["stop_reason"] == "removed_from_queue"
    assert _queue_frames(frames) == [[]]


async def test_a_recalled_steer_is_offered_again_by_the_next_pass(mock_db):
    """Recall puts the row back to "never sent": if it is still queued (the
    removal lost a race, or the recall came from elsewhere), the next send
    must hand it over again instead of skipping it forever."""
    sid = "recall-2"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="again")
    adapter = _Adapter()
    with _pooled(adapter):
        await session_runtime.try_steer_pending(sid)
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "cancelled"
        assert await session_runtime.try_steer_pending(sid) == 1
    assert adapter.calls == [("again", row["id"]), ("again", row["id"])]


async def test_remove_after_delivery_keeps_the_chip_until_the_receipt_absorbs_it(
    mock_db,
):
    """The documented rule, as the socket sees it: × on a message the running
    turn already read changes nothing — the queue_updated frame still lists it
    — and the delivery receipt then records it and clears it everywhere."""
    sid = _seed_session("recall-3")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    row = turns_db.enqueue_turn(sid, "user", prompt="already read")
    adapter = _Adapter()
    frames = []
    handler = _ws_handler(frames)
    box = {}

    async def _impl(**kwargs):
        await session_runtime.try_steer_pending(sid)
        adapter.delivered.add(row["id"])
        with _ws_sync():
            await handler.handle_remove_queued_message(
                {"session_id": sid, "message_id": row["id"]}
            )
        box["state_after_remove"] = turns_db.get_turn(row["id"])["state"]
        yield {"type": "steer_delivered", "steers": [
            {"id": row["id"], "text": "already read"},
        ]}
        yield {"type": "done", "metadata": {}}

    with _pooled(adapter), _pump_env(_impl) as env:
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert adapter.cancels == [row["id"]]
    assert box["state_after_remove"] == "queued"
    assert _queue_frames(frames) == [["already read"]]
    assert turns_db.get_turn(row["id"])["stop_reason"] == "steered"
    assert _messages(mock_db, sid) == [("user", "already read")]
    assert env["queue_views"] == [(sid, [])]


async def test_remove_after_the_engine_went_away_lets_the_removal_through(mock_db):
    """Submitted, but the pooled client is gone: nothing can deliver it any
    more, and nothing would ever absorb it, so × must remove it."""
    sid = "recall-4"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="orphaned")
    with _pooled(_Adapter()):
        await session_runtime.try_steer_pending(sid)
    with _pooled(None):
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"


async def test_a_recall_that_raises_falls_back_to_removal(mock_db):
    sid = "recall-5"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="flaky")
    adapter = _Adapter(cancel_raises=OSError("engine socket reset"))
    with _pooled(adapter):
        await session_runtime.try_steer_pending(sid)
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"


async def test_clear_all_recalls_pending_steers_and_keeps_delivered_ones(mock_db):
    """Regression. "Clear all" is × on every chip, so it must obey the same
    rule. It used to abort every row without asking the engine: a steer still
    pending in the engine then reached the model anyway (the user cleared it),
    and a delivered one lost its row, so its receipt recorded nothing and the
    model answered a message the transcript does not show."""
    sid = "recall-6"
    live = _start_live(sid)
    _live_runtime(sid, live)
    read = turns_db.enqueue_turn(sid, "user", prompt="already read")
    pending = turns_db.enqueue_turn(sid, "user", prompt="still pending")
    never = turns_db.enqueue_turn(
        sid, "user", prompt="attachment", payload={"upload_blob_ids": ["b"]},
    )
    internal = turns_db.enqueue_turn(sid, "unpark", prompt="wake")
    adapter = _Adapter()
    frames = []
    handler = _ws_handler(frames)

    with _pooled(adapter), _ws_sync():
        assert await session_runtime.try_steer_pending(sid) == 2
        adapter.delivered.add(read["id"])
        await handler.handle_clear_queued_messages({"session_id": sid})

    assert sorted(adapter.cancels) == sorted([read["id"], pending["id"]])
    assert turns_db.get_turn(read["id"])["state"] == "queued"
    assert turns_db.get_turn(pending["id"])["state"] == "aborted"
    assert turns_db.get_turn(never["id"])["state"] == "aborted"
    # Internal turns are never chips and are never cleared.
    assert turns_db.get_turn(internal["id"])["state"] == "queued"
    assert _queue_frames(frames) == [["already read"]]


# ---------------------------------------------------------------------------
# 3. The two fluid legs behind the real HarnessAdapter
# ---------------------------------------------------------------------------

def _harness(engine):
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    return adapter


def _native_loop(*, running=True):
    """A NativeLoop reduced to its steering state: the pending-steer list and
    the turn task whose liveness gates a Steer. Everything else stays unbuilt
    — the steer/cancel paths touch nothing more."""
    from fluid.engine.native import NativeLoop

    loop = object.__new__(NativeLoop)
    loop._pending_steers = []
    if running:
        fut = MagicMock(name="turn_task")
        fut.done.return_value = False
        loop._turn_task = fut
    else:
        loop._turn_task = None
    return loop


async def test_native_leg_holds_the_steer_until_a_tool_boundary(mock_db):
    """Native: the Steer sits in the engine's pending list carrying the queue
    row's id until the next tool result folds it in — so × before that point
    recalls it, and after it the engine says "delivered"."""
    from fluid.events.ops import Steer

    sid = "leg-native"
    live = _start_live(sid)
    _live_runtime(sid, live)
    recalled = turns_db.enqueue_turn(sid, "user", prompt="take this back")
    kept = turns_db.enqueue_turn(sid, "user", prompt="keep this")
    loop = _native_loop()

    with _pooled(_harness(loop)):
        assert await session_runtime.try_steer_pending(sid) == 2
        assert [(s.text, s.id, s.origin) for s in loop._pending_steers] == [
            ("take this back", recalled["id"], "user"),
            ("keep this", kept["id"], "user"),
        ]
        assert all(isinstance(s, Steer) for s in loop._pending_steers)
        assert await session_runtime.try_cancel_steer(sid, recalled["id"]) == "cancelled"
        assert [s.id for s in loop._pending_steers] == [kept["id"]]
        # The tool boundary drains the pending list into the tool result.
        loop._pending_steers.clear()
        assert await session_runtime.try_cancel_steer(sid, kept["id"]) == "delivered"


def _removal_outcomes(frames):
    return [
        (f["message_id"], f["outcome"])
        for f in frames
        if f.get("type") == "queued_message_removal"
    ]


async def test_a_steer_cancelled_before_its_tool_boundary_is_never_delivered(mock_db):
    """Regression (owner, 2026-10-07). × on a steer still waiting for its tool
    boundary recalls it from the engine, then aborts its row — two steps with a
    DB write between them. A steering pass that ran in that gap (another send,
    an answered question, a peer message) saw a still-queued row that was no
    longer "attempted" and handed it straight back to the engine, so the
    message the user cancelled reached the model at the next tool result."""
    import app.api.chat_ws_handler as cwh
    from app.core import turn_manager

    sid = _seed_session("cancel-race")
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="ignore that, rewriting")
    loop = _native_loop()
    frames = []
    handler = _ws_handler(frames)
    real_run_sync = cwh.run_sync
    passes = []

    async def _racing_run_sync(fn, *args, **kwargs):
        if fn is turn_manager.remove_queued:
            # A concurrent send lands while the removal is being written.
            passes.append(await session_runtime.try_steer_pending(sid))
        return await real_run_sync(fn, *args, **kwargs)

    with _pooled(_harness(loop)), _ws_sync(), \
            patch.object(cwh, "run_sync", new=_racing_run_sync):
        assert await session_runtime.try_steer_pending(sid) == 1
        assert [s.id for s in loop._pending_steers] == [row["id"]]
        await handler.handle_remove_queued_message(
            {"session_id": sid, "message_id": row["id"]}
        )
        # Later passes in the same turn leave it alone too.
        assert await session_runtime.try_steer_pending(sid) == 0

    assert passes == [0]
    # Nothing left for the tool boundary to fold into the next tool result.
    assert loop._pending_steers == []
    assert turns_db.get_turn(row["id"])["state"] == "aborted"
    assert _removal_outcomes(frames) == [(row["id"], "cancelled")]
    assert row["id"] in runtime._steer_withdrawn


async def test_a_cancel_after_the_tool_boundary_reports_already_delivered(mock_db):
    """The boundary already folded the steer into a tool result: the × cannot
    take it back, the row is left for the delivery receipt, and the reply
    tells the client so it can show the message as sent."""
    sid = _seed_session("cancel-late")
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="too late to take back")
    loop = _native_loop()
    frames = []
    handler = _ws_handler(frames)

    with _pooled(_harness(loop)), _ws_sync():
        assert await session_runtime.try_steer_pending(sid) == 1
        loop._pending_steers.clear()  # the tool boundary drained it
        await handler.handle_remove_queued_message(
            {"session_id": sid, "message_id": row["id"]}
        )

    assert turns_db.get_turn(row["id"])["state"] == "queued"
    assert _queue_frames(frames) == [["too late to take back"]]
    assert _removal_outcomes(frames) == [(row["id"], "already_delivered")]
    assert row["id"] not in runtime._steer_withdrawn


async def test_native_leg_refuses_when_no_turn_is_running(mock_db):
    """The engine's turn already ended: fluid refuses, the row stays queued
    and un-attempted, and × is a plain removal."""
    sid = "leg-native-idle"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="too late for this turn")
    loop = _native_loop(running=False)

    with _pooled(_harness(loop)):
        assert await session_runtime.try_steer_pending(sid) == 0
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"

    assert loop._pending_steers == []
    assert row["id"] not in runtime._steer_attempted
    assert turns_db.get_turn(row["id"])["state"] == "queued"


async def test_native_leg_task_notifications_go_out_as_harness_notices(mock_db):
    """A background completion rides the notice marker, never the user's."""
    sid = "leg-native-note"
    live = _start_live(sid)
    _live_runtime(sid, live)
    loop = _native_loop()

    with _pooled(_harness(loop)):
        assert await session_runtime.try_steer_task_notification(
            sid, "<task-notification>done</task-notification>",
            notification_id="note-1", frames=[], task_ids=["t1"],
        ) is True

    assert [(s.id, s.origin) for s in loop._pending_steers] == [("note-1", "system")]


async def test_delegated_leg_delivers_on_write_and_never_recalls(mock_db):
    """Delegated: the steer is written to the CLI's stdin at once — that IS
    delivery — so × always lands on "delivered" and the chip stays for the
    receipt to absorb."""
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    sid = "leg-delegated"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="on stdin already")
    loop = DelegatedLoop(store=MemoryStore(), model="m")
    written = []

    async def _deliver(op):
        written.append((op.text, op.id, op.origin))

    loop._deliver_steer = _deliver
    frames = []
    handler = _ws_handler(frames)

    with _pooled(_harness(loop)), _ws_sync():
        assert await session_runtime.try_steer_pending(sid) == 1
        await handler.handle_remove_queued_message(
            {"session_id": sid, "message_id": row["id"]}
        )

    assert written == [("on stdin already", row["id"], "user")]
    assert turns_db.get_turn(row["id"])["state"] == "queued"
    assert _queue_frames(frames) == [["on stdin already"]]


async def test_delegated_leg_refuses_without_a_running_turn(mock_db):
    """No CLI turn to write into: the refusal leaves the row queued for the
    drain and does not count as an attempt."""
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    sid = "leg-delegated-idle"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="nothing to write into")

    with _pooled(_harness(DelegatedLoop(store=MemoryStore(), model="m"))):
        assert await session_runtime.try_steer_pending(sid) == 0

    assert row["id"] not in runtime._steer_attempted
    assert turns_db.get_turn(row["id"])["state"] == "queued"


async def test_a_disconnected_harness_leaves_the_queue_alone(mock_db):
    sid = "leg-disconnected"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="pool still warming")
    from app.core.provider.harness_adapter import HarnessAdapter

    with _pooled(HarnessAdapter()):
        assert await session_runtime.try_steer_pending(sid) == 0
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"


# ---------------------------------------------------------------------------
# 4. Park / unpark
# ---------------------------------------------------------------------------

async def test_a_parked_session_has_nothing_to_steer_into(mock_db):
    """A parked turn is not live and its runtime is gone, so a message sent
    now is an ordinary turn: nothing is submitted and × is a plain removal."""
    sid = "park-1"
    parked = _start_live(sid)
    turns_db.park_turn(parked["id"], ["task-1"])
    row = turns_db.enqueue_turn(sid, "user", prompt="while it waits")
    adapter = _Adapter()

    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"

    assert adapter.calls == []
    assert turns_db.get_live_turn(sid) is None
    assert [r["id"] for r in turns_db.get_queued_turns(sid)] == [row["id"]]


async def test_a_user_message_steers_into_a_running_wake_turn(mock_db):
    """The unpark (wake) turn is a live turn like any other: a user message
    sent while it runs joins it rather than waiting behind it."""
    sid = "park-2"
    wake = _start_live(sid, source="unpark")
    _live_runtime(sid, wake)
    row = turns_db.enqueue_turn(sid, "user", prompt="and also this")
    adapter = _Adapter()

    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 1

    assert adapter.calls == [("and also this", row["id"])]


@pytest.mark.parametrize("ending", ["error", "interrupted"])
async def test_a_steer_swallowed_by_a_turn_that_parks_goes_back_on_the_queue(
    mock_db, ending,
):
    """Regression. A turn with background work still running PARKS instead of
    aborting — and the park arm used to skip the stranded-steer rescue. A
    message absorbed on the way out (after the stop, or before a provider
    error with no model reply) sat in the transcript unanswered until the
    background work finished, where a plain queued row would have run at once.
    """
    sid = _seed_session(f"park-3-{ending}")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    queued = turns_db.enqueue_turn(sid, "user", prompt="also check the logs",
                                   command_id="req-p")
    box = {}

    async def _impl(**kwargs):
        if ending == "interrupted":
            box["runtime"]._interrupt_requested_at = time.perf_counter()
        yield {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "also check the logs"},
        ]}
        yield ({"type": "error", "message": "429"} if ending == "error"
               else {"type": "interrupted", "metadata": {}})

    with _pump_env(_impl, background_work=["task-1"]) as env:
        box["runtime"] = session_runtime.SessionRuntime(live)
        assert await box["runtime"].execute() == "parked"

    assert turns_db.get_turn(live["id"])["state"] == "parked"
    rows = turns_db.get_queued_turns(sid)
    assert [r["prompt"] for r in rows] == ["also check the logs"]
    assert rows[0]["payload"]["user_message_already_recorded"] is True
    assert rows[0]["command_id"] == "req-p"
    # The chip comes back on every device: first cleared by the absorb, then
    # restored by the re-queue.
    assert env["queue_views"] == [(sid, []), (sid, ["also check the logs"])]


async def test_a_parked_turn_that_answered_its_steer_does_not_re_run_it(mock_db):
    """The ordinary park: the model read the interjection, answered, and ended
    with background work outstanding. Nothing was swallowed."""
    sid = _seed_session("park-4")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    queued = turns_db.enqueue_turn(sid, "user", prompt="and the tests")
    events = [
        {"type": "steer_delivered", "steers": [
            {"id": queued["id"], "text": "and the tests"},
        ]},
        {"type": "chunk", "content": "running them in the background"},
        {"type": "done", "metadata": {}},
    ]
    with _pump_env(_pump(events), background_work=["task-1"]):
        assert await session_runtime.SessionRuntime(live).execute() == "parked"

    assert turns_db.get_queued_turns(sid) == []
    assert _messages(mock_db, sid) == [("user", "and the tests")]


# ---------------------------------------------------------------------------
# 5. Terminal task notifications steered into a live turn
# ---------------------------------------------------------------------------

async def test_an_undelivered_task_notification_is_handed_back_exactly_once(mock_db):
    """No queue row stands behind a notification, so one the turn ends
    without delivering must go back to the scheduler for an at-rest wake —
    once, with its frames, task ids and any held peer messages."""
    sid = _seed_session("note-1")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    adapter = _Adapter()
    frames = [{"task_id": "t1", "status": "completed"}]
    stranded = AsyncMock()

    async def _impl(**kwargs):
        assert await session_runtime.try_steer_task_notification(
            sid, "<task-notification/>", notification_id="n1",
            frames=frames, task_ids=["t1"], held_peer_messages="peer says hi",
        ) is True
        yield {"type": "done", "metadata": {}}

    with _pooled(adapter), _pump_env(_impl), patch(
        "app.core.turn_scheduler.on_task_notification_stranded", new=stranded,
    ):
        runtime = session_runtime.SessionRuntime(live)
        assert await runtime.execute() == "completed"

    assert adapter.origins == ["system"]
    stranded.assert_awaited_once_with(
        sid, frames, ["t1"], held_peer_messages="peer says hi",
    )
    assert runtime._task_steers == {}


async def test_a_delivered_task_notification_settles_the_card_mid_turn(mock_db):
    sid = _seed_session("note-2")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    adapter = _Adapter()
    frames = [{"task_id": "t1", "status": "completed"}]
    stranded = AsyncMock()
    retire = AsyncMock(return_value=True)
    delivered = MagicMock()

    async def _impl(**kwargs):
        await session_runtime.try_steer_task_notification(
            sid, "<task-notification/>", notification_id="n1",
            frames=frames, task_ids=["t1"],
        )
        yield {"type": "steer_delivered", "steers": [{"id": "n1", "text": "x"}]}
        yield {"type": "done", "metadata": {}}

    with _pooled(adapter), _pump_env(_impl) as env, ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.turn_scheduler.on_task_notification_stranded", new=stranded,
        ))
        stack.enter_context(patch(
            "app.core.turn_scheduler.retire_task_terminals", new=retire,
        ))
        stack.enter_context(patch(
            "app.core.turn_scheduler.on_task_notification_delivered", new=delivered,
        ))
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    env["recorder"].record_task_terminal.assert_awaited_once_with(frames)
    retire.assert_awaited_once_with(sid, frames)
    delivered.assert_called_once_with(sid, ["t1"])
    stranded.assert_not_awaited()
    # A notification is not a user message: nothing recorded as one.
    env["recorder"].record_steered_user_message.assert_not_awaited()


@pytest.mark.parametrize("refusal", [
    NotImplementedError("no steering"),
    RuntimeError("no turn running"),
    ValueError("engine bug"),
])
async def test_a_refused_task_notification_leaves_nothing_pending(mock_db, refusal):
    """Every refusal is False (the caller enqueues the wake) and drops the
    registration, so finalize does not ALSO hand it back — two wakes."""
    sid = "note-3"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    with _pooled(_Adapter(refuse=refusal)):
        assert await session_runtime.try_steer_task_notification(
            sid, "<task-notification/>", notification_id="n1",
            frames=[{"task_id": "t1"}], task_ids=["t1"],
        ) is False
    assert runtime._task_steers == {}


async def test_a_task_notification_needs_a_prompt_an_id_and_a_live_engine(mock_db):
    sid = "note-4"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    adapter = _Adapter()
    with _pooled(adapter):
        assert await runtime.steer_task_notification(
            "", notification_id="n1", frames=[], task_ids=[],
        ) is False
        assert await runtime.steer_task_notification(
            "x", notification_id="", frames=[], task_ids=[],
        ) is False
    with _pooled(None):
        assert await runtime.steer_task_notification(
            "x", notification_id="n1", frames=[], task_ids=[],
        ) is False
    # No runtime in this process at all: the at-rest wake owns it.
    assert await session_runtime.try_steer_task_notification(
        "no-such-session", "x", notification_id="n1",
    ) is False
    assert adapter.calls == []


async def test_a_task_notification_is_not_steered_into_a_dying_turn(mock_db):
    sid = "note-5"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    turns_db.interrupt_turn(sid, "stop")
    adapter = _Adapter()
    with _pooled(adapter):
        assert await runtime.steer_task_notification(
            "x", notification_id="n1", frames=[], task_ids=[],
        ) is False
    assert adapter.calls == []


async def test_a_crashing_notification_steer_never_escapes(mock_db, monkeypatch):
    sid = "note-6"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    monkeypatch.setattr(
        runtime, "steer_task_notification", AsyncMock(side_effect=KeyError("boom")),
    )
    assert await session_runtime.try_steer_task_notification(
        sid, "x", notification_id="n1",
    ) is False


async def test_a_failing_strand_callback_does_not_stop_the_turn_settling(mock_db):
    sid = _seed_session("note-7")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")

    async def _impl(**kwargs):
        await session_runtime.try_steer_task_notification(
            sid, "<task-notification/>", notification_id="n1",
            frames=[], task_ids=["t1"],
        )
        yield {"type": "done", "metadata": {}}

    with _pooled(_Adapter()), _pump_env(_impl), patch(
        "app.core.turn_scheduler.on_task_notification_stranded",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert turns_db.get_turn(live["id"])["state"] == "completed"


# ---------------------------------------------------------------------------
# 6. Failure paths
# ---------------------------------------------------------------------------

async def test_a_failing_queue_read_steers_nothing(mock_db, monkeypatch):
    sid = "fail-1"
    live = _start_live(sid)
    _live_runtime(sid, live)
    monkeypatch.setattr(
        turns_db, "get_queued_turns", MagicMock(side_effect=OSError("db down")),
    )
    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
    assert adapter.calls == []


async def test_a_crashing_steer_attempt_never_breaks_the_send(mock_db, monkeypatch):
    sid = "fail-2"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    monkeypatch.setattr(
        runtime, "steer_pending", AsyncMock(side_effect=KeyError("boom")),
    )
    assert await session_runtime.try_steer_pending(sid) == 0


async def test_an_unreadable_turn_row_fails_open(mock_db, monkeypatch):
    """A DB hiccup re-reading the live row must not turn every send into a
    queued turn: the dying check fails open and the steer goes out."""
    sid = "fail-3"
    live = _start_live(sid)
    _live_runtime(sid, live)
    row = turns_db.enqueue_turn(sid, "user", prompt="hello")
    monkeypatch.setattr(turns_db, "get_turn", MagicMock(side_effect=OSError("blip")))
    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 1
    assert adapter.calls == [("hello", row["id"])]


async def test_a_broken_session_pool_means_nothing_to_steer_into(mock_db):
    sid = "fail-4"
    live = _start_live(sid)
    _live_runtime(sid, live)
    turns_db.enqueue_turn(sid, "user", prompt="hello")
    with patch(
        "app.core.session_pool.get_session_pool",
        side_effect=RuntimeError("pool not initialised"),
    ):
        assert await session_runtime.try_steer_pending(sid) == 0


async def test_a_submit_that_raises_is_retried_by_the_next_send(mock_db):
    """An unexpected adapter error is not "sent": the row stays eligible and
    the next send hands it over, still ahead of the newer message."""
    sid = "fail-5"
    live = _start_live(sid)
    runtime = _live_runtime(sid, live)
    first = turns_db.enqueue_turn(sid, "user", prompt="first")
    adapter = _Adapter(refuse_once=ValueError("transient"))
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
        assert first["id"] not in runtime._steer_attempted
        second = turns_db.enqueue_turn(sid, "user", prompt="second")
        assert await session_runtime.try_steer_pending(sid) == 2
    assert adapter.calls == [("first", first["id"]), ("second", second["id"])]


async def test_a_receipt_row_that_cannot_be_loaded_skips_only_that_row(
    mock_db, monkeypatch,
):
    sid = _seed_session("fail-6")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    bad = turns_db.enqueue_turn(sid, "user", prompt="unreadable")
    good = turns_db.enqueue_turn(sid, "user", prompt="readable")
    real_get = turns_db.get_turn

    def _get(turn_id):
        if turn_id == bad["id"]:
            raise OSError("row read failed")
        return real_get(turn_id)

    monkeypatch.setattr(turns_db, "get_turn", _get)
    events = [
        {"type": "steer_delivered", "steers": [
            "not-a-dict",
            {"id": bad["id"], "text": "unreadable"},
            {"id": good["id"], "text": "readable"},
        ]},
        {"type": "done", "metadata": {}},
    ]
    with _pump_env(_pump(events)):
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert _messages(mock_db, sid) == [("user", "readable")]
    assert real_get(bad["id"])["state"] == "queued"
    assert real_get(good["id"])["stop_reason"] == "steered"


async def test_a_receipt_whose_recording_fails_leaves_the_row_queued(mock_db):
    """No transcript record, no absorb: the message must not vanish, so the
    row stays queued and runs as a turn of its own."""
    sid = _seed_session("fail-7")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    row = turns_db.enqueue_turn(sid, "user", prompt="keep me")
    events = [
        {"type": "steer_delivered", "steers": [{"id": row["id"], "text": "keep me"}]},
        {"type": "done", "metadata": {}},
    ]
    with _pump_env(_pump(events)) as env:
        env["recorder"].record_steered_user_message.side_effect = None
        env["recorder"].record_steered_user_message.return_value = False
        await session_runtime.SessionRuntime(live).execute()

    assert turns_db.get_turn(row["id"])["state"] == "queued"
    assert _messages(mock_db, sid) == []
    assert env["queue_views"] == []


async def test_the_socket_going_away_does_not_stop_delivery(mock_db):
    """Every WS write fails (the tab closed mid-turn). The receipt is durable
    work, not a frame: the message is still recorded and its row retired."""
    sid = _seed_session("fail-8")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    row = turns_db.enqueue_turn(sid, "user", prompt="sent then closed the tab")
    events = [
        {"type": "tool_use", "id": "t1", "name": "Bash"},
        {"type": "steer_delivered", "steers": [
            {"id": row["id"], "text": "sent then closed the tab"},
        ]},
        {"type": "done", "metadata": {}},
    ]
    gone = AsyncMock(side_effect=ConnectionResetError("socket closed"))
    with _pump_env(_pump(events), broadcaster=gone):
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert gone.await_count >= 1
    assert turns_db.get_turn(row["id"])["stop_reason"] == "steered"
    assert _messages(mock_db, sid) == [("user", "sent then closed the tab")]


async def test_a_peer_message_receipt_is_published_not_dropped(mock_db):
    """A peer's message has no queue row; its receipt becomes an
    ``agent_message`` system frame so chat and voice observers see it."""
    sid = _seed_session("fail-9")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    events = [
        {"type": "steer_delivered", "steers": [
            {"from": "reviewer", "text": "LGTM with nits"},
        ]},
        {"type": "done", "metadata": {}},
    ]
    with _pump_env(_pump(events)) as env:
        await session_runtime.SessionRuntime(live).execute()

    published = [e[2] for e in env["log"] if e[1] == "system"]
    assert published == [{
        "type": "system", "subtype": "agent_message",
        "data": {"sender": "reviewer", "content": "LGTM with nits",
                 "delivery": "delivered"},
    }]
    env["recorder"].record_steered_user_message.assert_not_awaited()


async def test_a_requeue_that_fails_still_settles_the_turn(mock_db, monkeypatch):
    sid = _seed_session("fail-10")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    row = turns_db.enqueue_turn(sid, "user", prompt="swallowed")
    events = [
        {"type": "steer_delivered", "steers": [{"id": row["id"], "text": "swallowed"}]},
        {"type": "error", "message": "boom"},
    ]
    with _pump_env(_pump(events)) as env:
        monkeypatch.setattr(
            turns_db, "enqueue_turn", MagicMock(side_effect=OSError("db down")),
        )
        assert await session_runtime.SessionRuntime(live).execute() == "aborted"

    assert turns_db.get_turn(live["id"])["state"] == "aborted"
    # Only the absorb fan-out: a failed re-queue announces nothing.
    assert env["queue_views"] == [(sid, [])]


async def test_the_redelivery_flag_skips_a_row_that_already_left_the_queue(mock_db):
    sid = "fail-11"
    live = _start_live(sid)
    runtime = session_runtime.SessionRuntime(live)
    row = turns_db.enqueue_turn(sid, "user", prompt="gone")
    turns_db.remove_queued_turn(row["id"])
    await runtime._flag_redelivery(row["id"])
    assert not (turns_db.get_turn(row["id"])["payload"] or {}).get(
        "steer_already_delivered"
    )


async def test_nothing_is_steered_into_a_turn_that_is_finalizing(mock_db):
    """Regression. The runtime stays registered as live until execute() fully
    unwinds, so a send landing while ``_finalize`` runs still reaches it —
    after ``_steer_attempted`` was cleared. It used to hand the queued row to
    the engine again (which by then is ending this turn, or already running
    the next one on the same pooled client), and to accept a task
    notification after the stranded ones were handed back, losing it."""
    sid = _seed_session("final-1")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    row = turns_db.enqueue_turn(sid, "user", prompt="sent as the turn ended")
    adapter = _Adapter()
    seen = {}
    stranded = AsyncMock()

    async def _during_finalize(session_id):
        # persist_turn_end_state runs inside _finalize, after the clear.
        seen["steered"] = await session_runtime.try_steer_pending(sid)
        seen["note"] = await session_runtime.try_steer_task_notification(
            sid, "<task-notification/>", notification_id="late-note",
            frames=[{"task_id": "t9"}], task_ids=["t9"],
        )

    async def _impl(**kwargs):
        assert await session_runtime.try_steer_pending(sid) == 1
        yield {"type": "done", "metadata": {}}

    with _pooled(adapter), _pump_env(_impl), ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.turn_finalize.persist_turn_end_state",
            new=AsyncMock(side_effect=_during_finalize),
        ))
        stack.enter_context(patch(
            "app.core.turn_scheduler.on_task_notification_stranded", new=stranded,
        ))
        assert await session_runtime.SessionRuntime(live).execute() == "completed"

    assert seen == {"steered": 0, "note": False}
    # Submitted once, during the turn — never again into its dying engine.
    assert adapter.calls == [("sent as the turn ended", row["id"])]
    assert [r["id"] for r in turns_db.get_queued_turns(sid)] == [row["id"]]
