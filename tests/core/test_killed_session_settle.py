"""What a killed session's own rows look like afterwards.

The live-DB symptom: a user-killed sync child showed 0 calls / 0 tokens / $0
against real provider usage, with ``turns.state`` never leaving its live state
and ``sessions.status`` still ``'active'`` while its background-task row said
``killed``. The parent's card, registry row and attribution were all settled —
only the child's own bookkeeping was not, because the kill removes the very
runtime that would have written it.

``settle_killed_session`` is the repair, and the turn settle is its CLAIM: the
guarded ``settle_turn`` UPDATE moves a row exactly once, so a worker whose own
runtime DID reach its turn end is left completely alone and nothing is booked
twice.
"""

import pytest

from app.core import query_persistence as qp
from app.db import database
from app.db import session_events as events_db
from app.db import turns as turns_db


def _mk_session(session_id="ses-worker"):
    database.create_session(session_id=session_id, profile_id="default")
    return session_id


def _go_live(session_id):
    """Enqueue a turn and take it live, the way the runtime would."""
    turn = turns_db.enqueue_turn(session_id, "provider_agent", prompt="do it")
    started = turns_db.start_turn(turn["id"], instance_id="inst-1")
    assert started is not None
    return started["id"]


def _usage_event(turn_id, index, **usage):
    payload = {"usage": {"model_call_index": index, **usage}}
    return {"type": "turn.usage", "turn_id": turn_id, "payload": payload}


# ---------------------------------------------------------------------------
# The flush
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_killed_turn_books_its_partial_usage(mock_db):
    sid = _mk_session()
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=100, output_tokens=10,
                     cache_read_input_tokens=5000,
                     cache_creation_input_tokens=200, cost_usd=0.01),
        _usage_event(turn_id, 2, input_tokens=50, output_tokens=20,
                     cache_read_input_tokens=6000, cost_usd=0.02),
    ])

    assert await qp.settle_killed_session(sid) is True

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 150
    assert row["total_tokens_out"] == 30
    assert row["total_cache_read_tokens"] == 11000
    assert row["total_cache_creation_tokens"] == 200
    assert row["total_cost_usd"] == pytest.approx(0.03)
    # Two model calls landed; the turn itself never completed, so turn_count
    # stays where it was.
    assert row["total_model_calls"] == 2
    assert row["turn_count"] == 0


@pytest.mark.asyncio
async def test_killed_turn_writes_a_usage_log_row(mock_db):
    sid = _mk_session()
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=7, output_tokens=3, cost_usd=0.004),
    ])

    await qp.settle_killed_session(sid)

    logged = mock_db.execute(
        "SELECT tokens_in, tokens_out, cost_usd FROM usage_log "
        "WHERE session_id = ?", (sid,)
    ).fetchall()
    assert len(logged) == 1
    assert logged[0][0] == 7 and logged[0][1] == 3
    assert logged[0][2] == pytest.approx(0.004)


@pytest.mark.asyncio
async def test_a_retried_call_index_is_billed_once(mock_db):
    """Fluid re-emits a call's index when it retries; the flush must not
    double-count it."""
    sid = _mk_session()
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=10, cost_usd=0.001),
        _usage_event(turn_id, 1, input_tokens=10, cost_usd=0.001),
    ])

    await qp.settle_killed_session(sid)

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 10
    assert row["total_model_calls"] == 1


@pytest.mark.asyncio
async def test_usage_from_another_turn_is_not_swept_in(mock_db):
    sid = _mk_session()
    old_turn = turns_db.enqueue_turn(sid, "user", prompt="earlier")
    turns_db.settle_turn(old_turn["id"], "completed")
    events_db.append_events(sid, [
        _usage_event(old_turn["id"], 1, input_tokens=9999, cost_usd=9.99),
    ])
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=11, cost_usd=0.001),
    ])

    await qp.settle_killed_session(sid)

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 11


# ---------------------------------------------------------------------------
# Terminal lifecycle state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_leaves_turn_and_session_terminal(mock_db):
    sid = _mk_session()
    turn_id = _go_live(sid)
    assert turns_db.get_live_turn(sid) is not None

    await qp.settle_killed_session(sid, stop_reason="stop_all")

    turn = turns_db.get_turn(turn_id)
    assert turn["state"] == "aborted"
    assert turn["stop_reason"] == "stop_all"
    assert turns_db.get_live_turn(sid) is None
    assert database.get_session(sid)["status"] == qp.KILLED_SESSION_STATUS


