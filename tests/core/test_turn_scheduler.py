"""Unit tests for app/core/turn_scheduler.py — the settlement policies
(v2 core, Phase 2 A5).

Plan §5 coverage: policy ORDER (failover before queue drain before
auto-continue), F4 (a stop/abort settlement leaves queued rows untouched),
failover cap + no message re-append, the durable #557 auto-continue
derivation, unpark prompt synthesis, and the shutdown kick guard.

The turns/session_events tables are exercised for real (pg_shim SQLite).
Collaborators are patched at the CONSUMER module bindings the scheduler
resolves lazily at call time (``session_runtime.execute``,
``account_failover.attempt_failover``, ``compact_continue`` predicates,
``query_engine._read_task_output_file``) — the lazy-import-mock-freeze rule.
"""

import asyncio
from contextlib import ExitStack
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
    turn_scheduler._clear_pending_pump(None)
    yield
    session_runtime.register_scheduler(None)
    idle_reader.register_on_task_terminal(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    # Cancel any deferred-pump timers a test left armed (install() — which wires
    # the registry cleanup — is not called in these unit tests).
    turn_scheduler._clear_pending_pump(None)


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


async def test_f4_abort_settlement_leaves_queue_untouched(mock_db):
    # A user stop settles aborted → queued rows REMAIN and nothing auto-starts.
    settled = _settled_turn("s2", outcome="aborted", terminal_event=False)
    queued = turns_db.enqueue_turn("s2", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s2", settled["id"], "aborted")
        await _drain_kicks()

    execute.assert_not_awaited()
    assert [t["id"] for t in turns_db.get_queued_turns("s2")] == [queued["id"]]


async def test_parked_settlement_is_a_no_op(mock_db):
    turn = turns_db.enqueue_turn("s3", "user", prompt="bg work")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["task-1"])
    turns_db.enqueue_turn("s3", "queue", prompt="second")

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s3", turn["id"], "parked")
        await _drain_kicks()

    execute.assert_not_awaited()


async def test_completed_empty_queue_natural_end_does_nothing(mock_db):
    settled = _settled_turn("s4")  # end_turn, nothing queued

    with _patch_execute() as execute:
        await turn_scheduler.on_turn_settled("s4", settled["id"], "completed")
        await _drain_kicks()

    execute.assert_not_awaited()
    assert turns_db.get_queued_turns("s4") == []


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
    assert "<result>crawl finished</result>" in wake["prompt"]  # summary fallback
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


async def test_subagent_owned_terminal_with_parked_agent_fires_pump(mock_db):
    # 2026-07-10 regression: while a background sub-agent is still running, its
    # grandchild's terminal notification MUST wake the parent — the main-session
    # pump is the ONLY resume path for the parked sub-agent. Under the quiet-pump
    # split this is a grandchild STEP (not the parent's own work), so it wakes on
    # the PUMP lane (source="unpark_pump"), not the rich unpark lane.
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
    (wake,) = _by_source("sg2", "unpark_pump")
    assert "<task-notification>" in wake["prompt"]
    assert "<kind>pump</kind>" in wake["prompt"]
    assert "<task-id>gc-2</task-id>" in wake["prompt"]
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == wake["id"]


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


async def test_unknown_task_with_active_agent_fires_pump(mock_db):
    # Under the quiet-pump split the ownership lane no longer drives the wake: an
    # UNKNOWN terminal task (no lane armed) still wakes the parent as long as a
    # background sub-agent is running — the arming-miss strand guard, since a
    # grandchild is by construction launched while its sub-agent runs. Not-own +
    # active agent = the PUMP lane (source="unpark_pump").
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
    assert len(_by_source("sg5a", "unpark_pump")) == 1

    # (b) same unknown task, NO running agent → suppressed.
    frame_b = _terminal_frame("gc-6", "grandchild done")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("sg5b", frame_b)
        await _drain_kicks()

    assert _by_source("sg5b", "unpark") == []


# ---------------------------------------------------------------------------
# (4) PUMP — quiet subagent-step wakes (source="unpark_pump")
# ---------------------------------------------------------------------------
# A grandchild task (NOT background-launched by the parent → is_own false) going
# terminal while a background sub-agent is STILL running is an internal step of
# that sub-agent. It gets ONE quiet source="unpark_pump" wake per completion —
# never merged/batched — with the pump prompt contract (nested <kind>pump</kind>,
# pump note, NO <usage>).


async def test_pump_wake_enqueues_unpark_pump(mock_db):
    # active background sub-agent (kind=="agent") → has_active_background_agent.
    task_registry.start_task("pw1", "subagent-1", "agent", "spawning sub-agent")
    parked = turns_db.enqueue_turn(
        "pw1", "user", prompt="kick off",
        profile_id="pp", project_id="proj-p", worktree_id="wt-p",
        payload={"api_user_id": "u-api"},
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["subagent-1"])

    frame = {
        "task_id": "gc-1", "status": "completed", "tool_use_id": "tu-gc-1",
        "summary": "step done", "output_file": "",
        "usage": {"total_tokens": 50, "tool_uses": 2, "duration_ms": 1000},
    }

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("pw1", frame)
        await _drain_kicks()

    assert _by_source("pw1", "unpark") == []  # NOT the rich lane
    (wake,) = _by_source("pw1", "unpark_pump")
    assert "<task-notification>" in wake["prompt"]
    assert "<kind>pump</kind>" in wake["prompt"]
    assert "<task-id>gc-1</task-id>" in wake["prompt"]
    assert "<result>step done</result>" in wake["prompt"]  # summary fallback
    assert "<usage>" not in wake["prompt"]                 # usage omitted for pump
    assert "do NOT respond at all" in wake["prompt"]  # pump note (silent no-reply)
    # Context inherited from the parked turn.
    assert wake["parent_turn_id"] == parked["id"]
    assert wake["profile_id"] == "pp"
    assert wake["project_id"] == "proj-p"
    assert wake["worktree_id"] == "wt-p"
    assert wake["payload"]["api_user_id"] == "u-api"
    assert wake["payload"]["task_ids"] == ["gc-1"]
    assert wake["payload"]["pending_frames"] == [frame]
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == wake["id"]


async def test_pump_completions_never_merge(mock_db):
    # Two grandchild steps → TWO separate unpark_pump rows (never folded); each
    # payload.task_ids carries only its own task. A live turn keeps them queued.
    task_registry.start_task("pm1", "subagent-1", "agent", "spawning")
    live = turns_db.enqueue_turn("pm1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")

    f1 = _terminal_frame("gc-a", "alpha")
    f2 = _terminal_frame("gc-b", "beta")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("pm1", f1)
        await _drain_kicks()
        await turn_scheduler.on_task_terminal("pm1", f2)
        await _drain_kicks()

    assert _by_source("pm1", "unpark") == []
    pumps = _by_source("pm1", "unpark_pump")
    assert len(pumps) == 2
    # Each pump row carries ONLY its own frame — no folding across steps.
    by_task = {p["payload"]["task_ids"][0]: p for p in pumps}
    assert set(by_task) == {"gc-a", "gc-b"}
    assert by_task["gc-a"]["payload"]["pending_frames"] == [f1]
    assert by_task["gc-b"]["payload"]["pending_frames"] == [f2]


async def test_pump_duplicate_frame_noop(mock_db):
    # The SAME grandchild frame twice → dedup-skip: one row, second call kicks
    # nothing (matches the own-lane (a) idempotent no-op).
    task_registry.start_task("pd1", "subagent-1", "agent", "spawning")
    live = turns_db.enqueue_turn("pd1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    frame = _terminal_frame("gc-dup", "done once")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("pd1", frame)
        await _drain_kicks()
        (before,) = _by_source("pd1", "unpark_pump")
        prompt_before, payload_before = before["prompt"], before["payload"]
        execute.reset_mock()

        await turn_scheduler.on_task_terminal("pd1", frame)  # duplicate
        await _drain_kicks()

    pumps = _by_source("pd1", "unpark_pump")
    assert len(pumps) == 1
    assert pumps[0]["prompt"] == prompt_before
    assert pumps[0]["payload"] == payload_before
    execute.assert_not_awaited()  # dedup no-op does not kick


async def test_pump_suppressed_when_no_active_agent(mock_db):
    # Grandchild terminal, is_own false, NO running sub-agent → fully suppressed
    # (neither unpark nor unpark_pump).
    frame = _terminal_frame("gc-orphan", "orphan done")

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ps1", frame)
        await _drain_kicks()

    assert _by_source("ps1", "unpark") == []
    assert _by_source("ps1", "unpark_pump") == []
    execute.assert_not_awaited()


async def test_own_and_pump_coexist(mock_db):
    # The parent's OWN background work completes (→ unpark, merge lane intact)
    # while a grandchild step completes (→ separate unpark_pump). Neither folds
    # into the other. A live turn keeps both rows queued.
    live = turns_db.enqueue_turn("oc1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    # A running sub-agent so the grandchild admits the pump lane.
    task_registry.start_task("oc1", "subagent-1", "agent", "spawning")
    # Parent's OWN background work (is_own true → rich unpark lane).
    task_registry.start_task("oc1", "own-x", "agent", "bg", tool_use_id="tu-own-x")
    task_registry.mark_background_launch("oc1", "tu-own-x")
    own = _terminal_frame("own-x", "own done")
    gc = _terminal_frame("gc-x", "grandchild step")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("oc1", own)
        await _drain_kicks()
        await turn_scheduler.on_task_terminal("oc1", gc)
        await _drain_kicks()

    (unpark,) = _by_source("oc1", "unpark")
    assert unpark["payload"]["task_ids"] == ["own-x"]
    assert "<kind>pump</kind>" not in unpark["prompt"]
    (pump,) = _by_source("oc1", "unpark_pump")
    assert pump["payload"]["task_ids"] == ["gc-x"]
    assert "<kind>pump</kind>" in pump["prompt"]


# ---------------------------------------------------------------------------
# (4) PUMP — B-prime deferred (owner-quiescence) pump for child steps (bug 2)
# ---------------------------------------------------------------------------
# A CHILD step of a RESOLVABLE running owner is not pumped immediately: it is
# deferred against the owner's quiescence. A running owner keeps emitting inner
# activity on the drained stream (note_owner_activity); silence over the quiet
# window means the owner is at rest and the child step warrants a pump.


def _setup_owner_child(
    session, *, owner_task="owner-A", owner_tuid="tu-A",
    child_task="child-B", child_tuid="tu-B",
):
    """Owner = the parent's OWN background sub-agent (running); child = a
    grandchild it launched, linked owner⇐child. Returns the child terminal frame."""
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


async def test_child_step_while_owner_active_suppresses_pump(mock_db, monkeypatch):
    # Owner emits inner activity AFTER the child terminal → still running → the
    # deferred pump is dropped (decision=suppress-consumed). No wake at all.
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    frame = _setup_owner_child("cq1")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("cq1", frame)
        # Deferred — not fired yet.
        deferred = turn_scheduler._pending_pump["cq1"]["owner-A"]["task"]
        # Owner keeps working AFTER the child terminal.
        task_registry.note_owner_activity("cq1", "tu-A")
        await deferred
        await _drain_kicks()

    assert _by_source("cq1", "unpark_pump") == []
    assert _by_source("cq1", "unpark") == []


async def test_child_step_while_owner_silent_fires_one_pump(mock_db, monkeypatch):
    # Owner falls silent over the window → at rest → ONE pump over the child step,
    # inheriting the parked turn's context.
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    frame = _setup_owner_child("cs1")
    parked = turns_db.enqueue_turn(
        "cs1", "user", prompt="kick",
        profile_id="pp", project_id="proj-p", worktree_id="wt-p",
        payload={"api_user_id": "u-api"},
    )
    turns_db.start_turn(parked["id"], None, "runner")
    turns_db.park_turn(parked["id"], ["owner-A"])

    with ExitStack() as stack:
        execute = stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("cs1", frame)
        deferred = turn_scheduler._pending_pump["cs1"]["owner-A"]["task"]
        await deferred  # no owner activity → silent
        await _drain_kicks()

    assert _by_source("cs1", "unpark") == []
    (wake,) = _by_source("cs1", "unpark_pump")
    assert "<kind>pump</kind>" in wake["prompt"]
    assert "<task-id>child-B</task-id>" in wake["prompt"]
    assert wake["payload"]["task_ids"] == ["child-B"]
    assert wake["payload"]["pending_frames"] == [frame]
    assert wake["parent_turn_id"] == parked["id"]
    assert wake["profile_id"] == "pp"
    assert wake["project_id"] == "proj-p"
    assert wake["worktree_id"] == "wt-p"
    assert task_registry.is_woken("cs1", "child-B")
    execute.assert_awaited_once()
    assert execute.await_args.args[0]["id"] == wake["id"]


async def test_multiple_children_coalesce_to_one_pump(mock_db, monkeypatch):
    # Two child steps of the same owner, both deferred → coalesce into ONE pump
    # (the second stash debounce-re-arms the single per-owner timer).
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    fB = _setup_owner_child("mc1")
    task_registry.start_task("mc1", "child-C", "agent", "gc", tool_use_id="tu-C")
    task_registry.link_child_to_owner("mc1", "tu-C", "tu-A")
    fC = {
        "task_id": "child-C", "status": "completed", "tool_use_id": "tu-C",
        "summary": "step C", "output_file": "", "usage": {},
    }
    live = turns_db.enqueue_turn("mc1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("mc1", fB)
        await turn_scheduler.on_task_terminal("mc1", fC)
        deferred = turn_scheduler._pending_pump["mc1"]["owner-A"]["task"]
        await deferred
        await _drain_kicks()

    (pump,) = _by_source("mc1", "unpark_pump")
    assert set(pump["payload"]["task_ids"]) == {"child-B", "child-C"}
    assert "<task-id>child-B</task-id>" in pump["prompt"]
    assert "<task-id>child-C</task-id>" in pump["prompt"]


async def test_owner_terminal_supersedes_deferred_pump(mock_db, monkeypatch):
    # The owner's OWN terminal arrives while a child pump is deferred → cancel the
    # deferred pump (decision=superseded-by-own); the owner's rich unpark covers it.
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    child_frame = _setup_owner_child("ot1")
    live = turns_db.enqueue_turn("ot1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")
    owner_frame = {
        "task_id": "owner-A", "status": "killed", "tool_use_id": "tu-A",
        "summary": "owner stopped", "output_file": "", "usage": {},
    }

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ot1", child_frame)  # deferred
        assert "owner-A" in turn_scheduler._pending_pump["ot1"]
        await turn_scheduler.on_task_terminal("ot1", owner_frame)  # OWN lane cancels
        await _drain_kicks()
        # Wait past the window: a cancelled timer must NOT fire.
        await asyncio.sleep(0.1)
        await _drain_kicks()

    assert turn_scheduler._pending_pump.get("ot1") is None
    assert _by_source("ot1", "unpark_pump") == []
    # The owner's own killed terminal still fires its rich unpark.
    (unpark,) = _by_source("ot1", "unpark")
    assert unpark["payload"]["task_ids"] == ["owner-A"]


async def test_nested_owner_terminal_cancels_children_deferred_pump(mock_db, monkeypatch):
    # F4: a pump-lane terminal task may itself be a NESTED owner — NOT the parent's
    # OWN background work, but has_active_background_agent is true. Its terminal
    # supersedes its own children's stashed deferred pump, exactly like the OWN-lane
    # item-e cancel. Topology: grandparent G (running) ⇐ owner O ⇐ child C.
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    # None of G/O/C is the parent's OWN work (no mark_background_launch), so they
    # all ride the PUMP lane; G stays running to keep has_active_background_agent.
    task_registry.start_task("no1", "gp-G", "agent", "grandparent", tool_use_id="tu-G")
    task_registry.start_task("no1", "owner-O", "agent", "owner", tool_use_id="tu-O")
    task_registry.link_child_to_owner("no1", "tu-O", "tu-G")   # O ⇐ G
    task_registry.start_task("no1", "child-C", "agent", "child", tool_use_id="tu-C")
    task_registry.link_child_to_owner("no1", "tu-C", "tu-O")   # C ⇐ O
    live = turns_db.enqueue_turn("no1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")

    child_frame = {
        "task_id": "child-C", "status": "completed", "tool_use_id": "tu-C",
        "summary": "child step", "output_file": "", "usage": {},
    }
    owner_frame = {
        "task_id": "owner-O", "status": "completed", "tool_use_id": "tu-O",
        "summary": "owner step", "output_file": "", "usage": {},
    }

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        # Child step defers under owner O.
        await turn_scheduler.on_task_terminal("no1", child_frame)
        assert "owner-O" in turn_scheduler._pending_pump["no1"]
        # O's OWN terminal rides the PUMP lane (O not own; G still running) → F4
        # cancels O's stashed child, then O itself defers under grandparent G.
        await turn_scheduler.on_task_terminal("no1", owner_frame)
        assert "owner-O" not in turn_scheduler._pending_pump.get("no1", {})  # F4 dropped it
        # Keep G active so O's own deferred pump is suppress-consumed → no pump at all.
        task_registry.note_owner_activity("no1", "tu-G")
        await turn_scheduler._pending_pump["no1"]["gp-G"]["task"]
        await _drain_kicks()
        # Past any window: the cancelled child timer must never fire.
        await asyncio.sleep(0.1)
        await _drain_kicks()

    assert _by_source("no1", "unpark_pump") == []  # F4: the child pump never fires


async def test_unresolvable_child_still_fires_pump(mock_db):
    # A running sub-agent admits the pump lane, but the terminal child has NO
    # owner linkage (unresolvable) → today's immediate fire, never deferred.
    task_registry.start_task("ur1", "subagent-1", "agent", "spawning")
    frame = _terminal_frame("gc-unlinked", "step")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("ur1", frame)
        await _drain_kicks()

    assert "ur1" not in turn_scheduler._pending_pump  # not deferred
    assert len(_by_source("ur1", "unpark_pump")) == 1


async def test_mark_woken_prevents_deferred_re_pump(mock_db, monkeypatch):
    # A swept child that already pumped must not pump again on a late duplicate
    # frame: the deferred fire drops it via the mark_woken latch.
    monkeypatch.setattr(turn_scheduler, "PUMP_QUIET_WINDOW_SECONDS", 0.05)
    frame = _setup_owner_child("mw1")
    live = turns_db.enqueue_turn("mw1", "user", prompt="working")
    turns_db.start_turn(live["id"], None, "runner")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("mw1", frame)
        await turn_scheduler._pending_pump["mw1"]["owner-A"]["task"]
        await _drain_kicks()
        assert len(_by_source("mw1", "unpark_pump")) == 1
        assert task_registry.is_woken("mw1", "child-B")

        # Late duplicate frame for the SAME child → deferred, but the latch drops
        # it at the deadline: no second pump.
        await turn_scheduler.on_task_terminal("mw1", frame)
        await turn_scheduler._pending_pump["mw1"]["owner-A"]["task"]
        await _drain_kicks()

    assert len(_by_source("mw1", "unpark_pump")) == 1


async def test_pump_late_frame_after_wake_left_queue_no_reenqueue(mock_db):
    # F3: an UNRESOLVABLE child (no owner linkage) pumps IMMEDIATELY, latching the
    # woken mark BEFORE the enqueue. The pump wake then STARTS (leaves the queue),
    # so the dedup lookup finds nothing. A duplicate frame for the same child must
    # find the pre-enqueue latch set and NOT spawn a second pump.
    task_registry.start_task("plf1", "subagent-1", "agent", "spawning")
    frame = _terminal_frame("gc-late", "step")

    with ExitStack() as stack:
        stack.enter_context(_patch_execute())
        stack.enter_context(patch(
            "app.core.query_engine._read_task_output_file", return_value="",
        ))
        await turn_scheduler.on_task_terminal("plf1", frame)
        await _drain_kicks()
        (wake,) = _by_source("plf1", "unpark_pump")
        assert task_registry.is_woken("plf1", "gc-late")  # latched pre-enqueue
        # The pump wake starts — it leaves the queue (dedup lookup will miss).
        turns_db.start_turn(wake["id"], None, "runner")

        await turn_scheduler.on_task_terminal("plf1", frame)  # duplicate late frame
        await _drain_kicks()

    assert _by_source("plf1", "unpark_pump") == []  # no fresh duplicate pump


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
        assert "<subagent_tokens></subagent_tokens>" in before["prompt"]

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
