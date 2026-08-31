"""Unit tests for app/core/turn_scheduler.py — the settlement policies
(v2 core, Phase 2 A5).

Plan §5 coverage: policy ORDER (failover before queue drain before
auto-continue), the queue drain firing on EVERY settle outcome
(completed/parked/aborted/error — the old F4 preserve-on-stop rule is retired),
failover cap + no message re-append, the durable #557 auto-continue
derivation, unpark prompt synthesis, and the shutdown kick guard.

The turns/session_events tables are exercised for real (pg_shim SQLite).
Collaborators are patched at the CONSUMER module bindings the scheduler
resolves lazily at call time (``session_runtime.execute``,
``account_failover.attempt_failover``, ``compact_continue`` predicates,
``query_engine._read_task_output_file``) — the lazy-import-mock-freeze rule.
"""

import asyncio
import logging
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.core import idle_reader, session_runtime, turn_manager, turn_scheduler
from app.core.task_registry import task_registry
from app.core.account_failover import FailoverDecision
from app.core.compact_continue import AUTO_CONTINUE_PROMPT
from app.db import session_events as se_db
from app.db import turns as turns_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    session_runtime.register_scheduler(None)
    idle_reader.register_on_task_terminal(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    yield
    session_runtime.register_scheduler(None)
    idle_reader.register_on_task_terminal(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _terminal_event(
    session_id,
    turn_id,
    *,
    stop_reason="end_turn",
    api_error_status=None,
    api_error_message=None,
):
    """Seed the settled turn's terminal turn.completed event — what the A5
    recorder amendment writes (stop_reason + optional api_error_*)."""
    payload = {"stop_reason": stop_reason, "usage": {}}
    if api_error_status is not None:
        payload["api_error_status"] = api_error_status
    if api_error_message is not None:
        payload["api_error_message"] = api_error_message
    se_db.append_events(session_id, [{
        "type": "turn.completed", "payload": payload, "turn_id": turn_id,
    }])


def _settled_turn(
    session_id,
    *,
    source="user",
    prompt="hi",
    outcome="completed",
    stop_reason="end_turn",
    api_error_status=None,
    terminal_event=True,
    **enqueue_kw,
):
    """Enqueue → start → settle one turn, seeding its terminal event."""
    turn = turns_db.enqueue_turn(session_id, source, prompt=prompt, **enqueue_kw)
    assert turns_db.start_turn(turn["id"], None, "runner") is not None
    turns_db.settle_turn(turn["id"], outcome)
    if terminal_event and outcome == "completed":
        _terminal_event(
            session_id, turn["id"],
            stop_reason=stop_reason, api_error_status=api_error_status,
        )
    return turn


async def _drain_kicks():
    """Await every in-flight kick task the scheduler spawned."""
    for _ in range(10):
        tasks = list(turn_scheduler._kick_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _by_source(session_id, source):
    return [t for t in turns_db.get_queued_turns(session_id) if t["source"] == source]


def _patch_execute():
    return patch("app.core.session_runtime.execute", new=AsyncMock(return_value="completed"))


# ---------------------------------------------------------------------------
# (2) QUEUE DRAIN + F4
# ---------------------------------------------------------------------------

async def test_completed_settlement_drains_front_queued_turn(mock_db):
    settled = _settled_turn("s1", prompt="first")
    queued = turns_db.enqueue_turn("s1", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s1", settled["id"], "completed")
        await _drain_kicks()

    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == queued["id"]


async def test_aborted_settlement_drains_front_queued_turn(mock_db):
    # (b) A user stop settles aborted → the queued row DRAINS (supersedes the old
    # F4 preserve-on-stop rule: a queued message sends after the turn stops).
    settled = _settled_turn("s2", outcome="aborted", terminal_event=False)
    queued = turns_db.enqueue_turn("s2", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s2", settled["id"], "aborted")
        await _drain_kicks()

    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == queued["id"]


async def test_parked_settlement_drains_front_queued_turn(mock_db):
    # (a) A parked settlement (background work still running) DRAINS the queued
    # row: it runs concurrently with that background work (park released the
    # live-turn slot). A message queued BEFORE park is now consistent with one
    # queued AFTER park.
    turn = turns_db.enqueue_turn("s3", "user", prompt="bg work")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["task-1"])
    queued = turns_db.enqueue_turn("s3", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s3", turn["id"], "parked")
        await _drain_kicks()

    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == queued["id"]


async def test_error_settlement_drains_front_queued_turn(mock_db):
    # (c) An error settlement (the runtime settles error paths as aborted; a bare
    # "error" outcome is exercised here directly) DRAINS the queued row — each
    # drain consumes exactly one queued row, so there is no retry-loop risk.
    settled = _settled_turn("s2e", outcome="aborted", terminal_event=False)
    queued = turns_db.enqueue_turn("s2e", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s2e", settled["id"], "error")
        await _drain_kicks()

    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == queued["id"]


async def test_completed_empty_queue_natural_end_does_nothing(mock_db):
    settled = _settled_turn("s4")  # end_turn, nothing queued

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s4", settled["id"], "completed")
        await _drain_kicks()

    execute.assert_not_awaited()
    assert turns_db.get_queued_turns("s4") == []


async def test_parked_empty_queue_no_spurious_turn(mock_db):
    # (d) parked + nothing queued → the drain finds no front row and starts
    # nothing (the wake still arrives later via on_task_terminal).
    turn = turns_db.enqueue_turn("s5", "user", prompt="bg work")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["task-1"])

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s5", turn["id"], "parked")
        await _drain_kicks()

    execute.assert_not_awaited()
    assert turns_db.get_queued_turns("s5") == []


async def test_aborted_empty_queue_no_spurious_turn(mock_db):
    # (d) aborted + nothing queued → nothing auto-starts.
    settled = _settled_turn("s6", outcome="aborted", terminal_event=False)

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s6", settled["id"], "aborted")
        await _drain_kicks()

    execute.assert_not_awaited()
    assert turns_db.get_queued_turns("s6") == []


# ---------------------------------------------------------------------------
# (1) FAILOVER — order, attempts accumulation, cap, no re-append
# ---------------------------------------------------------------------------

async def test_failover_runs_before_queue_drain(mock_db):
    # A 429 settlement with a non-empty queue must STILL enqueue the failover
    # retry — if drain ran first it would return before the failover check —
    # AND the retry must sort AHEAD of the previously-queued user turn (the
    # retry continues the settled turn; it is not preempted by work a user
    # queued during the failed turn).
    settled = _settled_turn(
        "f1", prompt="retry me", api_error_status=429,
        payload={"api_user_id": "u1"},
        profile_id="p1", project_id="proj-1", worktree_id="wt-1",
    )
    queued = turns_db.enqueue_turn("f1", "queue", prompt="queued behind")

    async def fake_failover(session_id, api_user_id, attempted):
        assert session_id == "f1"
        assert api_user_id == "u1"
        assert attempted == set()
        attempted.update({"acc-1", "acc-2"})  # mutated like the real one
        return FailoverDecision(retry=True, account_id="acc-2", switched=True)

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.account_failover.attempt_failover", new=fake_failover,
        ))
        await turn_scheduler.on_turn_settled("f1", settled["id"], "completed")
        await _drain_kicks()

    retries = _by_source("f1", "failover")
    assert len(retries) == 1
    retry = retries[0]
    assert retry["prompt"] == "retry me"
    assert retry["parent_turn_id"] == settled["id"]
    assert retry["payload"]["failover_attempted"] == ["acc-1", "acc-2"]
    assert retry["payload"]["api_user_id"] == "u1"
    assert retry["profile_id"] == "p1"
    assert retry["project_id"] == "proj-1"
    assert retry["worktree_id"] == "wt-1"
    # Front-of-queue: the retry sorts strictly before the user turn queued
    # during the failed turn, so the kick STARTS the retry, not that turn.
    assert retry["position"] < queued["position"]
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == retry["id"]

    # No re-append: the retry adds NO message.user / no new events — the log
    # still holds only the seeded terminal event.
    types = [r["event_type"] for r in se_db.get_events("f1")]
    assert types == ["turn.completed"]


async def test_failover_attempts_accumulate_across_retries(mock_db):
    settled = _settled_turn(
        "f2", source="failover", prompt="retry me", api_error_status=429,
        payload={"failover_attempted": ["acc-1"], "api_user_id": "u2"},
    )

    async def fake_failover(session_id, api_user_id, attempted):
        assert attempted == {"acc-1"}  # prior attempts derived from payload
        attempted.add("acc-2")
        return FailoverDecision(retry=True, account_id="acc-2", switched=True)

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.account_failover.attempt_failover", new=fake_failover,
        ))
        await turn_scheduler.on_turn_settled("f2", settled["id"], "completed")
        await _drain_kicks()

    (retry,) = _by_source("f2", "failover")
    assert retry["payload"]["failover_attempted"] == ["acc-1", "acc-2"]


async def test_failover_cap_exhausted_no_retry(mock_db):
    settled = _settled_turn(
        "f3", prompt="retry me", api_error_status=429,
        payload={"failover_attempted": ["acc-1", "acc-2"]},
    )

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.account_failover.attempt_failover",
            new=AsyncMock(return_value=FailoverDecision(retry=False)),
        ))
        await turn_scheduler.on_turn_settled("f3", settled["id"], "completed")
        await _drain_kicks()

    assert _by_source("f3", "failover") == []
    execute.assert_not_awaited()  # nothing queued, nothing started


