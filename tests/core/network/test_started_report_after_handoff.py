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
    # Each test sends the outbox itself, as the worker it means.
    monkeypatch.setattr(orch, "_start_delivery", lambda: None, raising=False)
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


# ---------------------------------------------------------------------------
# Review round 1: a passed-on report, ownership races, failed admissions
# ---------------------------------------------------------------------------

def _owned_by(worker, device_id):
    """Who runs the parent, as ``worker`` sees it (None: itself)."""
    with worker.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s WHERE id=%s",
                                  (device_id, device_id, PARENT))


def _settled_report_on(worker):
    with worker.active():
        start = _start_child()
        return start, _settle(start)


@pytest.mark.asyncio
async def test_a_report_passed_on_and_back_is_still_admitted(pair, tmp_path, monkeypatch):
    """S1: A -> B -> C -> D -> B. B only passed it on the first time, so it
    keeps no receipt then and admits the report when it comes back."""
    a, b, _, _ = pair
    c, d = Worker("c", tmp_path), Worker("d", tmp_path)
    join(a, c)
    join(a, d)
    converge([a, b, c, d])
    try:
        for worker in (b, c, d):
            with worker.active():
                assert database.get_session(PARENT)
                _working(PARENT)
        _owned_by(a, b.device_id)
        _owned_by(b, c.device_id)
        _owned_by(c, d.device_id)
        _owned_by(d, b.device_id)
        monkeypatch.setattr(control, "call", _route([a, b, c, d]))
        _, settled = _settled_report_on(a)
        with a.active():
            await orch.report_settled_start(CHILD, settled, "completed")
            await orch.deliver_forwarded_reports()
            assert orch.pending_forwarded_reports() == []
        for worker in (b, c):
            with worker.active():
                await orch.deliver_forwarded_reports()
                assert orch.pending_forwarded_reports() == []
        _owned_by(b, None)  # the parent is back on B
        with d.active():
            await orch.deliver_forwarded_reports()
            assert orch.pending_forwarded_reports() == []
        with b.active():
            [wake] = _reports(PARENT)
            assert wake["payload"]["pending_frames"][0]["task_id"] == CHILD
        for worker in (a, c, d):
            with worker.active():
                assert _all_reports(PARENT) == [] and orch.pending_forwarded_reports() == []
    finally:
        c.close()
        d.close()


def _real_owner(home):
    from app.db import sessions
    row = sessions.get_session(home)
    return control.owner_of(row) if row else None


def _flip_after_check(monkeypatch, worker, device_id):
    """The hand-off commits right after this worker's first ownership check."""
    calls = []

    def owner(home):
        found = _real_owner(home)
        if not calls:
            calls.append(home)
            _owned_by(worker, device_id)
        return found
    monkeypatch.setattr(orch, "_report_owner", owner)
    return calls


@pytest.mark.asyncio
async def test_a_parent_moved_between_the_check_and_the_wake_gets_the_report(pair, monkeypatch):
    """S2, local report: the check saw the parent here, then it moved to B."""
    a, b, _, _ = pair
    monkeypatch.setattr(control, "call", _route([a, b]))
    _, settled = _settled_report_on(a)
    with b.active():
        _owned_by(b, None)
        _working(PARENT)
    calls = _flip_after_check(monkeypatch, a, b.device_id)
    with a.active():
        await orch.report_settled_start(CHILD, settled, "completed")
        assert calls == [PARENT]
        assert turns.get_queued_turns(PARENT) == []
        monkeypatch.setattr(orch, "_report_owner", _real_owner)
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    with b.active():
        assert len(_reports(PARENT)) == 1


