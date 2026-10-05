"""Unit tests for app/db/turns.py — the durable turn queue + live-turn state
machine (v2 core, Phase 2)."""

import pytest

from app.db import turns
from app.db import runs


# ---------------------------------------------------------------------------
# Enqueue + ordering + reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["user", "queue", "provider_agent"])
def test_permission_provenance_is_an_enqueue_choice_not_copied_context(mock_db, source):
    original = turns.enqueue_turn("provenance", source, explicit_permission=True,
                                  overrides={"permission_mode": "bypassPermissions", "model": "opus"})
    assert original["overrides"]["_permission_explicit"] is True
    copied = turns.enqueue_turn("provenance", source, overrides=original["overrides"])
    assert "_permission_explicit" not in copied["overrides"]
    assert copied["overrides"]["model"] == "opus"
    turns.start_turn(original["id"])
    requeued = turns.requeue_turn(original["id"])
    assert "_permission_explicit" not in requeued["overrides"]
    assert requeued["overrides"]["permission_mode"] == "bypassPermissions"


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


def test_session_has_unsettled_turns_counts_every_non_terminal_state(mock_db):
    """The wait_session_idle busy predicate: ANY non-terminal row counts, in
    one atomic statement (the split live/queued reads let a queued→running
    flip land between them and read as idle — the 2026-08-28 automation race).
    """
    assert turns.session_has_unsettled_turns("s1") is False

    t = turns.enqueue_turn("s1", "user", prompt="p")
    assert turns.session_has_unsettled_turns("s1") is True  # queued

    turns.start_turn(t["id"], run_id="run-1")
    assert turns.session_has_unsettled_turns("s1") is True  # running

    turns.interrupt_turn("s1", "stop")
    assert turns.session_has_unsettled_turns("s1") is True  # interrupting

    turns.escalate_to_draining(t["id"])
    assert turns.session_has_unsettled_turns("s1") is True  # draining

    turns.settle_turn(t["id"], "aborted", stop_reason="user_stop")
    assert turns.session_has_unsettled_turns("s1") is False  # terminal

    # Parked and completed rows are terminal — they never hold a session busy.
    t2 = turns.enqueue_turn("s1", "user", prompt="p2")
    turns.start_turn(t2["id"], run_id="run-2")
    turns.park_turn(t2["id"], ["task-1"])
    assert turns.session_has_unsettled_turns("s1") is False

    # Scoped per session.
    turns.enqueue_turn("s2", "user", prompt="other")
    assert turns.session_has_unsettled_turns("s1") is False
    assert turns.session_has_unsettled_turns("s2") is True


# ---------------------------------------------------------------------------
# Ad-hoc terminal history rows
# ---------------------------------------------------------------------------

def test_record_adhoc_turn_writes_a_terminal_row(mock_db):
    t = turns.record_adhoc_turn(
        "s1",
        "api_query",
        prompt="ad-hoc",
        profile_id="prof-1",
        project_id="proj-1",
        stop_reason="end_turn",
    )
    got = turns.get_turn(t["id"])
    assert got["state"] == "completed"
    assert got["source"] == "api_query"
    assert got["prompt"] == "ad-hoc"
    assert got["profile_id"] == "prof-1"
    assert got["project_id"] == "proj-1"
    assert got["stop_reason"] == "end_turn"
    # Terminal on arrival: both timestamps set, no run lease to point at.
    assert got["started_at"] is not None
    assert got["ended_at"] is not None
    assert got["run_id"] is None
    assert got["instance_id"] is None
    # Inert to the scheduler and the live-slot reads.
    assert turns.get_queued_turns("s1") == []
    assert turns.get_live_turn("s1") is None


def test_record_adhoc_turn_stores_overrides_and_payload(mock_db):
    # The context trail scheduler-enqueued successors inherit from: an unpark
    # wake copies this turn's overrides (model/provider leg) and api_user
    # attribution via turn_scheduler._enqueue_context_wake.
    t = turns.record_adhoc_turn(
        "s1", "api_background",
        overrides={"model": "prov:p1:m1", "effort": "high"},
        payload={"api_user_id": "u-api"},
    )
    got = turns.get_turn(t["id"])
    assert got["overrides"] == {"model": "prov:p1:m1", "effort": "high"}
    assert got["payload"]["api_user_id"] == "u-api"