async def test_chained_failover_exhaustion_terminates(mock_db):
    # A source=failover RETRY turn itself settles with a 429; its accumulated
    # payload.failover_attempted leaves attempt_failover no candidate
    # (retry=False) → NO new enqueue, no crash — the chain terminates cleanly.
    settled = _settled_turn(
        "f6", source="failover", prompt="retry me", api_error_status=429,
        payload={"failover_attempted": ["acc-1", "acc-2"], "api_user_id": "u6"},
    )

    async def fake_failover(session_id, api_user_id, attempted):
        # Prior attempts derive from the retry turn's own payload.
        assert attempted == {"acc-1", "acc-2"}
        return FailoverDecision(retry=False)

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.account_failover.attempt_failover", new=fake_failover,
        ))
        await turn_scheduler.on_turn_settled("f6", settled["id"], "completed")
        await _drain_kicks()

    assert _by_source("f6", "failover") == []  # chain terminated, no new turn
    execute.assert_not_awaited()               # nothing queued, nothing started


async def test_non_429_completion_never_calls_failover(mock_db):
    settled = _settled_turn("f4", prompt="fine")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        attempt = stack.enter_context(patch(
            "app.core.account_failover.attempt_failover", new=AsyncMock(),
        ))
        await turn_scheduler.on_turn_settled("f4", settled["id"], "completed")
        await _drain_kicks()

    attempt.assert_not_awaited()
    assert _by_source("f4", "failover") == []


# ---------------------------------------------------------------------------
# (3) AUTO-CONTINUE — boundary cut + durable #557 derivation
# ---------------------------------------------------------------------------

def _patch_over_threshold(stack, over=True, tokens=150_000):
    return stack.enter_context(patch(
        "app.core.compact_continue.context_over_threshold",
        new=AsyncMock(return_value=(over, tokens)),
    ))


async def test_auto_continue_on_boundary_cut(mock_db):
    settled = _settled_turn(
        "a1", prompt="long task", stop_reason="tool_use",
        profile_id="p1", payload={"api_user_id": "u1"},
        overrides={"model": "prov:prov-1:m1"},
    )

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        _patch_over_threshold(stack)
        await turn_scheduler.on_turn_settled("a1", settled["id"], "completed")
        await _drain_kicks()

    (cont,) = _by_source("a1", "auto_continue")
    assert cont["prompt"] == AUTO_CONTINUE_PROMPT
    assert cont["parent_turn_id"] == settled["id"]
    assert cont["profile_id"] == "p1"
    assert cont["payload"]["api_user_id"] == "u1"
    # The continuation runs under the settled turn's own per-turn settings.
    assert cont["overrides"] == {"model": "prov:prov-1:m1"}
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == cont["id"]


async def test_auto_continue_skipped_below_threshold(mock_db):
    settled = _settled_turn("a2", stop_reason="tool_use")

    with ExitStack() as stack:
        _patch_over_threshold(stack, over=False, tokens=100)
        stack.enter_context(_patch_execute())
        await turn_scheduler.on_turn_settled("a2", settled["id"], "completed")
        await _drain_kicks()

    assert _by_source("a2", "auto_continue") == []


async def test_auto_continue_skipped_when_queue_nonempty(mock_db):
    # A queued turn resumes work on its own — drain wins, no nudge.
    settled = _settled_turn("a3", stop_reason="tool_use")
    turns_db.enqueue_turn("a3", "queue", prompt="already queued")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        _patch_over_threshold(stack)
        await turn_scheduler.on_turn_settled("a3", settled["id"], "completed")
        await _drain_kicks()

    assert _by_source("a3", "auto_continue") == []


async def test_557_settled_auto_continue_turn_never_chains(mock_db):
    # The continuation itself cut at the boundary again → never re-nudge.
    settled = _settled_turn("a4", source="auto_continue", stop_reason="tool_use")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        _patch_over_threshold(stack)
        await turn_scheduler.on_turn_settled("a4", settled["id"], "completed")
        await _drain_kicks()

    assert _by_source("a4", "auto_continue") == []


async def test_557_durable_latch_and_rearm_on_user_turn(mock_db):
    # History: user → auto_continue → (settling now) unpark, all with no user
    # message since the nudge → latched, derived purely from the turns table.
    _settled_turn("a5", source="user", prompt="start")
    _settled_turn("a5", source="auto_continue", prompt=AUTO_CONTINUE_PROMPT)
    wake = _settled_turn("a5", source="unpark", prompt="<task-notification/>",
                         stop_reason="tool_use")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        _patch_over_threshold(stack)
        await turn_scheduler.on_turn_settled("a5", wake["id"], "completed")
        await _drain_kicks()
    assert _by_source("a5", "auto_continue") == []  # still latched

    # A genuine user turn settles → the latch re-arms automatically.
    user2 = _settled_turn("a5", source="user", prompt="go on",
                          stop_reason="tool_use")
    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        _patch_over_threshold(stack)
        await turn_scheduler.on_turn_settled("a5", user2["id"], "completed")
        await _drain_kicks()

    (cont,) = _by_source("a5", "auto_continue")
    assert cont["parent_turn_id"] == user2["id"]


# ---------------------------------------------------------------------------
# (4) UNPARK — synth prompt + context inheritance
# ---------------------------------------------------------------------------

async def test_unpark_synthesizes_notification_turn(mock_db):
    parked = turns_db.enqueue_turn(
        "u1", "user", prompt="kick off bg work",
        profile_id="p9", project_id="proj-9", worktree_id="wt-9",
        payload={"api_user_id": "u-api"},
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["task-7"])
    # task-7 is the parent's OWN background work (ownership gate admits it). It
    # terminates KILLED: the app still owns a kill wake (there is no CLI at-rest
    # resume for a kill, so the app enqueue is the sole delivery), whereas a
    # *completed* top-level agent while parked is CLI-owned and suppressed
    # (A-1). This test is the sole coverage of the wake PAYLOAD shape, so it is
    # repointed to the terminal the app still owns.
    task_registry.start_task("u1", "task-7", "agent", "bg", tool_use_id="tu-7")
    task_registry.mark_background_launch("u1", "tu-7")

    frame = {
        "task_id": "task-7", "status": "killed", "tool_use_id": "tu-7",
        "summary": "crawl finished", "output_file": "",
        "usage": {"total_tokens": 123, "tool_uses": 4, "duration_ms": 5000},
    }

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("u1", frame)
        await _drain_kicks()

    (wake,) = _by_source("u1", "unpark")
    assert "<task-notification>" in wake["prompt"]
    assert "<task-id>task-7</task-id>" in wake["prompt"]
    assert "<summary>crawl finished</summary>" in wake["prompt"]
    # The frame has no output_file, so the result chain lands back on the summary.
    # The payload is emitted ONCE — <summary> carries it and <result> is dropped,
    # rather than the same text appearing verbatim in both elements.
    assert "<result>" not in wake["prompt"]
    assert wake["parent_turn_id"] == parked["id"]
    assert wake["profile_id"] == "p9"
    assert wake["project_id"] == "proj-9"
    assert wake["worktree_id"] == "wt-9"
    # Payload carries the inherited context, the derived task_ids, AND the raw
    # pending frames (so a follow-on completion can merge + rebuild the prompt).
    assert wake["payload"] == {
        "api_user_id": "u-api",
        "task_ids": ["task-7"],
        "pending_frames": [frame],
    }
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == wake["id"]


def _terminal_frame(task_id, summary):
    return {
        "task_id": task_id, "status": "completed", "tool_use_id": f"tu-{task_id}",
        "summary": summary, "output_file": "", "usage": {},
    }


async def test_two_terminal_frames_batch_into_one_unpark(mock_db):
    # A live turn keeps the session busy (kick can start nothing); two DIFFERENT
    # tasks going terminal fold into ONE queued unpark whose prompt carries both
    # notifications and whose payload.task_ids carries both.
    live = turns_db.enqueue_turn("ub1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")

    f1 = _terminal_frame("task-a", "alpha done")
    f2 = _terminal_frame("task-b", "beta done")
    # Both are the parent's OWN background work (ownership gate admits them).
    for tid in ("task-a", "task-b"):
        task_registry.start_task("ub1", tid, "agent", "bg", tool_use_id=f"tu-{tid}")
        task_registry.mark_background_launch("ub1", f"tu-{tid}")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ub1", f1)
        await _drain_kicks()
        await turn_scheduler.on_task_terminal("ub1", f2)
        await _drain_kicks()

    (wake,) = _by_source("ub1", "unpark")
    assert "<task-id>task-a</task-id>" in wake["prompt"]
    assert "<task-id>task-b</task-id>" in wake["prompt"]
    assert "<summary>alpha done</summary>" in wake["prompt"]
    assert "<summary>beta done</summary>" in wake["prompt"]
    assert wake["payload"]["task_ids"] == ["task-a", "task-b"]
    assert wake["payload"]["pending_frames"] == [f1, f2]


