"""#1443: a started chat's report reaches the parent on the worker that runs it.

Two (and three) workers in one process, each with its own database. The
parent chat moves with a real hand-off; the chat it started settles on the
worker it ran on. The report turn must be queued on the parent's owner only,
exactly once, including when a delivery's reply is lost or the owner cannot
be reached for a while.
"""
import pytest

from app.core import placement
from app.core import session_orchestration as orch
from app.core.config import settings
from app.db import database, turns
from app.db.base import get_db
from app.network import control, engine, handoff
from tests.core.network.harness import Worker, converge, join
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401

PARENT = "handoff-test"
CHILD = "started-child"


@pytest.fixture(autouse=True)
def quiet_scheduler(monkeypatch):
    """Record kicks instead of running turns (no model in these tests)."""
    from app.core import turn_scheduler
    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", kicked.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", kicked.append)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(orch, "_delivery_tasks", set(), raising=False)
    return kicked


def _route(workers, lost_replies=None, unreachable=None):
    """control.call between in-process workers. ``lost_replies``: a list of
    actions whose next reply is dropped after the receiver applied it.
    ``unreachable``: a set of device ids no call reaches."""
    by_device = {w.device_id: w for w in workers}

    async def call(device, action, body, **kwargs):
        if unreachable and device in unreachable:
            raise control.RunnerUnreachable("no route")
        caller = engine.require_context().device_id
        with by_device[device].active():
            answer = await control.handle(action, body, from_device=caller)
        if lost_replies and action in lost_replies:
            lost_replies.remove(action)
            raise control.MaybeApplied("reply lost")
        return answer
    return call


def _start_child(home=PARENT, child=CHILD):
    """A chat the parent started with StartSession, its first turn running."""
    database.create_session(child, "default", title="Started work")
    start = turns.enqueue_turn(child, "agent_message", prompt="do the work", profile_id="default",
                               command_id="orchestration:start:" + "a" * 64,
                               payload={"orchestration_sender": home, "orchestration_root": "root-turn",
                                        "orchestration_depth": 1})
    assert turns.start_turn(start["id"])
    _working(home)
    return start


def _working(session_id):
    """The chat is in use: on the old worker before the move, and on the new
    one once its hand-off resume ran (an idle, settled chat defers reports)."""
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET is_active=1 WHERE id=%s", (session_id,))


def _settle(turn):
    return turns.settle_turn(turn["id"], "completed")


def _reports(session_id):
    return [row for row in turns.get_queued_turns(session_id) if row["source"] == "unpark"]


def _all_reports(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM turns WHERE session_id=%s AND source='unpark'", (session_id,))
        return cursor.fetchall()


async def _move_parent(a, b, monkeypatch):
    monkeypatch.setattr(control, "call", _route([a, b]))
    with b.active():
        await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) is None
        _working(PARENT)
    with a.active():
        assert control.owner_of(database.get_session(PARENT)) == b.device_id


@pytest.mark.asyncio
async def test_report_of_a_chat_left_on_the_old_worker_runs_on_the_new_one(pair, monkeypatch):
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    with a.active():
        settled = _settle(start)
        await orch.report_settled_start(CHILD, settled, "completed")
        # Nothing waits on a worker that no longer runs the parent.
        assert turns.get_queued_turns(PARENT) == []
        await orch.deliver_forwarded_reports()
    with b.active():
        [report] = _reports(PARENT)
        [frame] = report["payload"]["pending_frames"]
        assert frame["task_id"] == CHILD and frame["agent_message_turn_id"] == start["id"]
        assert frame["status"] == "completed"
        # B runs it: its placement gate keeps it here.
        decision = placement.resolve_placement(database.get_session(PARENT))
        assert decision.kind != "runner" or decision.runner_id == settings.effective_instance_id
    # A second settlement hook call or a later sweep never delivers it again.
    with a.active():
        await orch.report_settled_start(CHILD, settled, "completed")
        await orch.deliver_forwarded_reports()
        assert turns.get_queued_turns(PARENT) == []
    with b.active():
        assert len(_reports(PARENT)) == 1