@pytest.mark.asyncio
async def test_an_outbox_report_whose_parent_moves_again_is_not_queued_here(pair, monkeypatch):
    """S2, outbox pass: the parent looked back here, then moved to B again."""
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    monkeypatch.setattr(control, "call", _route([a, b], unreachable={b.device_id}))
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        assert len(orch.pending_forwarded_reports()) == 1
    _owned_by(a, None)  # A believes the parent came back...
    calls = _flip_after_check(monkeypatch, a, b.device_id)  # ...and it moves again
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert calls == [PARENT]
        assert turns.get_queued_turns(PARENT) == []
        assert len(orch.pending_forwarded_reports()) == 1
    monkeypatch.setattr(orch, "_report_owner", _real_owner)
    monkeypatch.setattr(control, "call", _route([a, b]))
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == [] and turns.get_queued_turns(PARENT) == []
    with b.active():
        assert len(_reports(PARENT)) == 1


@pytest.mark.asyncio
async def test_a_wake_that_could_not_be_queued_is_not_acknowledged(pair, monkeypatch):
    """S3: the owner's enqueue fails; the sender keeps the report and retries."""
    from app.core import turn_scheduler
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    real = turn_scheduler._enqueue

    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(turn_scheduler, "_enqueue", broken)
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports(force=True)
        assert len(orch.pending_forwarded_reports()) == 1
    with b.active():
        assert _all_reports(PARENT) == []
    monkeypatch.setattr(turn_scheduler, "_enqueue", real)
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    with b.active():
        assert len(_reports(PARENT)) == 1


class _Crash(Exception):
    """The receiving worker stops mid-delivery; the sender hears nothing."""


@pytest.mark.asyncio
async def test_a_crash_while_admitting_never_runs_the_report_twice(pair, monkeypatch):
    """S4: the receiver stops right after queuing the wake, before its report
    receipt is written, restarts (its in-memory latches gone) and gets the
    retry. Wake and receipt share one transaction, so neither is kept."""
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    real = turn_scheduler._enqueue
    crashes = []

    def enqueue(session_id, *args, **kwargs):
        row = real(session_id, *args, **kwargs)
        if session_id == PARENT and not crashes:
            crashes.append(session_id)
            raise _Crash()
        return row
    monkeypatch.setattr(turn_scheduler, "_enqueue", enqueue)
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports(force=True)
        assert crashes and len(orch.pending_forwarded_reports()) == 1
    with b.active():
        # Whatever the crash left queued runs after the restart.
        for wake in _reports(PARENT):
            assert turns.start_turn(wake["id"])
            turns.settle_turn(wake["id"], "completed")
    task_registry._woken.clear()
    with a.active():
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    with b.active():
        assert len(_all_reports(PARENT)) == 1


@pytest.mark.asyncio
async def test_a_report_queued_after_the_idle_check_refuses_the_commit(pair, monkeypatch):
    """S2 on the hand-off side: the child settles while the parent's commit is
    between its idle check and the ownership flip. The flip is refused and
    rolled back, the report runs on A, and the move goes through once it ran."""
    import asyncio

    a, b, _, _ = pair
    with a.active():
        start = _start_child()
        settled = _settle(start)
    monkeypatch.setattr(control, "call", _route([a, b]))
    real_lease, real_idle = handoff._lease, handoff._idle
    committing = []

    def lease(*args, **kwargs):
        committing.append(True)  # only the commit calls _idle after _lease
        return real_lease(*args, **kwargs)

    def idle(cursor, sid):
        real_idle(cursor, sid)
        if committing and sid == PARENT:
            committing.clear()
            # The real report path, run in the commit's window on A.
            asyncio.run(orch.report_settled_start(CHILD, settled, "completed"))
    monkeypatch.setattr(handoff, "_lease", lease)
    monkeypatch.setattr(handoff, "_idle", idle)
    with b.active():
        with pytest.raises(Exception, match="queued or active turn"):
            await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) == a.device_id
        assert _all_reports(PARENT) == []
    with a.active():
        assert control.owner_of(database.get_session(PARENT)) is None
        assert orch.pending_forwarded_reports() == []
        [report] = _reports(PARENT)
        assert report["payload"]["pending_frames"][0]["task_id"] == CHILD
        decision = placement.resolve_placement(database.get_session(PARENT))
        assert decision.kind != "runner" or decision.runner_id == settings.effective_instance_id
        assert turns.start_turn(report["id"])
        turns.settle_turn(report["id"], "completed")
    monkeypatch.setattr(handoff, "_idle", real_idle)
    await _move_parent(a, b, monkeypatch)