def test_record_adhoc_turn_aborted_carries_error(mock_db):
    t = turns.record_adhoc_turn(
        "s1", "api_stream", outcome="aborted",
        stop_reason="interrupted", error="boom",
    )
    got = turns.get_turn(t["id"])
    assert got["state"] == "aborted"
    assert got["stop_reason"] == "interrupted"
    assert got["error"] == "boom"


def test_record_adhoc_turn_succeeds_while_a_turn_is_live(mock_db):
    """Terminal rows sit outside the partial unique index, so recording one
    never trips on the session's live turn (or aborts it)."""
    live = turns.enqueue_turn("s1", "user")
    turns.start_turn(live["id"], run_id="run-1")

    adhoc = turns.record_adhoc_turn("s1", "api_background")

    assert turns.get_turn(adhoc["id"])["state"] == "completed"
    # The real live turn keeps the slot, untouched.
    assert turns.get_live_turn("s1")["id"] == live["id"]


def test_record_adhoc_turn_is_not_reaped(mock_db):
    """A terminal row has no run lease, but the reap sweep only looks at live
    states — so it must survive a sweep that aborts a genuinely dead turn."""
    adhoc = turns.record_adhoc_turn("s1", "api_query")
    dead = turns.enqueue_turn("s2", "auto_continue")
    turns.start_turn(dead["id"], run_id=None)

    reaped_ids = {r["id"] for r in turns.reap_expired()}

    assert adhoc["id"] not in reaped_ids
    assert dead["id"] in reaped_ids
    row = turns.get_turn(adhoc["id"])
    assert row["state"] == "completed"
    assert row["stop_reason"] is None


def test_record_adhoc_turn_rejects_non_terminal_outcome(mock_db):
    for bad in ("running", "queued", "parked"):
        with pytest.raises(ValueError):
            turns.record_adhoc_turn("s1", "api_query", outcome=bad)


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


def test_interrupt_turn_id_scoped_matches_current_running_turn(mock_db):
    # A turn-scoped stop whose turn_id IS the running turn interrupts it.
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    inter = turns.interrupt_turn("s1", "stop", turn_id=t["id"])
    assert inter is not None and inter["state"] == "interrupting"


def test_interrupt_turn_id_scoped_stale_stop_no_ops_and_spares_successor(mock_db):
    # GitHub #585: turn N settles completed, successor N+1 takes the live slot,
    # then a stop targeting N's turn_id arrives late. The id=turn_id guard
    # matches zero rows -> None -> N+1 is NOT interrupted.
    n = turns.enqueue_turn("s1", "user")
    turns.start_turn(n["id"])
    assert turns.settle_turn(n["id"], "completed") is not None
    n1 = turns.enqueue_turn("s1", "queue")
    turns.start_turn(n1["id"])
    assert turns.get_live_turn("s1")["id"] == n1["id"]

    # Stale stop for the already-settled N.
    assert turns.interrupt_turn("s1", "stop", turn_id=n["id"]) is None
    # Successor untouched, still running; N stays terminally completed.
    assert turns.get_turn(n1["id"])["state"] == "running"
    assert turns.get_turn(n["id"])["state"] == "completed"


def test_interrupt_without_turn_id_stays_session_scoped(mock_db):
    # No turn_id -> legacy session-scoped stop: whatever running turn the session
    # has is interrupted, byte-identical to before #585.
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    inter = turns.interrupt_turn("s1", "stop")
    assert inter is not None and inter["state"] == "interrupting"
    assert inter["id"] == t["id"]


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


def test_absorb_retires_a_queued_turn_as_steered(mock_db):
    """A queued prompt handed to the live turn as a steer settles completed with
    stop_reason='steered' — out of the queue, still in history."""
    live = turns.enqueue_turn("s1", "user", prompt="running")
    turns.start_turn(live["id"])
    t = turns.enqueue_turn("s1", "user", prompt="and also this")

    assert turns.absorb_queued_turn(t["id"]) is True
    row = turns.get_turn(t["id"])
    assert row["state"] == "completed"
    assert row["stop_reason"] == "steered"
    assert row["ended_at"] is not None
    # Gone from the queue, so the drain can never run it a second time.
    assert turns.get_queued_turns("s1") == []
    assert turns.pop_ready("s1") is None
    # The live turn is untouched.
    assert turns.get_live_turn("s1")["id"] == live["id"]