async def test_user_canceled_task_terminal_never_enqueues_wake(mock_db):
    session_id = "cancel-wake"
    task_id = "task-canceled"
    task_registry.start_task(
        session_id, task_id, "agent", "bg", tool_use_id="tu-canceled",
    )
    task_registry.mark_background_launch(session_id, "tu-canceled")
    task_registry.suppress_wake(session_id, task_id)

    try:
        with _patch_execute() as execute:
            await turn_scheduler.on_task_terminal(
                session_id, _terminal_frame(task_id, "too late"),
            )
            await _drain_kicks()

        assert _by_source(session_id, "unpark") == []
        execute.assert_not_awaited()
    finally:
        task_registry.clear_session(session_id)


def test_discard_queued_task_wakes_aborts_canceled_only_turn(mock_db):
    frame = _terminal_frame("task-canceled", "done")
    wake = turns_db.enqueue_turn(
        "cancel-only", "unpark", prompt="wake",
        payload={"task_ids": ["task-canceled"], "pending_frames": [frame]},
    )

    changed = turn_scheduler.discard_queued_task_wakes(
        "cancel-only", ["task-canceled"],
    )

    assert changed == 1
    assert turns_db.get_turn(wake["id"])["state"] == "aborted"
    assert _by_source("cancel-only", "unpark") == []


def test_discard_queued_task_wakes_preserves_unrelated_results(mock_db):
    canceled = _terminal_frame("task-canceled", "cancel me")
    completed = _terminal_frame("task-completed", "keep me")
    wake = turns_db.enqueue_turn(
        "cancel-mixed", "unpark", prompt="old wake",
        payload={
            "task_ids": ["task-canceled", "task-completed"],
            "pending_frames": [canceled, completed],
        },
    )

    with patch("app.core.query_engine._read_task_output_file", return_value=""):
        changed = turn_scheduler.discard_queued_task_wakes(
            "cancel-mixed", ["task-canceled"],
        )

    assert changed == 1
    updated = turns_db.get_turn(wake["id"])
    assert updated["state"] == "queued"
    assert updated["payload"]["task_ids"] == ["task-completed"]
    assert updated["payload"]["pending_frames"] == [completed]
    assert "task-canceled" not in updated["prompt"]
    assert "task-completed" in updated["prompt"]
    assert "keep me" in updated["prompt"]


