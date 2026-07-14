"""Unit tests for app/db/turns.py — the durable turn queue + live-turn state
machine (v2 core, Phase 2)."""

import pytest

from app.db import turns
from app.db import runs


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


# ---------------------------------------------------------------------------
# Enqueue + ordering + reads
# ---------------------------------------------------------------------------

def test_enqueue_assigns_increasing_positions_and_reads_back(mock_db):
    t1 = turns.enqueue_turn("s1", "user", prompt="first")
    t2 = turns.enqueue_turn("s1", "queue", prompt="second")
    t3 = turns.enqueue_turn("s1", "queue", prompt="third")
    assert t1["state"] == "queued"
    assert t1["position"] < t2["position"] < t3["position"]
    # overrides/payload decode to dicts, not raw JSON text.
    assert t1["overrides"] == {} and t1["payload"] == {}

    queued = turns.get_queued_turns("s1")
    assert [t["prompt"] for t in queued] == ["first", "second", "third"]
    # pop_ready peeks the front without mutating.
    assert turns.pop_ready("s1")["id"] == t1["id"]
    assert len(turns.get_queued_turns("s1")) == 3


def test_enqueue_persists_optional_fields(mock_db):
    t = turns.enqueue_turn(
        "s1",
        "automation",
        prompt="p",
        profile_id="prof-1",
        project_id="proj-1",
        worktree_id="wt-1",
        overrides={"model": "opus"},
        payload={"attempts": 2},
        command_id="cmd-1",
        parent_turn_id="turn-parent",
        instance_id="inst-1",
    )
    got = turns.get_turn(t["id"])
    assert got["source"] == "automation"
    assert got["profile_id"] == "prof-1"
    assert got["project_id"] == "proj-1"
    assert got["worktree_id"] == "wt-1"
    assert got["overrides"] == {"model": "opus"}
    assert got["payload"] == {"attempts": 2}
    assert got["command_id"] == "cmd-1"
    assert got["parent_turn_id"] == "turn-parent"
    assert got["instance_id"] == "inst-1"


# ---------------------------------------------------------------------------
# Live-state transitions
# ---------------------------------------------------------------------------

def test_start_turn_makes_it_live_and_get_live_turn_sees_it(mock_db):
    t = turns.enqueue_turn("s1", "user", prompt="go")
    started = turns.start_turn(t["id"], run_id="run-1", instance_id="inst-1")
    assert started is not None
    assert started["state"] == "running"
    assert started["run_id"] == "run-1"
    assert started["started_at"] is not None
    live = turns.get_live_turn("s1")
    assert live["id"] == t["id"]
    # No longer queued.
    assert turns.get_queued_turns("s1") == []


def test_start_turn_twice_returns_none_second_time(mock_db):
    t = turns.enqueue_turn("s1", "user")
    assert turns.start_turn(t["id"]) is not None
    # Second start of the same turn: it is no longer queued -> clean None.
    assert turns.start_turn(t["id"]) is None


def test_one_live_per_session_race_exactly_one_winner(mock_db):
    """Two queued turns, both trying to go live for the same session: the
    partial unique index lets exactly one win; the loser returns None cleanly
    and stays queued."""
    t1 = turns.enqueue_turn("s1", "user")
    t2 = turns.enqueue_turn("s1", "queue")
    w1 = turns.start_turn(t1["id"], run_id="run-1")
    assert w1 is not None and w1["state"] == "running"
    # t2 cannot also become live -> unique index conflict -> None.
    w2 = turns.start_turn(t2["id"], run_id="run-2")
    assert w2 is None
    # t2 is untouched, still queued; the live turn is t1.
    assert turns.get_turn(t2["id"])["state"] == "queued"
    assert turns.get_live_turn("s1")["id"] == t1["id"]


def test_different_sessions_can_each_have_a_live_turn(mock_db):
    a = turns.enqueue_turn("sA", "user")
    b = turns.enqueue_turn("sB", "user")
    assert turns.start_turn(a["id"]) is not None
    assert turns.start_turn(b["id"]) is not None
    assert turns.get_live_turn("sA")["id"] == a["id"]
    assert turns.get_live_turn("sB")["id"] == b["id"]


def test_interrupt_escalate_and_settle(mock_db):
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    inter = turns.interrupt_turn("s1", "send_now", stop_background=True)
    assert inter["state"] == "interrupting"
    assert inter["interrupt_mode"] == "send_now"
    assert inter["stop_background"] in (True, 1)
    drain = turns.escalate_to_draining(t["id"])
    assert drain["state"] == "draining"
    settled = turns.settle_turn(t["id"], "aborted", stop_reason="user_stop")
    assert settled["state"] == "aborted"
    assert settled["stop_reason"] == "user_stop"
    assert settled["ended_at"] is not None
    assert turns.get_live_turn("s1") is None


def test_interrupt_with_no_running_turn_returns_none(mock_db):
    assert turns.interrupt_turn("s1", "stop") is None