def test_absorb_only_from_queued_and_is_idempotent(mock_db):
    """The guard lives in the UPDATE: a row that already left the queue (raced
    to live, removed, or absorbed once) is never touched again."""
    running = turns.enqueue_turn("s1", "user")
    turns.start_turn(running["id"])
    assert turns.absorb_queued_turn(running["id"]) is False
    assert turns.get_turn(running["id"])["state"] == "running"

    queued = turns.enqueue_turn("s2", "queue", prompt="p")
    assert turns.absorb_queued_turn(queued["id"]) is True
    assert turns.absorb_queued_turn(queued["id"]) is False
    assert turns.get_turn(queued["id"])["stop_reason"] == "steered"

    assert turns.absorb_queued_turn("turn-does-not-exist") is False


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


def test_settle_parked_turns_clears_stale_parked_rows(mock_db):
    """settle_parked_turns flips every 'parked' row for a session to 'completed'
    (the escape hatch settle_turn refuses), and leaves other sessions alone."""
    a = turns.enqueue_turn("s1", "user")
    turns.start_turn(a["id"])
    turns.park_turn(a["id"], task_ids=["t-a"])
    b = turns.enqueue_turn("s1", "queue")
    turns.start_turn(b["id"])
    turns.park_turn(b["id"], task_ids=["t-b"])
    # A live (running) row and another session must be untouched.
    live = turns.enqueue_turn("s1", "queue")
    turns.start_turn(live["id"])
    other = turns.enqueue_turn("s2", "user")
    turns.start_turn(other["id"])
    turns.park_turn(other["id"], task_ids=["t-c"])

    settled = turns.settle_parked_turns("s1")

    assert settled == 2
    assert turns.get_turn(a["id"])["state"] == "completed"
    assert turns.get_turn(b["id"])["state"] == "completed"
    assert turns.get_turn(a["id"])["ended_at"] is not None
    # Running row untouched; other session's parked row untouched.
    assert turns.get_turn(live["id"])["state"] == "running"
    assert turns.get_turn(other["id"])["state"] == "parked"


def test_settle_parked_turns_excludes_current_turn(mock_db):
    """The self-heal call site passes the turn about to go live as exclude so it
    never settles itself."""
    stale = turns.enqueue_turn("s1", "user")
    turns.start_turn(stale["id"])
    turns.park_turn(stale["id"], task_ids=["t-a"])
    current = turns.enqueue_turn("s1", "queue")
    turns.start_turn(current["id"])
    turns.park_turn(current["id"], task_ids=["t-b"])

    settled = turns.settle_parked_turns("s1", exclude_turn_id=current["id"])

    assert settled == 1
    assert turns.get_turn(stale["id"])["state"] == "completed"
    assert turns.get_turn(current["id"])["state"] == "parked"


def test_settle_parked_turns_no_rows_returns_zero(mock_db):
    """No parked rows -> a harmless zero (the common case at every turn start)."""
    t = turns.enqueue_turn("s1", "user")
    turns.start_turn(t["id"])
    assert turns.settle_parked_turns("s1") == 0
    # settle_turn's own guard is unchanged: it still refuses parked rows.
    turns.park_turn(t["id"], task_ids=["t-a"])
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

@pytest.mark.parametrize("source", ["user", "queue", "agent_message"])
def test_reap_requeues_messages_and_aborts_automatic_work(mock_db, source):
    # Accepted messages on a dead lease stay queued for the replacement runner.
    tu = turns.enqueue_turn("sUser", source, prompt="preserve this message")
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
    assert turns.get_turn(tu["id"])["prompt"] == "preserve this message"
    assert turns.get_turn(tu["id"])["position"] == tu["position"]
    aborted = turns.get_turn(ta["id"])
    assert aborted["state"] == "aborted"
    assert aborted["stop_reason"] == "lease_expired"