@pytest.mark.asyncio
async def test_a_lost_reply_is_retried_and_still_delivers_one_report(pair, monkeypatch):
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    lost = [orch.FORWARD_REPORT_ACTION]
    monkeypatch.setattr(control, "call", _route([a, b], lost_replies=lost))
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports()
        assert lost == []  # B applied it; A never heard back.
        assert orch.pending_forwarded_reports()  # so A keeps it and retries
    with b.active():
        assert len(_reports(PARENT)) == 1
        # The report turn may already have run when the retry arrives.
        [report] = _reports(PARENT)
        assert turns.start_turn(report["id"])
        turns.settle_turn(report["id"], "completed")
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
        assert turns.get_queued_turns(PARENT) == []
    with b.active():
        assert _reports(PARENT) == []
        assert len(_all_reports(PARENT)) == 1

@pytest.mark.asyncio
async def test_an_unreachable_owner_gets_the_report_when_it_is_back(pair, monkeypatch):
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    monkeypatch.setattr(control, "call", _route([a, b], unreachable={b.device_id}))
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports(force=True)
        assert len(orch.pending_forwarded_reports()) == 1
        assert turns.get_queued_turns(PARENT) == []
    with b.active():
        assert _reports(PARENT) == []
    monkeypatch.setattr(control, "call", _route([a, b]))
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    with b.active():
        assert len(_reports(PARENT)) == 1


@pytest.mark.asyncio
async def test_a_chat_started_on_a_third_worker_reports_through_the_starting_worker(pair, tmp_path, monkeypatch):
    """C reports to A (fixed at start); A no longer runs the parent and forwards it to B."""
    a, b, _, _ = pair
    c = Worker("c", tmp_path)
    join(a, c)
    converge([a, b, c])
    try:
        with a.active():
            # A's receipt for a StartSession it sent to C on the parent's behalf.
            receipt = turns.enqueue_turn(orch.REMOTE_RECEIPT_PREFIX + CHILD, "agent_message",
                                         profile_id="default", command_id="orchestration:start:" + "b" * 64,
                                         parent_turn_id="root-turn",
                                         payload={"remote_device_id": c.device_id, "report_to": PARENT,
                                                  "orchestration_depth": 1})
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (receipt["id"],))
        await _move_parent(a, b, monkeypatch)
        monkeypatch.setattr(control, "call", _route([a, b, c]))
        report = {"task_id": CHILD, "status": "completed", "summary": "Done on C",
                  "agent_message_turn_id": "child-turn-1", "kind": "session", "title": "Started work"}
        with c.active():
            for _ in range(2):  # C's own retry after a lost reply
                answer = await control.call(a.device_id, orch.REPORT_ACTION,
                                            {"session_id": CHILD, "report": report})
                assert answer["delivered"] is True
        with a.active():
            await orch.deliver_forwarded_reports(force=True)
            assert turns.get_queued_turns(PARENT) == []
            assert orch.pending_forwarded_reports() == []
        with b.active():
            [wake] = _reports(PARENT)
            [frame] = wake["payload"]["pending_frames"]
            assert frame["task_id"] == CHILD and frame["summary"].startswith("Done on C")
    finally:
        c.close()


@pytest.mark.asyncio
async def test_the_report_of_a_moved_started_chat_reaches_the_parent_that_stayed(pair, monkeypatch):
    """Mirror case: the started chat moves A -> B, the parent stays on A."""
    a, b, _, _ = pair
    with a.active():
        database.create_session("boss", "default", title="Boss")
        _working("boss")
        start = turns.enqueue_turn(PARENT, "agent_message", prompt="do the work", profile_id="default",
                                   command_id="orchestration:start:" + "c" * 64,
                                   payload={"orchestration_sender": "boss", "orchestration_root": "root-turn",
                                            "orchestration_depth": 1})
        assert turns.start_turn(start["id"])
        _settle(start)
        engine.scan_all()
    converge([a, b])
    monkeypatch.setattr(control, "call", _route([a, b]))
    with b.active():
        await handoff.receive(database.get_session(PARENT))
        _working(PARENT)
        # The next piece of work runs on B and settles there.
        work = turns.enqueue_turn(PARENT, "agent_message", prompt="more", profile_id="default",
                                  payload={"orchestration_sender": "boss"})
        assert turns.start_turn(work["id"])
        settled = _settle(work)
        await orch.report_settled_start(PARENT, settled, "completed")
        await orch.deliver_forwarded_reports()
        assert turns.get_queued_turns("boss") == []
    with a.active():
        [wake] = _reports("boss")
        assert wake["payload"]["pending_frames"][0]["task_id"] == PARENT