async def test_duplicate_terminal_frame_is_complete_noop(mock_db):
    # A live turn keeps the unpark queued between calls; a duplicate frame for a
    # task already folded in is a COMPLETE no-op — one queued turn, prompt +
    # payload unchanged, and no kick.
    live = turns_db.enqueue_turn("ub2", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    frame = _terminal_frame("task-d", "done once")
    # task-d is the parent's OWN background work (ownership gate admits it).
    task_registry.start_task("ub2", "task-d", "agent", "bg", tool_use_id="tu-task-d")
    task_registry.mark_background_launch("ub2", "tu-task-d")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ub2", frame)
        await _drain_kicks()
        (before,) = _by_source("ub2", "unpark")
        prompt_before, payload_before = before["prompt"], before["payload"]
        execute.reset_mock()

        await turn_scheduler.on_task_terminal("ub2", frame)  # duplicate
        await _drain_kicks()

    wakes = _by_source("ub2", "unpark")
    assert len(wakes) == 1
    assert wakes[0]["prompt"] == prompt_before
    assert wakes[0]["payload"] == payload_before
    execute.assert_not_awaited()  # the (a) no-op path does not even kick


async def test_unpark_enqueued_stays_queued_while_turn_live(mock_db):
    # on_task_terminal fired while a turn is LIVE: the wake is enqueued and
    # REMAINS queued; the kick loses gracefully (no exception, no second live
    # turn).
    live = turns_db.enqueue_turn("ub3", "user", prompt="mid turn")
    turns_db.start_turn(live["id"], None, "runner")
    frame = _terminal_frame("task-x", "bg done")
    # task-x is the parent's OWN background work (ownership gate admits it).
    task_registry.start_task("ub3", "task-x", "agent", "bg", tool_use_id="tu-task-x")
    task_registry.mark_background_launch("ub3", "tu-task-x")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ub3", frame)
        await _drain_kicks()

    (wake,) = _by_source("ub3", "unpark")
    assert wake["state"] == "queued"
    # The live turn is untouched — no second live turn was created.
    assert turns_db.get_live_turn("ub3")["id"] == live["id"]


# ---------------------------------------------------------------------------
# (4) UNPARK — ownership gate (subagent task-notification leak, R1)
# ---------------------------------------------------------------------------

async def test_subagent_owned_terminal_without_parked_agent_no_unpark(mock_db):
    # A background task launched BY a sub-agent (grandchild) goes terminal, but
    # NO background agent is still running (orphan) → the main session must NOT
    # be woken with a <task-notification> for work it never launched.
    frame = _terminal_frame("gc-1", "grandchild done")
    task_registry.mark_subagent_background_launch("sg1", frame["tool_use_id"])

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg1", frame)
        await _drain_kicks()

    assert _by_source("sg1", "unpark") == []
    execute.assert_not_awaited()


async def test_subagent_owned_terminal_with_active_agent_suppressed(mock_db):
    # #690: a grandchild STEP of a still-running background sub-agent (NOT the
    # parent's own work) no longer produces a parent-visible turn. The retired
    # PUMP lane used to enqueue a source="unpark_pump" wake here — the storm
    # source. The parent hears about this work only via the owner sub-agent's
    # OWN-lane terminal (or the CLI resumes the owner natively).
    task_registry.start_task("sg2", "subagent-1", "agent", "spawning sub-agent")
    frame = _terminal_frame("gc-2", "grandchild done")
    task_registry.mark_subagent_background_launch("sg2", frame["tool_use_id"])

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg2", frame)
        await _drain_kicks()

    assert _by_source("sg2", "unpark") == []
    assert _by_source("sg2", "unpark_pump") == []
    execute.assert_not_awaited()


async def test_synchronous_subagent_at_rest_no_unpark(mock_db):
    # A synchronous sub-agent (no background lane armed) comes to rest AFTER its
    # turn: not own work, no running agent → suppress the spurious "nothing to
    # act on" wake.
    frame = _terminal_frame("sync-1", "nothing to act on")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg3", frame)
        await _drain_kicks()

    assert _by_source("sg3", "unpark") == []
    execute.assert_not_awaited()


async def test_parent_owned_terminal_still_fires_unpark(mock_db):
    # The parent's OWN background work is unchanged — it still fires the unpark
    # regardless of whether any background agent is currently running.
    frame = _terminal_frame("own-1", "parent bg done")
    task_registry.start_task(
        "sg4", "own-1", "agent", "bg", tool_use_id=frame["tool_use_id"]
    )
    task_registry.mark_background_launch("sg4", frame["tool_use_id"])

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg4", frame)
        await _drain_kicks()

    (wake,) = _by_source("sg4", "unpark")
    assert "<task-id>own-1</task-id>" in wake["prompt"]
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == wake["id"]


async def test_unknown_task_with_active_agent_suppressed(mock_db):
    # #690: an UNKNOWN terminal task (no ownership lane armed) is NOT the parent's
    # own background work, so with the PUMP lane retired it produces no parent-
    # visible turn — whether or not a background sub-agent is still running. This
    # is exactly the tool_use_id-less shell case that used to fire blindly.
    task_registry.start_task("sg5a", "subagent-a", "agent", "spawning")
    frame_a = _terminal_frame("gc-5", "grandchild done")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg5a", frame_a)
        await _drain_kicks()

    assert _by_source("sg5a", "unpark") == []
    assert _by_source("sg5a", "unpark_pump") == []

    # (b) same unknown task, NO running agent → equally suppressed.
    frame_b = _terminal_frame("gc-6", "grandchild done")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg5b", frame_b)
        await _drain_kicks()

    assert _by_source("sg5b", "unpark") == []
    assert _by_source("sg5b", "unpark_pump") == []


# ---------------------------------------------------------------------------
# (4) #690 — PUMP lane retired: child/grandchild steps produce NO parent turn
# ---------------------------------------------------------------------------
# The per-child PUMP lane (immediate + deferred) is DELETED. A child/grandchild
# completion while its owner sub-agent is still alive no longer enqueues any
# parent-visible turn (source="unpark" or the retired source="unpark_pump").
# The parent hears about delegated work ONLY via the owner task's OWN-lane
# terminal; a silent turn is valid. Reference-harness contract: a background
# child notifies only its owning agent.


def _child_of_running_owner(
    session, *, owner_task="owner-A", owner_tuid="tu-A",
    child_task="child-B", child_tuid="tu-B",
):
    """Owner = the parent's OWN background sub-agent (running); child = a
    grandchild it launched, linked owner<-child. Returns the child terminal
    frame. The linkage that used to route the child into the (deferred) PUMP
    lane must now route it to the else-suppress branch."""
    task_registry.start_task(session, owner_task, "agent", "sub",
                             tool_use_id=owner_tuid)
    task_registry.mark_background_launch(session, owner_tuid)
    task_registry.start_task(session, child_task, "agent", "gc",
                             tool_use_id=child_tuid)
    task_registry.link_child_to_owner(session, child_tuid, owner_tuid)
    return {
        "task_id": child_task, "status": "completed", "tool_use_id": child_tuid,
        "summary": "step done", "output_file": "", "usage": {},
    }


async def test_child_step_while_owner_alive_no_pump(mock_db):
    # #690 core: a RESOLVABLE child of a still-running owner completes -> NO turn
    # is enqueued at all (neither unpark nor the retired unpark_pump). Replaces
    # the deleted deferred-pump quiescence dance.
    child_frame = _child_of_running_owner("cq1")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("cq1", child_frame)
        await _drain_kicks()

    assert _by_source("cq1", "unpark") == []
    assert _by_source("cq1", "unpark_pump") == []
    execute.assert_not_awaited()


async def test_three_shells_from_subagent_no_pump_storm(mock_db):
    # #690 regression ("3 shells -> 3 wakes"): a sub-agent launches three
    # background shells that lack a resolvable parent tool_use_id (the exact
    # storm trigger). All three going terminal while the sub-agent runs produce
    # ZERO parent-visible turns.
    task_registry.start_task("st1", "subagent-1", "agent", "spawning shells")
    shells = [
        {"task_id": f"shell-{n}", "status": "completed",
         "tool_use_id": f"tu-shell-{n}", "summary": f"done-{n}",
         "output_file": "", "usage": {}}
        for n in range(3)
    ]

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        for f in shells:
            await turn_scheduler.on_task_terminal("st1", f)
            await _drain_kicks()

    assert _by_source("st1", "unpark") == []
    assert _by_source("st1", "unpark_pump") == []
    execute.assert_not_awaited()


async def test_owner_own_lane_completion_one_notification(mock_db):
    # #690: the parent's OWN background sub-agent (the owner) coming to rest is
    # the ONLY path that wakes the parent -- exactly ONE unpark. A grandchild
    # step that completed earlier under this owner adds nothing (suppressed).
    live = turns_db.enqueue_turn("ow1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    task_registry.start_task("ow1", "owner-A", "agent", "sub", tool_use_id="tu-A")
    task_registry.mark_background_launch("ow1", "tu-A")
    task_registry.start_task("ow1", "child-B", "agent", "gc", tool_use_id="tu-B")
    task_registry.link_child_to_owner("ow1", "tu-B", "tu-A")
    child = {"task_id": "child-B", "status": "completed", "tool_use_id": "tu-B",
             "summary": "child step", "output_file": "", "usage": {}}
    owner = {"task_id": "owner-A", "status": "completed", "tool_use_id": "tu-A",
             "summary": "owner done", "output_file": "",
             "usage": {"total_tokens": 42, "tool_uses": 3, "duration_ms": 900}}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ow1", child)
        await _drain_kicks()
        assert _by_source("ow1", "unpark") == []
        assert _by_source("ow1", "unpark_pump") == []
        await turn_scheduler.on_task_terminal("ow1", owner)
        await _drain_kicks()

    wakes = _by_source("ow1", "unpark")
    assert len(wakes) == 1
    assert "<task-id>owner-A</task-id>" in wakes[0]["prompt"]
    assert wakes[0]["payload"]["task_ids"] == ["owner-A"]
    assert _by_source("ow1", "unpark_pump") == []


async def test_own_lane_unaffected_by_active_agent(mock_db):
    # #690 does NOT touch the OWN lane: the parent's own background work still
    # fires its rich unpark even while a background sub-agent is running.
    task_registry.start_task("oa1", "subagent-1", "agent", "spawning")
    frame = _terminal_frame("own-1", "parent bg done")
    task_registry.start_task(
        "oa1", "own-1", "agent", "bg", tool_use_id=frame["tool_use_id"]
    )
    task_registry.mark_background_launch("oa1", frame["tool_use_id"])

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("oa1", frame)
        await _drain_kicks()

    (wake,) = _by_source("oa1", "unpark")
    assert "<task-id>own-1</task-id>" in wake["prompt"]
    assert _by_source("oa1", "unpark_pump") == []


# ---------------------------------------------------------------------------
# (4) #690 — content-empty guard at the single enqueue choke point
# ---------------------------------------------------------------------------

def test_frame_has_renderable_content_predicate():
    # Identity (task_id / tool_use_id) or result (summary / output_file) counts
    # as renderable; a frame with none of them is contentless.
    assert turn_scheduler._frame_has_renderable_content({"task_id": "t"})
    assert turn_scheduler._frame_has_renderable_content({"tool_use_id": "u"})
    assert turn_scheduler._frame_has_renderable_content({"summary": "s"})
    assert turn_scheduler._frame_has_renderable_content({"output_file": "/f"})
    assert not turn_scheduler._frame_has_renderable_content({})
    assert not turn_scheduler._frame_has_renderable_content({"status": "completed"})
    assert not turn_scheduler._frame_has_renderable_content(
        {"task_id": "", "tool_use_id": None, "summary": "", "output_file": ""}
    )


async def test_empty_frame_dropped_at_choke_point(mock_db):
    # A wholly-contentless frame (only a status) is dropped at the choke point:
    # no turn is enqueued AND the guard short-circuits BEFORE any lane consults
    # the registry (proving it is the guard, not the else-suppress branch).
    from unittest.mock import MagicMock

    empty = {"status": "completed"}
    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        spy = MagicMock(return_value=False)
        stack.enter_context(patch.object(
            task_registry, "is_own_background_task", spy
        ))
        await turn_scheduler.on_task_terminal("ef1", empty)
        await _drain_kicks()

    assert _by_source("ef1", "unpark") == []
    assert _by_source("ef1", "unpark_pump") == []
    execute.assert_not_awaited()
    spy.assert_not_called()  # guard returned before lane resolution


async def test_content_bearing_frame_not_dropped_by_guard(mock_db):
    # The guard must NOT drop a bare TaskUpdated frame (task_id + tool_use_id,
    # no summary/result) -- that is a legitimate OWN-lane wake (later upgraded by
    # its rich frame, #688 A-2). Here it is the parent's own work -> one unpark.
    task_registry.start_task("cb1", "agent-1", "agent", "bg", tool_use_id="tu-cb1")
    task_registry.mark_background_launch("cb1", "tu-cb1")
    bare = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-cb1"}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("cb1", bare)
        await _drain_kicks()

    (wake,) = _by_source("cb1", "unpark")
    assert "<task-id>agent-1</task-id>" in wake["prompt"]


# ---------------------------------------------------------------------------
# (4) UNPARK — A-1 CLI-owned at-rest suppression (bug 1: duplicate/empty wakes)
# ---------------------------------------------------------------------------

async def test_completed_top_level_agent_no_app_unpark_cli_owns(mock_db):
    # A top-level backgrounded AGENT completes while its parent turn is PARKED:
    # the CLI itself injects the rich <task-notification> follow-up turn, so the
    # app's own content-poor wake would be a SECOND, empty delivery. A-1 suppresses
    # the app enqueue entirely. (Fails before A-1: one empty unpark row.)
    parked = turns_db.enqueue_turn("ca1", "user", prompt="kick off bg")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["agent-1"])
    task_registry.start_task("ca1", "agent-1", "agent", "bg", tool_use_id="tu-ca1")
    task_registry.mark_background_launch("ca1", "tu-ca1")
    # The bare TaskUpdated-built frame (content-poor: no summary/output_file/usage).
    bare = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-ca1"}

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ca1", bare)
        await _drain_kicks()

    assert _by_source("ca1", "unpark") == []  # CLI owns the wake; app adds nothing
    execute.assert_not_awaited()


async def test_completed_provider_agent_worker_is_never_cli_owned(mock_db):
    """A ProviderAgent worker is registered ``kind == "agent"`` (load-bearing —
    it pins the parent in the session pool), so a COMPLETED worker resting under
    a PARKED parent matches every other condition of the A-1 suppression. But
    there is no CLI task behind it at all: the worker is a separate session
    dispatched from an MCP tool handler, so no CLI follow-up turn is ever coming
    and suppressing the app wake would strand the parent parked forever. The
    ``origin`` marker on the frame is what keeps A-1 scoped to genuine CLI-native
    sub-agents."""
    parked = turns_db.enqueue_turn("pw1", "user", prompt="dispatch a worker")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["worker-1"])
    task_registry.start_task("pw1", "worker-1", "agent", "worker", tool_use_id="tu-pw1")
    task_registry.mark_background_launch("pw1", "tu-pw1")
    frame = {
        "task_id": "worker-1", "status": "completed", "tool_use_id": "tu-pw1",
        "summary": "the worker's report",
        "origin": turn_scheduler.PROVIDER_AGENT_ORIGIN,
    }

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        signal = await turn_scheduler.on_task_terminal("pw1", frame)
        await _drain_kicks()

    assert signal is None  # never the cli-owned-wake grace signal
    (wake,) = _by_source("pw1", "unpark")
    assert "<task-id>worker-1</task-id>" in wake["prompt"]
    assert "<summary>the worker's report</summary>" in wake["prompt"]
    assert wake["parent_turn_id"] == parked["id"]
    execute.assert_awaited_once()