def test_escalate_only_from_interrupting(mock_db):
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    # running -> draining is not allowed directly.
    assert turns.escalate_to_draining(t["id"]) is None


def test_settle_is_idempotent(mock_db):
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    assert turns.settle_turn(t["id"], "completed") is not None
    # Second settle is a no-op (already terminal) -> None, state unchanged.
    assert turns.settle_turn(t["id"], "aborted") is None
    assert turns.get_turn(t["id"])["state"] == "completed"


def test_settle_rejects_bad_outcome(mock_db):
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    with pytest.raises(ValueError):
        turns.settle_turn(t["id"], "parked")


def test_park_records_task_ids_and_frees_slot(mock_db):
    t = turns.enqueue_turn("s1", "user", payload={"keep": 1})
    turns.start_turn(t["id"])
    parked = turns.park_turn(t["id"], task_ids=["task-a", "task-b"])
    assert parked["state"] == "parked"
    assert parked["payload"]["task_ids"] == ["task-a", "task-b"]
    # Existing payload keys are preserved through the merge.
    assert parked["payload"]["keep"] == 1
    assert turns.get_live_turn("s1") is None
    # Parked is terminal: settle is a no-op.
    assert turns.settle_turn(t["id"], "completed") is None


def test_requeue_keeps_position_and_frees_slot(mock_db):
    t1 = turns.enqueue_turn("s1", "user")
    t2 = turns.enqueue_turn("s1", "queue")
    turns.start_turn(t1["id"], run_id="run-1")
    original_pos = turns.get_turn(t1["id"])["position"]
    rq = turns.requeue_turn(t1["id"])
    assert rq["state"] == "queued"
    assert rq["run_id"] is None
    assert rq["position"] == original_pos
    # It re-enters at the FRONT (lower position than the later-queued t2).
    assert turns.pop_ready("s1")["id"] == t1["id"]
    assert original_pos < turns.get_turn(t2["id"])["position"]
    assert turns.get_live_turn("s1") is None


# ---------------------------------------------------------------------------
# Reaping dead-lease live turns
# ---------------------------------------------------------------------------

def test_reap_requeues_user_and_queue_aborts_others(mock_db):
    # A user turn on a dead (expired) lease -> requeued.
    tu = turns.enqueue_turn("sUser", "user")
    run_dead = runs.claim_run("sUser", "runner", -1)  # already-expired lease
    turns.start_turn(tu["id"], run_id=run_dead["id"])

    # An auto_continue turn on a NULL-run live state -> aborted.
    ta = turns.enqueue_turn("sAuto", "auto_continue")
    turns.start_turn(ta["id"], run_id=None)

    reaped = turns.reap_expired()
    reaped_ids = {r["id"] for r in reaped}
    assert tu["id"] in reaped_ids and ta["id"] in reaped_ids

    assert turns.get_turn(tu["id"])["state"] == "queued"
    assert turns.get_turn(tu["id"])["run_id"] is None
    aborted = turns.get_turn(ta["id"])
    assert aborted["state"] == "aborted"
    assert aborted["stop_reason"] == "lease_expired"


def test_reap_leaves_live_turn_with_valid_lease_alone(mock_db):
    t = turns.enqueue_turn("sLive", "user")
    run_live = runs.claim_run("sLive", "runner", 60)  # healthy lease
    turns.start_turn(t["id"], run_id=run_live["id"])
    reaped = turns.reap_expired()
    assert reaped == []
    assert turns.get_live_turn("sLive")["id"] == t["id"]


# ---------------------------------------------------------------------------
# v59 migration: queued_messages -> turns
# ---------------------------------------------------------------------------

def test_migration_seeds_turns_from_queued_messages(test_db):
    """The idempotent v59 migration carries queued_messages rows into turns as
    source='queue' queued turns, preserving order, and never duplicates on a
    re-run."""
    from tests.pg_shim import route_db_to
    from app.db import database, queued_messages

    # queued_messages has an FK to sessions we don't need for this unit test.
    test_db.execute("PRAGMA foreign_keys = OFF")

    with route_db_to(test_db) as shim:
        queued_messages.add_queued_message("sess-1", "first msg", profile_id="p1")
        queued_messages.add_queued_message("sess-1", "second msg")
        queued_messages.add_queued_message("sess-2", "other session")

        cursor = shim.cursor()
        database._run_column_migrations(cursor)

        seeded = turns.get_queued_turns("sess-1")
        assert [t["prompt"] for t in seeded] == ["first msg", "second msg"]
        assert all(t["source"] == "queue" for t in seeded)
        assert seeded[0]["profile_id"] == "p1"
        assert len(turns.get_queued_turns("sess-2")) == 1

        # Idempotent: re-running does not duplicate.
        database._run_column_migrations(shim.cursor())
        assert len(turns.get_queued_turns("sess-1")) == 2