@pytest.mark.parametrize("source", ["user", "queue", "agent_message"])
def test_reap_aborts_interrupted_messages_instead_of_requeueing(mock_db, source):
    """A dead-lease turn the user asked to STOP must never be retried.

    Requeueing an ``interrupting``/``draining`` user turn re-ran the very
    prompt the user interrupted — the message re-appeared in the transcript
    and its Task workers re-spawned on a session the UI showed as stopped
    (the fluid-leg interrupt-replay bug). Only ``running`` user/queue turns
    (a genuine runner crash) are retried.
    """
    ti = turns.enqueue_turn("sInt", source)
    run_dead = runs.claim_run("sInt", "runner", -1)
    turns.start_turn(ti["id"], run_id=run_dead["id"])
    assert turns.interrupt_turn("sInt", "stop") is not None

    td = turns.enqueue_turn("sDrain", source)
    run_dead2 = runs.claim_run("sDrain", "runner", -1)
    turns.start_turn(td["id"], run_id=run_dead2["id"])
    turns.interrupt_turn("sDrain", "stop")
    turns.escalate_to_draining(td["id"])

    reaped = turns.reap_expired()
    reaped_ids = {r["id"] for r in reaped}
    assert ti["id"] in reaped_ids and td["id"] in reaped_ids
    for tid in (ti["id"], td["id"]):
        row = turns.get_turn(tid)
        assert row["state"] == "aborted"
        assert row["stop_reason"] == "lease_expired"


def test_reap_leaves_live_turn_with_valid_lease_alone(mock_db):
    t = turns.enqueue_turn("sLive", "user")
    run_live = runs.claim_run("sLive", "runner", 60)  # healthy lease
    turns.start_turn(t["id"], run_id=run_live["id"])
    reaped = turns.reap_expired()
    assert reaped == []
    assert turns.get_live_turn("sLive")["id"] == t["id"]


def test_reap_skips_dead_lease_turns_the_caller_is_still_executing(mock_db):
    """#1444: the sweeping process passes the turns it is still running; a dead
    lease under one of those must not requeue it for a second start."""
    alive = turns.enqueue_turn("sAlive", "user")
    run_alive = runs.claim_run("sAlive", "runner", -1)
    turns.start_turn(alive["id"], run_id=run_alive["id"])
    gone = turns.enqueue_turn("sGone", "user")
    run_gone = runs.claim_run("sGone", "runner", -1)
    turns.start_turn(gone["id"], run_id=run_gone["id"])

    reaped = turns.reap_expired(skip_turn_ids={alive["id"]})

    assert [r["id"] for r in reaped] == [gone["id"]]
    assert turns.get_turn(alive["id"])["state"] == "running"
    assert turns.get_turn(alive["id"])["run_id"] == run_alive["id"]
    assert turns.get_turn(gone["id"])["state"] == "queued"


def test_settle_turn_run_guard_leaves_a_rebound_turn_alone(mock_db):
    """A runner that lost its lease settles only while the row is still bound
    to its run, never a row the sweep requeued or a new owner started."""
    t = turns.enqueue_turn("sRebound", "user")
    old = runs.claim_run("sRebound", "old", -1)
    turns.start_turn(t["id"], run_id=old["id"])
    turns.reap_expired()  # requeued by another install's sweep
    assert turns.settle_turn(t["id"], "aborted", "lease_lost", run_id=old["id"]) is None
    assert turns.get_turn(t["id"])["state"] == "queued"

    new = runs.claim_run("sRebound", "new", 60)
    turns.start_turn(t["id"], run_id=new["id"])
    assert turns.settle_turn(t["id"], "aborted", "lease_lost", run_id=old["id"]) is None
    assert turns.get_turn(t["id"])["state"] == "running"
    settled = turns.settle_turn(t["id"], "completed", run_id=new["id"])
    assert settled is not None and settled["state"] == "completed"


# ---------------------------------------------------------------------------
# v59 migration: queued_messages -> turns
# ---------------------------------------------------------------------------