# ---------------------------------------------------------------------------
# Review round 2: one guarded admission for every report, receipts that move
# with the parent, no report given up at the hop limit
# ---------------------------------------------------------------------------

def _receipts(home=PARENT):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM pending_interactions WHERE session_id=%s AND kind='orchestration_report'",
                       (home,))
        return [row["id"] for row in cursor.fetchall()]


def _deferred_events(starter=PARENT):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM session_events WHERE event_type='orchestration.report_deferred'")
        rows = [row["payload"] for row in cursor.fetchall()]
    import json
    rows = [json.loads(row) if isinstance(row, str) else row for row in rows]
    return [row for row in rows if row.get("starter_session_id") == starter]


def _at_rest(session_id):
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET is_active=0 WHERE id=%s", (session_id,))


def _run(wake):
    assert turns.start_turn(wake["id"])
    turns.settle_turn(wake["id"], "completed")


@pytest.mark.asyncio
async def test_a_deferred_report_for_a_parent_that_moved_meanwhile_goes_to_its_owner(pair, monkeypatch):
    """S2, deferral: the parent at rest is on A at the check, then moves to B
    before the deferral is written. The deferral is kept by B, not by A."""
    a, b, _, _ = pair
    monkeypatch.setattr(control, "call", _route([a, b]))
    _, settled = _settled_report_on(a)
    with a.active():
        _at_rest(PARENT)
    with b.active():
        _owned_by(b, None)
    calls = _flip_after_check(monkeypatch, a, b.device_id)
    with a.active():
        await orch.report_settled_start(CHILD, settled, "completed")
        assert calls == [PARENT]
        assert _deferred_events() == [] and _receipts() == []
        assert len(orch.pending_forwarded_reports()) == 1
        monkeypatch.setattr(orch, "_report_owner", _real_owner)
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    with b.active():
        assert len(_deferred_events()) == 1 and len(_receipts()) == 1
        assert _all_reports(PARENT) == []


@pytest.mark.asyncio
async def test_the_first_local_admission_keeps_a_receipt_a_retry_finds(pair, tmp_path, monkeypatch):
    """S4, first admission: C reports to A, which runs the parent. A admits it,
    the reply is lost, the wake runs, A restarts, and C sends it again."""
    from app.core.task_registry import task_registry
    a, b, _, _ = pair
    c = Worker("c", tmp_path)
    join(a, c)
    converge([a, b, c])
    try:
        with a.active():
            receipt = turns.enqueue_turn(orch.REMOTE_RECEIPT_PREFIX + CHILD, "agent_message",
                                         profile_id="default", command_id="orchestration:start:" + "d" * 64,
                                         parent_turn_id="root-turn",
                                         payload={"remote_device_id": c.device_id, "report_to": PARENT,
                                                  "orchestration_depth": 1})
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (receipt["id"],))
            _working(PARENT)
        lost = [orch.REPORT_ACTION]
        monkeypatch.setattr(control, "call", _route([a, b, c], lost_replies=lost))
        report = {"task_id": CHILD, "status": "completed", "summary": "Done on C",
                  "agent_message_turn_id": "child-turn-1", "kind": "session", "title": "Started work"}
        with c.active():
            with pytest.raises(control.MaybeApplied):
                await control.call(a.device_id, orch.REPORT_ACTION, {"session_id": CHILD, "report": report})
        with a.active():
            [wake] = _reports(PARENT)
            assert len(_receipts()) == 1
            _run(wake)
        task_registry._woken.clear()
        with c.active():
            answer = await control.call(a.device_id, orch.REPORT_ACTION, {"session_id": CHILD, "report": report})
            assert answer["delivered"] is True
        with a.active():
            assert len(_all_reports(PARENT)) == 1 and _reports(PARENT) == []
    finally:
        c.close()