async def test_second_provider_agent_frame_does_not_double_wake(mock_db):
    """The one-wake-per-task ``mark_woken`` latch is untouched: a duplicate
    terminal frame for the same worker (a retry, a racing teardown) finds the
    latch set and produces no second wake."""
    parked = turns_db.enqueue_turn("pw2", "user", prompt="dispatch a worker")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["worker-2"])
    task_registry.start_task("pw2", "worker-2", "agent", "worker", tool_use_id="tu-pw2")
    task_registry.mark_background_launch("pw2", "tu-pw2")
    frame = {
        "task_id": "worker-2", "status": "error", "tool_use_id": "tu-pw2",
        "summary": "worker failed: provider blew up",
        "origin": turn_scheduler.PROVIDER_AGENT_ORIGIN,
    }

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("pw2", frame)
        await _drain_kicks()
        first = _by_source("pw2", "unpark")
        await turn_scheduler.on_task_terminal("pw2", dict(frame))
        await _drain_kicks()

    assert len(first) <= 1
    assert len(_by_source("pw2", "unpark")) == len(first)


async def test_origin_marker_leaves_cli_native_suppression_intact(mock_db):
    """Guard the scope of the marker: an ordinary CLI-native frame (no
    ``origin``) still takes the A-1 suppression branch."""
    parked = turns_db.enqueue_turn("pw3", "user", prompt="kick off bg")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["agent-3"])
    task_registry.start_task("pw3", "agent-3", "agent", "bg", tool_use_id="tu-pw3")
    task_registry.mark_background_launch("pw3", "tu-pw3")
    frame = {"task_id": "agent-3", "status": "completed", "tool_use_id": "tu-pw3"}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        signal = await turn_scheduler.on_task_terminal("pw3", frame)
        await _drain_kicks()

    assert signal == "cli-owned-wake"
    assert _by_source("pw3", "unpark") == []


async def test_own_shell_completion_still_enqueues_unpark(mock_db):
    # A-1 conservatism: a backgrounded SHELL (kind "shell") completing while parked
    # is NOT a CLI-owned agent at-rest wake — the CLI does not own a shell's resume,
    # so the app MUST still deliver its completion.
    parked = turns_db.enqueue_turn("sh1", "user", prompt="run bg shell")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["shell-1"])
    task_registry.start_task("sh1", "shell-1", "shell", "ls", tool_use_id="tu-sh1")
    task_registry.mark_background_launch("sh1", "tu-sh1")  # own background work
    frame = {"task_id": "shell-1", "status": "completed", "tool_use_id": "tu-sh1",
             "summary": "exit 0", "output_file": "", "usage": {}}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sh1", frame)
        await _drain_kicks()

    (wake,) = _by_source("sh1", "unpark")
    assert "<task-id>shell-1</task-id>" in wake["prompt"]


async def test_app_monitor_shell_completion_enqueues_unpark(mock_db):
    # Regression (live E2E 2026-08-24): an APP-spawned Monitor shell — the MCP
    # Monitor tool or the fluid monitor_spawn spawner — registers with NO
    # tool_use_id and is never mark_sdk_background_task'ed, so the ownership
    # gate dropped its terminal frame as not-own work (#690 suppression) and a
    # parked parent stranded forever. The monitor-shell lane admits it.
    parked = turns_db.enqueue_turn("ms1", "user", prompt="monitor a build")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["shell-ms"])
    task_registry.start_task("ms1", "shell-ms", "shell", "make build")
    task_registry.mark_monitor_shell_task("ms1", "shell-ms")
    frame = {"task_id": "shell-ms", "status": "completed",
             "summary": "build ok", "output_file": "", "usage": {}}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ms1", frame)
        await _drain_kicks()

    (wake,) = _by_source("ms1", "unpark")
    assert "<task-id>shell-ms</task-id>" in wake["prompt"]
    assert "<summary>build ok</summary>" in wake["prompt"]


async def test_monitor_shell_is_not_pool_pinning_background_work(mock_db):
    # The new lane feeds ONLY ownership (wake + kill-surface admission) — it
    # must NOT put the shell into get_active_background_work: app Monitor shells
    # are independent of the pooled SDK client and deliberately do not pin.
    task_registry.start_task("mp1", "shell-mp", "shell", "tail -f x")
    task_registry.mark_monitor_shell_task("mp1", "shell-mp")
    assert task_registry.is_own_background_task("mp1", "shell-mp")
    assert task_registry.get_active_background_work("mp1") == []
    assert not task_registry.has_active_background_work("mp1")


async def test_completed_agent_not_parked_still_enqueues_unpark(mock_db):
    # A-1 fires ONLY when the parent is parked (the CLI's rich follow-up exists
    # only in the parked state). A completed own agent with NO parked turn keeps
    # the app wake — otherwise the completion is lost.
    live = turns_db.enqueue_turn("np1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")  # live, NOT parked
    task_registry.start_task("np1", "agent-1", "agent", "bg", tool_use_id="tu-np1")
    task_registry.mark_background_launch("np1", "tu-np1")
    frame = _terminal_frame("agent-1", "done")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("np1", frame)
        await _drain_kicks()

    (wake,) = _by_source("np1", "unpark")
    assert "<task-id>agent-1</task-id>" in wake["prompt"]


async def test_late_frame_after_cli_owned_suppression_does_not_wake(mock_db):
    # F1: a completed top-level agent frame while PARKED is CLI-owned → the app
    # suppresses its enqueue AND latches the woken mark (the CLI emits a rest-
    # notification each rest; latching lets the A-3 guard drop later duplicate
    # frames instead of enqueueing an empty wake). A SECOND frame for the same
    # task, now with NO parked turn (A-1 no longer fires), must find the latch set
    # and NOT enqueue a fresh wake.
    parked = turns_db.enqueue_turn("clf1", "user", prompt="kick off bg")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["agent-1"])
    task_registry.start_task("clf1", "agent-1", "agent", "bg", tool_use_id="tu-clf1")
    task_registry.mark_background_launch("clf1", "tu-clf1")
    frame = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-clf1"}

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("clf1", frame)
        await _drain_kicks()
        assert _by_source("clf1", "unpark") == []  # CLI owns the wake; app suppressed
        assert task_registry.is_woken("clf1", "agent-1")  # F1: latched on suppression

        # The parent unparks (the CLI's follow-up ran); the SECOND late frame for
        # the same task no longer sees a parked turn, so A-1 stays dormant — the
        # F1 latch is what blocks the (c) fresh enqueue.
        turns_db.settle_turn(parked["id"], "completed")
        await turn_scheduler.on_task_terminal("clf1", frame)
        await _drain_kicks()

    assert _by_source("clf1", "unpark") == []
    execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# (4) UNPARK — A-2 frame upgrading (bare TaskUpdated then rich TaskNotification)
# ---------------------------------------------------------------------------

async def test_bare_then_rich_frame_upgrades_prompt(mock_db):
    # NOT parked (A-1 dormant) so the app owns the wake. A bare TaskUpdated frame
    # enqueues the wake first; the rich TaskNotification frame for the SAME task
    # then upgrades the stored frame + prompt in place (no second row, no dup task).
    live = turns_db.enqueue_turn("bt1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    task_registry.start_task("bt1", "agent-1", "agent", "bg", tool_use_id="tu-bt1")
    task_registry.mark_background_launch("bt1", "tu-bt1")
    bare = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-bt1"}
    rich = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-bt1",
            "summary": "crawl done", "output_file": "",
            "usage": {"total_tokens": 99, "tool_uses": 3, "duration_ms": 2000}}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("bt1", bare)
        await _drain_kicks()
        (before,) = _by_source("bt1", "unpark")
        assert "<summary></summary>" in before["prompt"]              # bare: empty
        # R16: an all-blank <usage> block is omitted, not emitted empty.
        assert "<usage>" not in before["prompt"]

        await turn_scheduler.on_task_terminal("bt1", rich)
        await _drain_kicks()

    (after,) = _by_source("bt1", "unpark")
    assert "<summary>crawl done</summary>" in after["prompt"]         # upgraded
    assert "<subagent_tokens>99</subagent_tokens>" in after["prompt"]
    assert after["payload"]["pending_frames"] == [rich]              # replaced in place
    assert after["payload"]["task_ids"] == ["agent-1"]              # not duplicated


