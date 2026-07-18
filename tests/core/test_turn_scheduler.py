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
    # task-7 is the parent's OWN background work (ownership gate admits it).
    task_registry.start_task("u1", "task-7", "agent", "bg", tool_use_id="tu-7")
    task_registry.mark_background_launch("u1", "tu-7")

    frame = {
        "task_id": "task-7", "status": "completed", "tool_use_id": "tu-7",
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
    assert "internal step of a background agent" in wake["prompt"]  # pump note
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