@pytest.mark.asyncio
async def test_receipts_move_with_the_parent_so_it_never_runs_a_report_twice(pair, monkeypatch):
    """S5: B admits a report from A, the reply is lost, the wake runs on B and
    the parent moves back B -> A. A's retry finds the receipt that came along."""
    from app.core.task_registry import task_registry
    a, b, _, _ = pair
    for worker in (a, b):  # a chat without a checkout can move back and forth
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL, branch=NULL WHERE id=%s", (PARENT,))
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    monkeypatch.setattr(control, "call", _route([a, b], lost_replies=[orch.FORWARD_REPORT_ACTION]))
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports(force=True)
        assert len(orch.pending_forwarded_reports()) == 1
    with b.active():
        [wake] = _reports(PARENT)
        _run(wake)
        assert len(_receipts()) == 1
    with a.active():
        await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) is None
        _working(PARENT)
        assert len(_receipts()) == 1
        task_registry._woken.clear()
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
        assert _all_reports(PARENT) == []


@pytest.mark.asyncio
async def test_a_receipt_written_during_the_move_refuses_the_commit(pair, monkeypatch):
    """S5, commit check: a report is admitted on A (deferred: the parent is at
    rest) after begin's export and before the flip. The receipt would stay
    behind, so the flip is refused; the next move carries it."""
    import asyncio

    a, b, _, _ = pair
    with a.active():
        start = _start_child()
        settled = _settle(start)
        _at_rest(PARENT)
    monkeypatch.setattr(control, "call", _route([a, b]))
    real_lease, real_idle = handoff._lease, handoff._idle
    committing = []

    def lease(*args, **kwargs):
        committing.append(True)
        return real_lease(*args, **kwargs)

    def idle(cursor, sid):
        real_idle(cursor, sid)
        if committing and sid == PARENT:
            committing.clear()
            asyncio.run(orch.report_settled_start(CHILD, settled, "completed"))
    monkeypatch.setattr(handoff, "_lease", lease)
    monkeypatch.setattr(handoff, "_idle", idle)
    with b.active():
        with pytest.raises(Exception, match="A report reached this chat during the move"):
            await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) == a.device_id
    with a.active():
        assert control.owner_of(database.get_session(PARENT)) is None
        assert len(_receipts()) == 1 and len(_deferred_events()) == 1
    monkeypatch.setattr(handoff, "_idle", real_idle)
    with b.active():
        await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) is None
        assert len(_receipts()) == 1


@pytest.mark.asyncio
async def test_a_report_at_the_hop_limit_is_kept_and_delivered_later(pair, tmp_path, monkeypatch):
    """S6: A -> B -> C with a limit of two hops, while C has the owner wrong.
    B keeps the report pending with backoff, never refused, and delivers it
    once it learns D runs the parent."""
    a, b, _, _ = pair
    c, d = Worker("c", tmp_path), Worker("d", tmp_path)
    join(a, c)
    join(a, d)
    converge([a, b, c, d])
    monkeypatch.setattr(orch, "MAX_FORWARD_HOPS", 2)
    try:
        for worker in (b, c, d):
            with worker.active():
                _working(PARENT)
        _owned_by(a, b.device_id)
        _owned_by(b, c.device_id)
        _owned_by(c, b.device_id)
        _owned_by(d, None)
        monkeypatch.setattr(control, "call", _route([a, b, c, d]))
        _, settled = _settled_report_on(a)
        with a.active():
            await orch.report_settled_start(CHILD, settled, "completed")
            await orch.deliver_forwarded_reports(force=True)
            assert orch.pending_forwarded_reports() == []
        with b.active():
            await orch.deliver_forwarded_reports(force=True)
            [held] = orch.pending_forwarded_reports()
            assert held["payload"]["attempts"] == 1 and held["payload"]["next_attempt_at"] > 0
        with c.active():
            assert orch.pending_forwarded_reports() == []
        _owned_by(b, d.device_id)
        with b.active():
            await orch.deliver_forwarded_reports(force=True)
            assert orch.pending_forwarded_reports() == []
        with d.active():
            assert len(_reports(PARENT)) == 1
        for worker in (a, b, c):
            with worker.active():
                assert _all_reports(PARENT) == []
    finally:
        c.close()
        d.close()