# ---------------------------------------------------------------------------
# (4) UNPARK — A-3 late-frame latch (mark_woken)
# ---------------------------------------------------------------------------

async def test_late_rich_frame_after_wake_ran_does_not_reenqueue(mock_db):
    # The bare frame enqueues + latches the wake; the wake then STARTS (leaves the
    # queue). A late frame for the same task finds the latch set → it must NOT
    # enqueue a fresh duplicate wake.
    task_registry.start_task("lf1", "agent-1", "agent", "bg", tool_use_id="tu-lf1")
    task_registry.mark_background_launch("lf1", "tu-lf1")
    bare = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-lf1"}
    rich = {"task_id": "agent-1", "status": "completed", "tool_use_id": "tu-lf1",
            "summary": "late", "output_file": "", "usage": {}}

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("lf1", bare)
        await _drain_kicks()
        (wake,) = _by_source("lf1", "unpark")
        # The wake starts — it leaves the queue.
        turns_db.start_turn(wake["id"], None, "runner")

        await turn_scheduler.on_task_terminal("lf1", rich)  # late frame
        await _drain_kicks()

    assert _by_source("lf1", "unpark") == []  # no fresh duplicate wake


async def test_resumed_agent_can_wake_again(mock_db):
    # A-3 clear-on-revival: once a task has woken, the latch blocks re-wakes — until
    # a genuine RESUME (completed→running revival) clears it, after which the
    # re-run agent may drive a fresh wake.
    task_registry.start_task("ra1", "agent-1", "agent", "bg", tool_use_id="tu-ra1")
    task_registry.mark_background_launch("ra1", "tu-ra1")
    frame = _terminal_frame("agent-1", "first rest")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ra1", frame)
        await _drain_kicks()
        (wake1,) = _by_source("ra1", "unpark")
        turns_db.start_turn(wake1["id"], None, "runner")  # wake1 leaves the queue

        # Latched: a repeat rest produces no new wake.
        await turn_scheduler.on_task_terminal("ra1", frame)
        await _drain_kicks()
        assert _by_source("ra1", "unpark") == []

        # Genuine resume: the completed→running revival clears the wake latch.
        task_registry.complete_task("ra1", "agent-1", "completed")
        task_registry.start_task("ra1", "agent-1", "agent", "bg", tool_use_id="tu-ra1")

        await turn_scheduler.on_task_terminal("ra1", frame)  # rests again
        await _drain_kicks()

    (wake2,) = _by_source("ra1", "unpark")
    assert wake2["id"] != wake1["id"]  # a fresh wake after the resume


# ---------------------------------------------------------------------------
# (4b) UNPARK — context inheritance across the settlement window
# ---------------------------------------------------------------------------

def test_context_turn_prefers_parked_over_live_and_settled(mock_db):
    _settled_turn("ctxa", prompt="settled", profile_id="p-settled")

    parked = turns_db.enqueue_turn(
        "ctxa", "user", prompt="parked", profile_id="p-parked",
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["task-1"])

    live = turns_db.enqueue_turn(
        "ctxa", "user", prompt="live", profile_id="p-live",
    )
    turns_db.start_turn(live["id"], None, "runner")

    context = turn_scheduler._latest_context_turn("ctxa")
    assert context["id"] == parked["id"]
    assert context["profile_id"] == "p-parked"


def test_context_turn_prefers_live_over_settled(mock_db):
    _settled_turn("ctxb", prompt="settled", profile_id="p-settled")

    live = turns_db.enqueue_turn(
        "ctxb", "user", prompt="live", profile_id="p-live",
    )
    turns_db.start_turn(live["id"], None, "runner")

    context = turn_scheduler._latest_context_turn("ctxb")
    assert context["id"] == live["id"]
    assert context["profile_id"] == "p-live"


async def test_wake_during_settlement_inherits_live_turn_context(mock_db):
    # Regression: a background task goes terminal while its parent turn is
    # still settling, and it is the session's FIRST turn — so there is neither a
    # parked nor a settled turn to inherit from. The wake used to enqueue with
    # profile_id=None, be refused downstream ("Profile not found: None") and
    # lose the notification for good (the mark_woken latch blocks a re-enqueue).
    live = turns_db.enqueue_turn(
        "race1", "user", prompt="working",
        profile_id="p-live", project_id="proj-live", worktree_id="wt-live",
        payload={"api_user_id": "u-live"},
    )
    turns_db.start_turn(live["id"], None, "runner")

    task_registry.start_task("race1", "task-r", "agent", "bg", tool_use_id="tu-r")
    task_registry.mark_background_launch("race1", "tu-r")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("race1", _terminal_frame("task-r", "done"))
        await _drain_kicks()

    (wake,) = _by_source("race1", "unpark")
    assert wake["profile_id"] == "p-live"
    assert wake["project_id"] == "proj-live"
    assert wake["worktree_id"] == "wt-live"
    assert wake["parent_turn_id"] == live["id"]
    assert wake["payload"]["api_user_id"] == "u-live"


async def test_wake_without_context_falls_back_to_session_profile(mock_db, caplog):
    # Belt and braces: no usable turn at all, but the session row carries a
    # profile — the wake inherits it (and says so in the log) rather than
    # enqueueing profile-less.
    mock_db.execute(
        "INSERT INTO agents (id, name, config) VALUES (?, ?, ?)",
        ("p-sess", "Test", "{}"),
    )
    mock_db.execute(
        "INSERT INTO sessions (id, profile_id) VALUES (?, ?)", ("nc1", "p-sess"),
    )
    mock_db.commit()

    frame = _terminal_frame("task-n", "done")
    with caplog.at_level(logging.WARNING, logger="app.core.turn_scheduler"):
        wake = await turn_scheduler._enqueue_context_wake(
            "nc1", "unpark", [frame], ["task-n"], "<task-notification/>",
        )

    assert wake["profile_id"] == "p-sess"
    assert "nc1" in caplog.text
    assert "no profile" in caplog.text


async def test_wake_inherits_context_turn_overrides(mock_db):
    # Regression (2026-08-31, session c66b213a): a wake built with empty
    # overrides for a session whose model/provider binding lived only in its
    # turns' overrides ran on the profile/global default leg with the wrong
    # credentials and 401'd in seconds. The wake continues the parked turn's
    # work, so it runs under that turn's own per-turn settings.
    parked = turns_db.enqueue_turn(
        "ov1", "user", prompt="work", profile_id="p1",
        overrides={"model": "prov:prov-1:z-ai/glm-5.3-flash", "effort": "high"},
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["task-o"])

    frame = _terminal_frame("task-o", "done")
    wake = await turn_scheduler._enqueue_context_wake(
        "ov1", "unpark", [frame], ["task-o"], "<task-notification/>",
    )

    assert wake["overrides"] == {
        "model": "prov:prov-1:z-ai/glm-5.3-flash", "effort": "high",
    }


async def test_wake_falls_back_to_session_api_user(mock_db):
    # An API-owned session whose context turn carries no api_user_id payload
    # must not wake as an admin turn — the wake inherits the session row's
    # api_user_id so is_admin gates and credential scoping stay per-user.
    mock_db.execute(
        "INSERT INTO agents (id, name, config) VALUES (?, ?, ?)",
        ("p1", "Test", "{}"),
    )
    mock_db.execute(
        "INSERT INTO api_users (id, name, api_key_hash) VALUES (?, ?, ?)",
        ("u-api", "Test API user", "hash"),
    )
    mock_db.execute(
        "INSERT INTO sessions (id, profile_id, api_user_id) VALUES (?, ?, ?)",
        ("au1", "p1", "u-api"),
    )
    mock_db.commit()
    parked = turns_db.enqueue_turn(
        "au1", "api_background", prompt="work", profile_id="p1",
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["task-a"])

    frame = _terminal_frame("task-a", "done")
    wake = await turn_scheduler._enqueue_context_wake(
        "au1", "unpark", [frame], ["task-a"], "<task-notification/>",
    )

    assert wake["payload"]["api_user_id"] == "u-api"
    # A context turn that DOES carry the attribution still wins over the row.
    parked2 = turns_db.enqueue_turn(
        "au2", "user", prompt="work", profile_id="p1",
        payload={"api_user_id": "u-turn"},
    )
    turns_db.start_turn(parked2["id"], None, "runner")
    turns_db.park_turn(parked2["id"], ["task-b"])
    wake2 = await turn_scheduler._enqueue_context_wake(
        "au2", "unpark", [_terminal_frame("task-b", "done")], ["task-b"], "<x/>",
    )
    assert wake2["payload"]["api_user_id"] == "u-turn"