@pytest.mark.asyncio
async def test_status_is_stamped_when_the_kill_lost_the_settle_race(mock_db):
    """The worker's dying runtime settled the turn a beat before the flush
    looked. It was still mid-turn when the kill landed, so the session's
    verdict is still a kill — only the booking is someone else's."""
    sid = _mk_session()
    turn_id = _go_live(sid)
    real_settle = turns_db.settle_turn

    def racing_settle(tid, outcome, stop_reason=None, error=None):
        real_settle(tid, "aborted", "lost_the_race")
        return real_settle(tid, outcome, stop_reason, error)

    turns_db_settle = turns_db.settle_turn
    turns_db.settle_turn = racing_settle
    try:
        assert await qp.settle_killed_session(sid) is False
    finally:
        turns_db.settle_turn = turns_db_settle

    assert turns_db.get_turn(turn_id)["state"] == "aborted"
    assert database.get_session(sid)["status"] == qp.KILLED_SESSION_STATUS


@pytest.mark.asyncio
async def test_a_worker_that_already_finished_keeps_its_status(mock_db):
    """Turn-end reap and stop-all sweep EVERY registered worker, including one
    that already delivered and is only waiting for its dispatch to return.
    Stamping a failure verdict on that session would be a lie."""
    sid = _mk_session()
    turn_id = _go_live(sid)
    turns_db.settle_turn(turn_id, "completed")

    assert await qp.settle_killed_session(sid) is False

    assert database.get_session(sid)["status"] == "active"


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_already_settled_turn_is_never_re_booked(mock_db):
    """The worker's own runtime got there first: it settled the turn and booked
    the usage through the normal path. The flush must add nothing."""
    sid = _mk_session()
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=100, cost_usd=0.05),
    ])
    turns_db.settle_turn(turn_id, "completed")
    database.update_session(
        sid, tokens_in_increment=100, cost_increment=0.05,
        turn_increment=1, model_calls_increment=1,
    )

    assert await qp.settle_killed_session(sid) is False

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 100
    assert row["total_cost_usd"] == pytest.approx(0.05)
    assert row["total_model_calls"] == 1
    assert turns_db.get_turn(turn_id)["state"] == "completed"


@pytest.mark.asyncio
async def test_a_second_flush_books_nothing_more(mock_db):
    """Idempotent by the same claim — two kill paths racing the same worker
    cannot double-bill it."""
    sid = _mk_session()
    turn_id = _go_live(sid)
    events_db.append_events(sid, [
        _usage_event(turn_id, 1, input_tokens=40, cost_usd=0.02),
    ])

    assert await qp.settle_killed_session(sid) is True
    assert await qp.settle_killed_session(sid) is False

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 40
    assert row["total_model_calls"] == 1


@pytest.mark.asyncio
async def test_a_kill_with_no_usage_recorded_is_a_clean_no_op(mock_db):
    sid = _mk_session()
    turn_id = _go_live(sid)

    assert await qp.settle_killed_session(sid) is True

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 0
    assert row["total_model_calls"] == 0
    assert turns_db.get_turn(turn_id)["state"] == "aborted"
    assert mock_db.execute(
        "SELECT COUNT(*) FROM usage_log WHERE session_id = ?", (sid,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_teardown_worker_settles_the_worker_it_kills(mock_db, monkeypatch):
    """Every ProviderAgent kill funnels through ``teardown_worker``; the flush
    has to be part of that funnel, not something the caller remembers to do."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core import provider_agent_supervisor as sup
    from app.core.task_registry import task_registry

    parent = _mk_session("ses-parent")
    worker = _mk_session("ses-worker-kill")
    turn_id = _go_live(worker)
    events_db.append_events(worker, [
        _usage_event(turn_id, 1, input_tokens=64, output_tokens=8, cost_usd=0.03),
    ])

    sup.reset()
    task_registry.reset_all()
    pool = MagicMock()
    pool.remove_broken = AsyncMock(return_value=object())
    monkeypatch.setattr(
        "app.core.session_pool.get_session_pool", lambda: pool
    )
    # The real interrupt would flip the turn to 'interrupting'; the flush must
    # still claim and settle it from there.
    monkeypatch.setattr(
        "app.core.turn_manager.interrupt",
        lambda sid, mode, stop_background=False: turns_db.interrupt_turn(
            sid, mode, stop_background
        ),
    )

    task_registry.start_task(parent, worker, "agent", "worker")
    record = sup.register_worker(sup.WorkerRecord(
        parent_session_id=parent,
        worker_session_id=worker,
        task_id=worker,
        tool_use_id=worker,
        provider_id="prov",
        model_id="m",
    ))
    await sup.teardown_worker(
        record, reason=sup.STOP_ALL_REASON, summary="s", preview="p",
    )

    row = database.get_session(worker)
    assert row["total_tokens_in"] == 64
    assert row["total_model_calls"] == 1
    assert row["total_cost_usd"] == pytest.approx(0.03)
    assert row["status"] == qp.KILLED_SESSION_STATUS
    assert turns_db.get_turn(turn_id)["state"] == "aborted"
    assert task_registry.get_task(parent, worker)["status"] == "killed"
    sup.reset()
    task_registry.reset_all()