# ---------------------------------------------------------------------------
# Review round 3: no steer for a report with a receipt, an older receiver
# ---------------------------------------------------------------------------

def _steer_taken(monkeypatch):
    """The parent is mid-turn and its live turn takes every steer."""
    from app.core import turn_scheduler
    steered = []

    async def steer(*args, **kwargs):
        steered.append(args[0])
        return True
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    return steered


@pytest.mark.asyncio
async def test_a_forwarded_report_for_a_parent_mid_turn_is_queued_with_its_receipt_not_steered(pair, monkeypatch):
    """S8: a steer is held only in memory. A report that can come again (here
    forwarded by the old worker) is a queued wake written with its receipt,
    so a crash cannot leave the receipt alone."""
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
    await _move_parent(a, b, monkeypatch)
    steered = _steer_taken(monkeypatch)
    with a.active():
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        await orch.deliver_forwarded_reports(force=True)
        assert orch.pending_forwarded_reports() == []
    assert steered == []
    with b.active():
        [wake] = _reports(PARENT)
        assert wake["payload"]["pending_frames"][0]["task_id"] == CHILD
        assert len(_receipts()) == 1


@pytest.mark.asyncio
async def test_a_local_report_for_a_parent_mid_turn_steers_as_before(pair, monkeypatch):
    """S11: the first delivery of this worker's own settled chat to a parent
    it runs mid-turn steers into the live turn, as before #1443: no queued
    wake, no receipt, nothing in the outbox."""
    a, _b, _, _ = pair
    steered = _steer_taken(monkeypatch)
    with a.active():
        start = _start_child()
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        assert steered == [PARENT]
        assert _all_reports(PARENT) == []
        assert _receipts() == []
        assert orch.pending_forwarded_reports() == []


@pytest.mark.asyncio
async def test_a_local_report_the_live_turn_does_not_take_is_queued_with_its_receipt(pair, monkeypatch):
    """S11: a steer the parent's turn refuses falls through to the guarded
    queued wake with its receipt."""
    from app.core import turn_scheduler
    a, _b, _, _ = pair

    async def refused(*args, **kwargs):
        return False
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", refused)
    with a.active():
        start = _start_child()
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        [wake] = _reports(PARENT)
        assert wake["payload"]["pending_frames"][0]["task_id"] == CHILD
        assert len(_receipts()) == 1


def _as_older_receiver(monkeypatch):
    """The receiver's commit body as main's names it: no report receipts."""
    real = handoff._moving_wakes

    def older(data):
        fields = dict(real(data))
        fields.pop("report_receipts", None)
        return fields
    monkeypatch.setattr(handoff, "_moving_wakes", older)


@pytest.mark.asyncio
async def test_a_chat_with_report_receipts_does_not_move_to_an_older_worker(pair, monkeypatch):
    """S10: an older receiver neither installs nor names the receipts. The
    commit is refused, so the report is never admitted again over there."""
    a, b, _, _ = pair
    with a.active():
        start = _start_child()
        await orch.report_settled_start(CHILD, _settle(start), "completed")
        assert len(_receipts()) == 1
        [wake] = _reports(PARENT)
        _run(wake)
    _as_older_receiver(monkeypatch)
    monkeypatch.setattr(control, "call", _route([a, b]))
    with b.active():
        with pytest.raises(Exception, match="update that worker"):
            await handoff.receive(database.get_session(PARENT))
        assert control.owner_of(database.get_session(PARENT)) == a.device_id
    with a.active():
        assert control.owner_of(database.get_session(PARENT)) is None
        assert len(_receipts()) == 1


@pytest.mark.asyncio
async def test_a_chat_without_report_receipts_still_moves_to_an_older_worker(pair, monkeypatch):
    """S10: nothing to carry, so the move is the same as on main."""
    a, b, _, _ = pair
    with a.active():
        assert _receipts() == []
    _as_older_receiver(monkeypatch)
    await _move_parent(a, b, monkeypatch)