async def test_api_lane_adhoc_parent_context_reaches_the_wake(mock_db):
    # The exact live failure shape (session c66b213a, 2026-08-31): a
    # conversation/start session's parent turn is an AD-HOC terminal row
    # (record_adhoc_turn — the API lane never goes through the scheduler).
    # Its wake must run with the same model/provider overrides and api_user
    # attribution the parent ran with, not the profile/global defaults.
    parent = turns_db.record_adhoc_turn(
        "api1", "api_background",
        prompt="start a monitor", profile_id="p1",
        overrides={"model": "prov:prov-1:z-ai/glm-5.3-flash"},
        payload={"api_user_id": "u-api"},
    )

    frame = _terminal_frame("task-m", "ci green")
    wake = await turn_scheduler._enqueue_context_wake(
        "api1", "unpark", [frame], ["task-m"], "<task-notification/>",
    )

    assert wake["parent_turn_id"] == parent["id"]
    assert wake["profile_id"] == "p1"
    assert wake["overrides"] == {"model": "prov:prov-1:z-ai/glm-5.3-flash"}
    assert wake["payload"]["api_user_id"] == "u-api"


# ---------------------------------------------------------------------------
# Kick — shutdown guard
# ---------------------------------------------------------------------------

async def test_kick_shutdown_guard_leaves_turn_queued(mock_db):
    settled = _settled_turn("k1")
    queued = turns_db.enqueue_turn("k1", "queue", prompt="after restart")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch("app.core.startup.is_shutting_down", True))
        await turn_scheduler.on_turn_settled("k1", settled["id"], "completed")
        turn_scheduler.kick("k1")  # direct kick is suppressed too
        await _drain_kicks()

    execute.assert_not_awaited()
    assert turn_scheduler._kick_tasks == set()
    # The queued turn stays durable for the next boot's sweep/kick.
    assert [t["id"] for t in turns_db.get_queued_turns("k1")] == [queued["id"]]


async def test_unpark_enqueue_survives_shutdown_kick_suppression(mock_db):
    # on_task_terminal still records the wake durably; only the START is
    # suppressed while shutting down.
    frame = {"task_id": "task-1", "status": "completed", "tool_use_id": "tu-1",
             "summary": "done", "output_file": "", "usage": {}}
    # task-1 is the parent's OWN background work (ownership gate admits it).
    task_registry.start_task("k2", "task-1", "agent", "bg", tool_use_id="tu-1")
    task_registry.mark_background_launch("k2", "tu-1")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch("app.core.startup.is_shutting_down", True))
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("k2", frame)
        await _drain_kicks()

    execute.assert_not_awaited()
    assert len(_by_source("k2", "unpark")) == 1


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

async def test_install_registers_all_three_seams(mock_db):
    turn_scheduler.install()
    assert session_runtime._on_turn_settled is turn_scheduler.on_turn_settled
    assert idle_reader._on_task_terminal is turn_scheduler.on_task_terminal
    assert turn_manager._kick_hook is turn_scheduler.kick


async def test_settlement_hook_never_raises(mock_db):
    # A broken derivation must never break turn teardown.
    with patch(
        "app.core.turn_scheduler._turn_terminal_payload",
        side_effect=RuntimeError("boom"),
    ):
        settled = _settled_turn("e1")
        await turn_scheduler.on_turn_settled("e1", settled["id"], "completed")


# Task-notification payloads must contain one copy of a result. A summary-only ProviderAgent frame must not
# repeat the same unbounded report in both summary and result.


def _prompt_for(**frame):
    frame.setdefault("task_id", "task-x")
    frame.setdefault("tool_use_id", "tu-x")
    frame.setdefault("status", "completed")
    frame.setdefault("output_file", "")
    frame.setdefault("usage", {})
    with patch("app.core.query_engine._read_task_output_file", return_value=""):
        return turn_scheduler._build_task_notification_prompt([frame])


def test_a_summary_sourced_result_is_emitted_once():
    """Summary-only frame: the payload appears in ONE element, not two."""
    report = "the worker's 26KB report " * 500
    prompt = _prompt_for(summary=report)

    assert prompt.count(report) == 1
    assert f"<summary>{report}</summary>" in prompt
    assert "<result>" not in prompt


def test_a_distinct_result_keeps_both_but_caps_the_summary():
    """When the transcript yields a real result the summary is back to being a
    label — and a label gets a few hundred characters, not the whole payload."""
    label = "L" * 5_000
    result = "the sub-agent's actual final answer"
    with patch(
        "app.core.turn_scheduler._extract_final_assistant_text",
        return_value=result,
    ):
        prompt = _prompt_for(summary=label)

    assert f"<result>{result}</result>" in prompt
    assert f"<summary>{label}</summary>" not in prompt
    summary_body = prompt.split("<summary>")[1].split("</summary>")[0]
    assert len(summary_body) < turn_scheduler._MAX_TASK_SUMMARY_CHARS + 100
    assert summary_body.startswith("L" * turn_scheduler._MAX_TASK_SUMMARY_CHARS)
    assert "see <result>" in summary_body


def test_a_summary_sole_payload_is_still_bounded():
    """Sole carrier or not, nothing unbounded reaches the parent's context."""
    oversized = turn_scheduler._MAX_TASK_RESULT_CHARS * 2
    prompt = _prompt_for(summary="x" * oversized)

    summary_body = prompt.split("<summary>")[1].split("</summary>")[0]
    assert len(summary_body) < oversized
    assert "truncated" in summary_body


def test_one_task_id_yields_one_block_per_prompt():
    """A task can produce several terminal frames in one wake batch — a bare
    TaskUpdated and the rich TaskNotification behind it. They are ONE task."""
    bare = {"task_id": "task-d", "tool_use_id": "tu-d", "status": "completed",
            "summary": "", "output_file": "", "usage": {}}
    rich = {"task_id": "task-d", "tool_use_id": "tu-d", "status": "completed",
            "summary": "the real report", "output_file": "", "usage": {}}
    other = {"task_id": "task-e", "tool_use_id": "tu-e", "status": "completed",
             "summary": "unrelated", "output_file": "", "usage": {}}

    with patch("app.core.query_engine._read_task_output_file", return_value=""):
        prompt = turn_scheduler._build_task_notification_prompt([bare, rich, other])

    assert prompt.count("<task-id>task-d</task-id>") == 1
    assert prompt.count("<task-id>task-e</task-id>") == 1
    # The richer frame wins, in the first occurrence's position.
    assert "<summary>the real report</summary>" in prompt
    assert prompt.index("task-d") < prompt.index("task-e")


# ---------------------------------------------------------------------------
# R16 — template cosmetics: no blank tags, and a monitor is not a subagent
# ---------------------------------------------------------------------------


def test_blank_identity_tags_are_omitted():
    """A shell monitor has no tool-use id and no output file. Emitting them
    blank read like fields the notification had failed to populate."""
    prompt = _prompt_for(task_id="mon-1", tool_use_id="", output_file="",
                         summary="done", kind="shell")

    assert "<tool-use-id>" not in prompt
    assert "<output-file>" not in prompt
    assert "<task-id>mon-1</task-id>" in prompt
    assert "<status>completed</status>" in prompt


def test_populated_identity_tags_are_kept():
    prompt = _prompt_for(task_id="agent-1", tool_use_id="tu-1",
                         output_file="/tmp/agent-1.jsonl", summary="done")

    assert "<tool-use-id>tu-1</tool-use-id>" in prompt
    assert "<output-file>/tmp/agent-1.jsonl</output-file>" in prompt


def test_a_shell_monitor_is_labelled_a_monitor_not_a_subagent():
    prompt = _prompt_for(task_id="mon-2", tool_use_id="", summary="exit 0",
                         kind="shell", usage={"total_tokens": 10})

    assert "<kind>monitor</kind>" in prompt
    assert "subagent" not in prompt
    assert "<usage>" not in prompt


def test_an_agent_frame_keeps_its_usage_and_gets_no_kind_element():
    prompt = _prompt_for(task_id="agent-2", summary="done",
                         usage={"total_tokens": 99, "tool_uses": 3,
                                "duration_ms": 2000})

    assert "<kind>" not in prompt
    assert "<subagent_tokens>99</subagent_tokens>" in prompt
    assert "<tool_uses>3</tool_uses>" in prompt
    assert "<duration_ms>2000</duration_ms>" in prompt


def test_an_all_blank_usage_block_is_omitted_and_partial_usage_is_kept():
    assert "<usage>" not in _prompt_for(summary="done", usage={})

    partial = _prompt_for(summary="done", usage={"total_tokens": 5})
    assert "<subagent_tokens>5</subagent_tokens>" in partial
    assert "<tool_uses>" not in partial
    assert "<duration_ms>" not in partial


