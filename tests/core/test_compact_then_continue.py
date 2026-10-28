"""Compact, then continue: the lifecycle for a turn the context limit cut off.

A delegated turn the CLI stops at the context limit reports the engine outcome
``needs_compaction`` (durable as ``terminal_reason`` on ``turn.completed``).
The scheduler then admits ONE atomic pair — a Fluid ``/compact`` and the
continuation behind it — and starts the continuation only once the compaction
compacted. See ``app.core.compact_continue``.

The turns/session_events tables are exercised for real (pg_shim SQLite); only
the runtime start (``session_runtime.execute``) is patched, so a "started"
turn stays queued and the test settles it by hand.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import compact_continue as cc
from app.core import session_runtime, turn_manager, turn_scheduler, wakeup_service
from app.core.task_registry import task_registry
from app.db import session_events as se_db
from app.db import turns as turns_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    from app.core import provider_agent_supervisor
    provider_agent_supervisor.reset()
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    yield
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()


@pytest.fixture
def execute():
    with patch("app.core.session_runtime.execute", new=AsyncMock(return_value="completed")) as run:
        yield run


@pytest.fixture
def notes():
    with patch.object(wakeup_service, "announce_note", new=AsyncMock()) as note:
        yield note


async def _drain_kicks():
    for _ in range(20):
        tasks = list(turn_scheduler._kick_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _run(turn, *, outcome="completed", terminal=None):
    """Start and settle ``turn``, seeding its terminal ``turn.completed``."""
    assert turns_db.start_turn(turn["id"], None, "runner") is not None
    turns_db.settle_turn(turn["id"], outcome)
    if terminal is not None:
        se_db.append_events(turn["session_id"], [{
            "type": "turn.completed", "turn_id": turn["id"],
            "payload": {"stop_reason": "end_turn", "usage": {}, **terminal},
        }])
    return turns_db.get_turn(turn["id"])


CUT = {"terminal_reason": "needs_compaction"}


def _cut_turn(session_id, *, source="user", **kw):
    return _run(turns_db.enqueue_turn(session_id, source, prompt="long task", **kw), terminal=CUT)


def _queued_pair_rows(session_id):
    return [t for t in turns_db.get_queued_turns(session_id) if t["source"] == cc.SOURCE]


def _pair(session_id):
    compact, cont = _queued_pair_rows(session_id)
    assert compact["prompt"] == cc.COMPACT_PROMPT
    assert cont["payload"]["compact_before_turn_id"] == compact["id"]
    return compact, cont


def _compacted(compact):
    se_db.append_events(compact["session_id"], [{
        "type": "system.compaction_boundary", "turn_id": compact["id"],
        "payload": {"subtype": "compact_boundary", "data": {}},
    }])


def _compact_failed(compact, *, nothing=False):
    data = {"reason": "no", **({"nothing_to_compact": True} if nothing else {})}
    se_db.append_events(compact["session_id"], [{
        "type": "system.note", "turn_id": compact["id"],
        "payload": {"subtype": "compact_failed", "data": data},
    }])


async def _settle(session_id, turn):
    await turn_scheduler.on_turn_settled(session_id, turn["id"], turn["state"])
    await _drain_kicks()


# ─── (a) one pair, one continuation, idempotent across a restart ────────────


async def test_cut_turn_compacts_then_continues_exactly_once(mock_db, execute, notes):
    cut = _cut_turn("c1", profile_id="p1", payload={"api_user_id": "u1"},
                    overrides={"model": "m1"})
    await _settle("c1", cut)

    compact, cont = _pair("c1")
    assert cont["prompt"] == cc.AUTO_CONTINUE_PROMPT
    assert cont["parent_turn_id"] == cut["id"]
    assert cont["command_id"] == f"{cc.COMMAND_PREFIX}{cut['id']}"
    assert cont["payload"][cc.PAYLOAD_KEY] == {
        "interrupted_turn_id": cut["id"], "generation": 1, "attempt": 1,
    }
    assert compact["payload"][cc.PAYLOAD_KEY]["interrupted_turn_id"] == cut["id"]
    # The app's continuation is never written as a message the owner sent.
    assert "deferred_user_record" not in cont["payload"]
    # The compaction runs first.
    assert [c.args[0]["id"] for c in execute.await_args_list] == [compact["id"]]

    # A restart replays the settlement: the same pair, no second one.
    turn_scheduler._kick_tasks.clear()
    await _settle("c1", cut)
    assert [t["id"] for t in _queued_pair_rows("c1")] == [compact["id"], cont["id"]]

    # The compaction compacted: the continuation (and only it) starts.
    execute.reset_mock()
    _compacted(compact)
    await _settle("c1", _run(compact))
    assert [c.args[0]["id"] for c in execute.await_args_list] == [cont["id"]]
    notes.assert_not_awaited()


async def test_continuation_waits_while_its_compaction_runs(mock_db, execute):
    await _settle("c2", _cut_turn("c2"))
    compact, cont = _pair("c2")
    assert turns_db.start_turn(compact["id"], None, "runner") is not None
    execute.reset_mock()
    assert await turn_scheduler._gate_continuation("c2", cont) is False
    await _drain_kicks()
    execute.assert_not_awaited()
    assert turns_db.get_turn(cont["id"])["state"] == "queued"


# ─── (b) a continuation cut again gets its own generation ──────────────────


async def test_a_continuation_cut_again_gets_a_new_generation(mock_db, execute):
    first = _cut_turn("c3")
    await _settle("c3", first)
    compact, cont = _pair("c3")
    _compacted(compact)
    await _settle("c3", _run(compact))

    # The continuation itself stops at the limit.
    cont = _run(turns_db.get_turn(cont["id"]), terminal=CUT)
    await _settle("c3", cont)
    compact2, cont2 = _pair("c3")
    assert cont2["parent_turn_id"] == cont["id"]
    assert cont2["payload"][cc.PAYLOAD_KEY]["generation"] == 2
    assert compact2["id"] != compact["id"]


async def test_generations_stop_after_the_runaway_cap(mock_db, execute, notes):
    link = {"interrupted_turn_id": "older", "generation": cc.MAX_GENERATIONS, "attempt": 1}
    cut = _cut_turn("c4", source=cc.SOURCE, payload={cc.PAYLOAD_KEY: link})
    await _settle("c4", cut)
    assert _queued_pair_rows("c4") == []
    notes.assert_awaited_once()


async def test_only_the_engine_outcome_starts_a_pair(mock_db, execute):
    plain = _run(turns_db.enqueue_turn("c5", "user", prompt="x"),
                 terminal={"stop_reason": "tool_use"})
    await _settle("c5", plain)
    assert _queued_pair_rows("c5") == []


async def test_the_setting_turns_it_off(mock_db, execute):
    from app.core.config import settings

    with patch.object(settings, "auto_continue_on_compact", False):
        await _settle("c6", _cut_turn("c6"))
    assert _queued_pair_rows("c6") == []


# ─── (c) a message the owner queued wins ────────────────────────────────────


async def test_queued_owner_message_at_settlement_suppresses_the_pair(mock_db, execute):
    cut = _cut_turn("c7")
    owner = turns_db.enqueue_turn("c7", "queue", prompt="actually, do this")
    await _settle("c7", cut)
    assert _queued_pair_rows("c7") == []
    assert [c.args[0]["id"] for c in execute.await_args_list] == [owner["id"]]


async def test_owner_message_during_compaction_resumes_instead(mock_db, execute, notes):
    await _settle("c8", _cut_turn("c8"))
    compact, cont = _pair("c8")
    owner = turns_db.enqueue_turn("c8", "user", prompt="new instruction")
    _compacted(compact)
    execute.reset_mock()
    await _settle("c8", _run(compact))

    dropped = turns_db.get_turn(cont["id"])
    assert dropped["state"] == "aborted" and dropped["stop_reason"] == "superseded_by_owner"
    assert [c.args[0]["id"] for c in execute.await_args_list] == [owner["id"]]
    notes.assert_not_awaited()  # the owner's message is what resumes the work


# ─── (d) Stop suppresses it ─────────────────────────────────────────────────


async def test_stop_before_settlement_admits_nothing(mock_db, execute):
    cut = _cut_turn("c9")
    task_registry.mark_user_stopped("c9")
    try:
        await _settle("c9", cut)
    finally:
        task_registry.clear_user_stop("c9")
    assert _queued_pair_rows("c9") == []


async def test_stop_after_admission_aborts_the_pair_without_a_not_sent_row(mock_db, execute):
    from app.core.query_session_lifecycle import enter_user_stop
    from app.db import sessions

    await _settle("c10", _cut_turn("c10"))
    compact, cont = _pair("c10")
    try:
        await enter_user_stop("c10")
        assert {turns_db.get_turn(t)["state"] for t in (compact["id"], cont["id"])} == {"aborted"}
        # The app's continuation prompt never shows as a message the owner
        # wrote and did not send.
        messages = sessions.get_session_messages("c10")
        assert not any(cc.AUTO_CONTINUE_PROMPT in str(m.get("content")) for m in messages)
        assert not any(e["event_type"] == "message.user" and e["turn_id"] == cont["id"]
                       for e in se_db.get_events("c10", 0, 100))
    finally:
        task_registry.clear_user_stop("c10")


async def test_never_beside_a_parked_turn(mock_db, execute):
    # Parked work continues through its unpark wake; a turn started beside a
    # parked SendMessage continuation is what lost a worker its parent (#123).
    turn = turns_db.enqueue_turn("c13", "agent_message", prompt="continue",
                                 payload={"agent_message_reply_to": "parent"})
    assert turns_db.start_turn(turn["id"], None, "runner") is not None
    turns_db.park_turn(turn["id"], ["task-1"])
    se_db.append_events("c13", [{"type": "turn.completed", "turn_id": turn["id"],
                                 "payload": {"usage": {}, **CUT}}])
    await _settle("c13", turns_db.get_turn(turn["id"]))
    assert _queued_pair_rows("c13") == []


# ─── compaction failures: bounded retry, honest no-op ───────────────────────


async def test_a_failed_compaction_is_retried_then_given_up(mock_db, execute, notes):
    await _settle("c11", _cut_turn("c11"))
    compact, cont = _pair("c11")
    seen = [compact["id"]]
    for attempt in range(2, cc.MAX_COMPACTION_ATTEMPTS + 1):
        _compact_failed(compact)
        await _settle("c11", _run(compact, outcome="aborted"))
        cont = turns_db.get_turn(cont["id"])
        assert cont["state"] == "queued"
        assert cont["payload"][cc.PAYLOAD_KEY]["attempt"] == attempt
        compact = turns_db.get_turn(cont["payload"]["compact_before_turn_id"])
        assert compact["state"] == "queued" and compact["id"] not in seen
        seen.append(compact["id"])
        # The retry is idempotent per attempt.
        again = turns_db.retry_pair_compaction(cont["id"], attempt)
        assert again["id"] == compact["id"]

    _compact_failed(compact)
    await _settle("c11", _run(compact, outcome="aborted"))
    dropped = turns_db.get_turn(cont["id"])
    assert dropped["state"] == "aborted" and dropped["stop_reason"] == "compaction_failed"
    notes.assert_awaited_once()
    assert not any(c.args[0]["id"] == cont["id"] for c in execute.await_args_list)


async def test_a_refused_retry_drops_the_continuation(mock_db, execute, notes):
    await _settle("c14", _cut_turn("c14"))
    compact, cont = _pair("c14")
    _compact_failed(compact)
    with patch.object(turns_db, "retry_pair_compaction", return_value=None):
        await _settle("c14", _run(compact, outcome="aborted"))
    dropped = turns_db.get_turn(cont["id"])
    assert dropped["state"] == "aborted" and dropped["stop_reason"] == "compaction_failed"
    notes.assert_awaited_once()


async def test_nothing_to_compact_drops_the_continuation_with_a_note(mock_db, execute, notes):
    await _settle("c12", _cut_turn("c12"))
    compact, cont = _pair("c12")
    _compact_failed(compact, nothing=True)
    await _settle("c12", _run(compact))
    dropped = turns_db.get_turn(cont["id"])
    assert dropped["state"] == "aborted" and dropped["stop_reason"] == "nothing_to_compact"
    notes.assert_awaited_once()
    assert "reclaimed nothing" in notes.await_args.args[1]


# ─── (e) usage-reset compact-then-resume: one atomic, idempotent admission ──


def test_limit_resume_pair_is_one_idempotent_admission(mock_db):
    from app.core import limit_resume as lr

    def admit():
        return turns_db.enqueue_turn(
            "r1", "wakeup", prompt=lr.RESUME_PROMPT, command_id=f"{lr.RESUME_COMMAND_PREFIX}wake-1",
            payload={lr.PAYLOAD_KEY: {"limit_key": "k"}, "api_user_id": "u", "compact_first": True},
        )

    # The DB layer mirrors the producers' keys (it cannot import them).
    assert turns_db.LIMIT_RESUME_KEY == lr.PAYLOAD_KEY
    assert turns_db.COMPACTION_CONTINUATION_KEY == cc.PAYLOAD_KEY
    assert cc.SOURCE in turns_db.AUTOMATIC_PAIR_SOURCES

    resume = admit()
    assert admit()["id"] == resume["id"]  # a retried fire finds the same pair
    compact, follow = turns_db.get_queued_turns("r1")
    assert (compact["prompt"], follow["id"]) == (lr.COMPACT_PROMPT, resume["id"])
    assert follow["payload"]["compact_before_turn_id"] == compact["id"]
    # Both rows are the resume's, so the usage-limit hold lets both run.
    assert compact["payload"][lr.PAYLOAD_KEY] == {"limit_key": "k"}
    assert follow["payload"][lr.PAYLOAD_KEY] == {"limit_key": "k"}
    assert "deferred_user_record" not in follow["payload"]


# ─── (f) the idle watchdog no longer sends /compact turns ───────────────────


def test_the_idle_watchdog_is_retired():
    from app.core.config import settings
    from app.db import wakeups as wakeups_db

    for name in ("scan_once", "watchdog_loop", "WATCHDOG_COMPACT_PROMPT", "WATCHDOG_NUDGE_PROMPT"):
        assert not hasattr(wakeup_service, name)
    assert not hasattr(wakeups_db, "idle_edge_candidates")
    assert not any(name.startswith("idle_watchdog") for name in type(settings).model_fields)


async def test_rehydrate_drops_rows_the_watchdog_left(mock_db):
    from app.core.config import settings
    from app.db import wakeups as wakeups_db

    iid = settings.effective_instance_id
    wakeups_db.create_wakeup(id="w-old", session_id="r2", prompt="/compact", fire_at=0.0,
                             instance_id=iid, source="idle_watchdog")
    wakeups_db.create_wakeup(id="w-keep", session_id="r2", prompt="check CI", fire_at=1e12,
                             instance_id=iid, source="schedule_wakeup")
    with patch.object(wakeup_service, "_arm") as arm:
        assert await wakeup_service.rehydrate_pending() == 1
    assert [c.args[0]["id"] for c in arm.call_args_list] == ["w-keep"]
    assert not wakeups_db.session_has_pending("r2", source="idle_watchdog")


# ─── the adapter carries the engine outcome into the terminal payload ───────


def test_adapter_maps_the_outcome_to_terminal_reason():
    from fluid.events.model import NEEDS_COMPACTION, ErrorEvent, TurnComplete

    import app.core.provider.harness_adapter as ha

    adapter = ha.HarnessAdapter()
    [done] = adapter._translate(TurnComplete(
        session_id="s", turn_id="t", stop_reason="end_turn", num_model_calls=1,
        outcome=NEEDS_COMPACTION,
    ))
    assert done.terminal_reason == NEEDS_COMPACTION
    [plain] = adapter._translate(TurnComplete(
        session_id="s", turn_id="t", stop_reason="end_turn", num_model_calls=1,
    ))
    assert plain.terminal_reason == "completed"
    [failed] = adapter._translate(ErrorEvent(
        session_id="s", turn_id="t", message="Prompt is too long", outcome=NEEDS_COMPACTION,
    ))
    assert failed.is_error and failed.terminal_reason == NEEDS_COMPACTION
    assert cc.needs_compaction({"terminal_reason": failed.terminal_reason}) is True