def test_migration_seeds_turns_from_queued_messages(test_db):
    """The idempotent v59 migration carries queued_messages rows into turns as
    source='queue' queued turns, preserving order, and never duplicates on a
    re-run."""
    from tests.pg_shim import route_db_to
    from app.db import database

    # queued_messages has an FK to sessions we don't need for this unit test.
    test_db.execute("PRAGMA foreign_keys = OFF")

    with route_db_to(test_db) as shim:
        # Seed the legacy queued_messages TABLE directly. The module that used to
        # write it is deleted (V2 deletion), but the table and the v59 migration
        # that reads it both survive (older installs on the shared DB still write
        # it), so the migration must still carry any existing rows into turns.
        seed = shim.cursor()
        for row in (
            ("qm-1", "sess-1", "first msg", "p1", None, 0),
            ("qm-2", "sess-1", "second msg", None, None, 1),
            ("qm-3", "sess-2", "other session", None, None, 0),
        ):
            seed.execute(
                "INSERT INTO queued_messages "
                "(id, session_id, content, profile_id, project_id, position) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                row,
            )

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


# ---------------------------------------------------------------------------
# list_claimable_sessions — full scan vs. the targeted (session_ids=…) form
# ---------------------------------------------------------------------------

def _seed_claimable_fixture():
    """A mixed fixture: claimable, lease-blocked, dead-lease and non-queued
    sessions, so the two forms have something to actually disagree about."""
    # Plainly claimable: a queued turn, no run at all.
    turns.enqueue_turn("c-plain", "user", prompt="p")
    # Two queued turns for one session -> DISTINCT must still yield one id.
    turns.enqueue_turn("c-dupe", "user", prompt="a")
    turns.enqueue_turn("c-dupe", "queue", prompt="b")
    # Queued, but a LIVE foreign lease blocks the claim.
    turns.enqueue_turn("blocked", "user", prompt="p")
    assert runs.claim_run("blocked", "peer", 60) is not None
    # Queued with an EXPIRED lease -> still claimable (crashed owner).
    turns.enqueue_turn("c-dead-lease", "user", prompt="p")
    assert runs.claim_run("c-dead-lease", "peer", -1) is not None
    # Not queued at all: a running turn is never a claim candidate.
    running = turns.enqueue_turn("running-only", "user", prompt="p")
    assert runs.claim_run("running-only", "me", 60) is not None
    turns.start_turn(running["id"], "me")


def test_claimable_targeted_equals_the_filtered_full_scan(mock_db):
    """The whole point of the targeted form: it must return EXACTLY the subset
    the full scan would have returned for those ids. A silent divergence here
    mis-marks sessions as claimable in the sidebar."""
    _seed_claimable_fixture()

    full = set(turns.list_claimable_sessions())
    assert full == {"c-plain", "c-dupe", "c-dead-lease"}

    # Every prefix of the seeded ids (plus ids that do not exist at all) must
    # agree with the full scan filtered to the same ids.
    universe = [
        "c-plain", "c-dupe", "blocked", "c-dead-lease", "running-only",
        "never-existed",
    ]
    for size in range(1, len(universe) + 1):
        page = universe[:size]
        targeted = set(turns.list_claimable_sessions(session_ids=page))
        assert targeted == full & set(page), page

    # And the reverse order, to prove the answer does not depend on ordering.
    assert set(turns.list_claimable_sessions(session_ids=universe[::-1])) == full


def test_claimable_no_argument_form_is_unchanged(mock_db):
    """Existing callers (the cross-runner claim loop) pass nothing and keep the
    full cluster-wide candidate set."""
    _seed_claimable_fixture()
    assert sorted(turns.list_claimable_sessions()) == [
        "c-dead-lease", "c-dupe", "c-plain",
    ]
    assert sorted(turns.list_claimable_sessions(None)) == [
        "c-dead-lease", "c-dupe", "c-plain",
    ]


def test_claimable_empty_id_list_returns_nothing_without_querying(mock_db):
    """``IN ()`` is a syntax error in Postgres: an empty page short-circuits.

    An empty list is NOT the same as ``None`` — it means "these zero sessions",
    whose claimable subset is empty.
    """
    _seed_claimable_fixture()
    assert turns.list_claimable_sessions(session_ids=[]) == []
    # Blank/None ids are dropped; a page of only those is also empty.
    assert turns.list_claimable_sessions(session_ids=["", None]) == []


def test_claimable_targeted_deduplicates_repeated_ids(mock_db):
    """A repeated id must not produce a repeated (or missing) result."""
    _seed_claimable_fixture()
    assert turns.list_claimable_sessions(
        session_ids=["c-plain", "c-plain", "c-plain"]
    ) == ["c-plain"]