def test_frames_without_a_task_id_are_all_kept():
    """Nothing identifies them, so nothing may collapse them."""
    anon = {"task_id": "", "tool_use_id": "tu-1", "status": "completed",
            "summary": "one", "output_file": "", "usage": {}}
    other = dict(anon, tool_use_id="tu-2", summary="two")

    with patch("app.core.query_engine._read_task_output_file", return_value=""):
        prompt = turn_scheduler._build_task_notification_prompt([anon, other])

    assert prompt.count("<task-notification>") == 2


async def test_a_repeat_frame_never_doubles_the_stored_wake(mock_db):
    """Payload-level dedupe: the wake turn's pending_frames is what a later
    merge rebuilds the prompt from, so a duplicate stored there outlives the
    call that made it."""
    frame = _terminal_frame("task-r", "done")
    wake = await turn_scheduler._enqueue_context_wake(
        "dr1", "unpark", [frame, dict(frame)], ["task-r", "task-r"], "<x/>",
    )

    assert wake["payload"]["pending_frames"] == [frame]
    assert wake["payload"]["task_ids"] == ["task-r"]


# Deliver background terminal notifications into a running parent turn through the same steer path as an owner
# message, rather than waiting for the parent to finish and encouraging duplicate work.

class _FakeLiveRuntime:
    """Stand-in for the SessionRuntime holding this session's running turn.

    Only the one method ``turn_scheduler`` reaches for. ``accepts`` chooses
    between an engine that took the steer and one with nothing live to steer
    into (no adapter, a turn already being killed)."""

    def __init__(self, *, accepts=True):
        self.accepts = accepts
        self.steers = []

    async def steer_task_notification(
        self, prompt, *, notification_id, frames, task_ids
    ):
        self.steers.append({
            "prompt": prompt,
            "id": notification_id,
            "frames": frames,
            "task_ids": task_ids,
        })
        return self.accepts


@contextmanager
def _live_runtime(session_id, *, accepts=True):
    runtime = _FakeLiveRuntime(accepts=accepts)
    session_runtime._live_runtimes[session_id] = runtime
    try:
        yield runtime
    finally:
        session_runtime._live_runtimes.pop(session_id, None)


def _own_agent_task(session_id, task_id):
    """Register ``task_id`` as the parent's OWN background work."""
    task_registry.start_task(
        session_id, task_id, "agent", "bg", tool_use_id=f"tu-{task_id}",
    )
    task_registry.mark_background_launch(session_id, f"tu-{task_id}")


async def test_midturn_completion_is_steered_instead_of_queued(mock_db):
    """(a) The parent is INSIDE a turn: the notification is steered into it and
    NO wake is queued behind the still-running turn."""
    live = turns_db.enqueue_turn("mt1", "user", prompt="a very long turn")
    turns_db.start_turn(live["id"], None, "runner")
    _own_agent_task("mt1", "task-mt1")
    frame = _terminal_frame("task-mt1", "the child's report")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        runtime = stack.enter_context(_live_runtime("mt1"))
        await turn_scheduler.on_task_terminal("mt1", frame)
        await _drain_kicks()

    assert _by_source("mt1", "unpark") == []
    assert len(runtime.steers) == 1
    steer = runtime.steers[0]
    assert "<task-notification>" in steer["prompt"]
    assert "<task-id>task-mt1</task-id>" in steer["prompt"]
    assert "the child's report" in steer["prompt"]
    assert steer["task_ids"] == ["task-mt1"]
    assert steer["frames"] == [frame]
    # The receipt handler tells a notification id from a queued turn's row id by
    # this prefix — they share one namespace on the wire.
    assert steer["id"].startswith(turn_scheduler.TASK_STEER_ID_PREFIX)
    execute.assert_not_awaited()


async def test_steered_completion_is_not_redelivered_when_the_turn_parks(mock_db):
    """(a, exactly-once) The turn parks after the steer and the frame arrives
    again from the idle reader: the ``mark_woken`` latch the steer spent keeps
    the at-rest path from delivering the same completion twice."""
    live = turns_db.enqueue_turn("mt2", "user", prompt="a very long turn")
    turns_db.start_turn(live["id"], None, "runner")
    _own_agent_task("mt2", "task-mt2")
    frame = _terminal_frame("task-mt2", "done once")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        runtime = stack.enter_context(_live_runtime("mt2"))
        await turn_scheduler.on_task_terminal("mt2", frame)
        await _drain_kicks()

    assert len(runtime.steers) == 1
    # The turn parks, the session goes to rest (no live runtime), and the idle
    # reader replays the same terminal frame.
    turns_db.park_turn(live["id"], ["task-mt2"])
    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("mt2", dict(frame))
        await _drain_kicks()

    assert _by_source("mt2", "unpark") == []


async def test_parked_parent_still_gets_the_at_rest_wake(mock_db):
    """(b) Nothing changes at rest: a parked parent's completion is enqueued as
    the ordinary wake and no steer is even attempted (no live runtime)."""
    parked = turns_db.enqueue_turn("mt3", "user", prompt="run bg shell")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["shell-mt3"])
    task_registry.start_task("mt3", "shell-mt3", "shell", "make", tool_use_id="tu-mt3")
    task_registry.mark_background_launch("mt3", "tu-mt3")
    frame = _terminal_frame("shell-mt3", "build ok")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("mt3", frame)
        await _drain_kicks()

    (wake,) = _by_source("mt3", "unpark")
    assert "<task-id>shell-mt3</task-id>" in wake["prompt"]
    assert wake["payload"]["task_ids"] == ["shell-mt3"]
    execute.assert_awaited_once()


async def test_unsteerable_turn_falls_back_to_the_queued_wake(mock_db):
    """(d) A live turn that cannot take the steer (its engine is gone, or the
    user is already killing it) falls straight through to the wake path — the
    delivery is late, never lost."""
    live = turns_db.enqueue_turn("mt4", "user", prompt="dying turn")
    turns_db.start_turn(live["id"], None, "runner")
    _own_agent_task("mt4", "task-mt4")
    frame = _terminal_frame("task-mt4", "report anyway")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        runtime = stack.enter_context(_live_runtime("mt4", accepts=False))
        await turn_scheduler.on_task_terminal("mt4", frame)
        await _drain_kicks()

    assert len(runtime.steers) == 1  # attempted...
    (wake,) = _by_source("mt4", "unpark")  # ...refused, so the wake stands
    assert "<task-id>task-mt4</task-id>" in wake["prompt"]
    assert wake["payload"]["pending_frames"] == [frame]


async def test_a_steer_the_turn_never_delivered_re_enqueues_the_wake(mock_db):
    """(d) The submit was accepted but the turn ended before fluid delivered it.
    A user's steer survives that on its still-queued row; a notification has no
    row, so ``_finalize`` hands it back here and the ordinary wake is enqueued."""
    context = turns_db.enqueue_turn(
        "mt5", "user", prompt="kick off bg work",
        profile_id="p5", project_id="proj-5",
    )
    turns_db.start_turn(context["id"], None, "runner")
    frame = _terminal_frame("task-mt5", "stranded report")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_notification_stranded(
            "mt5", [frame], ["task-mt5"],
        )
        await _drain_kicks()

    (wake,) = _by_source("mt5", "unpark")
    assert "<task-id>task-mt5</task-id>" in wake["prompt"]
    assert wake["payload"]["task_ids"] == ["task-mt5"]
    assert wake["profile_id"] == "p5"
    execute.assert_awaited_once()


async def test_a_cli_owned_wake_is_never_steered(mock_db):
    """A-1 keeps precedence: a top-level backgrounded agent completing while the
    parent is parked is the CLI's wake to deliver, so the app neither steers nor
    enqueues. (A parked parent has no live runtime in practice — this pins the
    order of the two decisions.)"""
    parked = turns_db.enqueue_turn("mt6", "user", prompt="kick off bg")
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["agent-mt6"])
    _own_agent_task("mt6", "agent-mt6")
    frame = {
        "task_id": "agent-mt6", "status": "completed",
        "tool_use_id": "tu-agent-mt6",
    }

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        runtime = stack.enter_context(_live_runtime("mt6"))
        signal = await turn_scheduler.on_task_terminal("mt6", frame)
        await _drain_kicks()

    assert signal == "cli-owned-wake"
    assert runtime.steers == []
    assert _by_source("mt6", "unpark") == []


async def test_delivery_latches_terminal_notified(mock_db):
    """(c) ``terminal_notified`` is latched when a notification is actually
    delivered. It used to stay False forever for a clean completion: the only
    caller of the latch is ``complete_task``, and only on error/killed."""
    _own_agent_task("mt7", "task-mt7")
    task_registry.complete_task("mt7", "task-mt7", "completed", summary="done")

    turn_scheduler.on_task_notification_delivered("mt7", ["task-mt7"])

    # The one-shot latch is spent: a second delivery cannot claim it again.
    assert task_registry.mark_terminal_notified("mt7", "task-mt7") is False
