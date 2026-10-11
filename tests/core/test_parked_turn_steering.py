"""A parked foreground attempt is still part of its unfinished logical turn."""
from unittest.mock import patch

import pytest

from app.core import query_persistence, session_runtime, turn_recorder
from app.db import database, session_events, turns
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import _runtime_env, _clean_module_state  # noqa: F401


@pytest.mark.asyncio
async def test_park_send_and_wake_share_one_turn_and_terminal(mock_db):
    sid = "park-steer"
    database.create_session(session_id=sid, profile_id="default")
    create_recorder = turn_recorder.TurnRecorder.create_for_run
    first = turns.enqueue_turn(sid, "user", prompt="start", command_id="first")
    usage = {"tokens_in": 10, "tokens_out": 2, "total_cost_usd": 0.01, "num_turns": 1}
    pump = make_pump([{"type": "stream_delta", "delta_type": "text", "content": "answer"},
                      {"type": "done", "metadata": usage}])
    with _runtime_env(pump=pump, bg_work=[{"id": "monitor"}]) as env, patch.object(
        turn_recorder.TurnRecorder, "create_for_run", new=create_recorder,
    ), patch.object(session_runtime, "record_parked", new=turn_recorder.record_parked):
        assert await session_runtime.SessionRuntime(first).execute() == "parked"
        rows = session_events.get_events(sid)
        assert "turn.completed" not in [r["event_type"] for r in rows]
        assert rows[-1]["event_type"] == "turn.parked"
        assert rows[-1]["turn_id"] == first["id"]

        send = turns.enqueue_turn(sid, "user", prompt="also do this", command_id="steer")
        resumed = session_runtime.SessionRuntime(send)
        assert await resumed.execute() == "parked"
        assert resumed.turn_id == first["id"]
        assert turns.get_turn(send["id"])["stop_reason"] == "steered"
        assert turns.get_turn(first["id"])["payload"]["parked_usage"]["tokens_in"] == 20

        env["registry"].get_parking_background_work.return_value = []
        wake = turns.enqueue_turn(sid, "unpark", prompt="monitor completed")
        assert await session_runtime.SessionRuntime(wake).execute() == "completed"

    rows = session_events.get_events(sid)
    types = [r["event_type"] for r in rows]
    starts = [r for r in rows if r["event_type"] == "turn.started"]
    assert len(starts) == 3
    assert [r["payload"].get("continued", False) for r in starts] == [False, True, True]
    assert types.count("turn.parked") == 2
    assert types.count("turn.completed") == 1
    assert types[-1] == "turn.completed"
    assert {r["turn_id"] for r in rows} == {first["id"]}
    steer = next(r for r in rows if r["event_type"] == "message.user" and r["command_id"] == "steer")
    assert steer["payload"]["content"] == "also do this"
    assert rows[-1]["payload"]["usage"]["tokens_in"] == 30
    assert rows[-1]["payload"]["usage"]["total_cost_usd"] == pytest.approx(0.03)
    assert turns.get_live_turn(sid) is None
    assert turns.get_queued_turns(sid) == []
    # The REST event projection/export does not drop the second user message.
    from app.api.sessions_helpers import load_event_messages
    from app.api.events import _wire
    messages, present = load_event_messages(sid)
    assert present
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "start", "also do this", "monitor completed",
    ]
    assert { _wire(r)["turn_id"] for r in rows } == {first["id"]}


@pytest.mark.asyncio
async def test_completed_turn_send_is_new_turn(mock_db):
    sid = "completed-send"
    first = turns.enqueue_turn(sid, "user", prompt="first")
    with _runtime_env(pump=make_pump([{"type": "done"}])):
        assert await session_runtime.SessionRuntime(first).execute() == "completed"
        second = turns.enqueue_turn(sid, "user", prompt="second")
        runtime = session_runtime.SessionRuntime(second)
        assert await runtime.execute() == "completed"
    assert runtime.turn_id == second["id"] != first["id"]


@pytest.mark.parametrize("change", [
    {"prompt": "/compact"}, {"profile_id": "different"},
    {"overrides": {"model": "other"}}, {"payload": {"upload_blob_ids": ["blob"]}},
    {"source": "resume"}, {"project_id": "other"},
])
def test_incompatible_send_keeps_its_queued_identity(mock_db, change):
    sid = "incompatible"
    first = turns.enqueue_turn(sid, "user", prompt="first")
    turns.start_turn(first["id"])
    turns.park_turn(first["id"], ["task"])
    second = turns.enqueue_turn(sid, **{"source": "user", "prompt": "next", **change})
    started = turns.start_turn(second["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked)
    assert started["id"] == second["id"]
    assert turns.get_turn(first["id"])["state"] == "parked"


def test_resumed_claim_is_idempotent_and_crash_requeues_same_input(mock_db):
    sid = "park-race"
    first = turns.enqueue_turn(sid, "user", prompt="first", payload={"agent_message_reply_to": "parent"})
    turns.start_turn(first["id"])
    turns.park_turn(first["id"], ["task"])
    second = turns.enqueue_turn(sid, "user", prompt="important", command_id="second")
    start = lambda: turns.start_turn(second["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked)
    assert start()["id"] == first["id"]
    assert start() is None
    turns.requeue_turn(first["id"])
    retried = turns.start_turn(first["id"])
    assert retried["prompt"] == "important"
    assert retried["payload"]["continuation_command_id"] == "second"
    assert retried["payload"]["agent_message_reply_to"] == "parent"


def test_partial_usage_recovery_only_counts_current_attempt(mock_db):
    sid = "park-usage"
    database.create_session(session_id=sid, profile_id="default")
    for run, tokens in (("old", 10), ("new", 20)):
        # DB insert preserves distinct run attribution without needing live leases.
        with database.get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO session_events (id, session_id, seq, run_id, turn_id, event_type, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run, sid, tokens, run, "same-turn", "turn.usage",
                 '{"usage": {"model_call_index": 1, "input_tokens": %d}}' % tokens),
            )
    totals = query_persistence._partial_usage_from_events(sid, "same-turn", "new")
    assert totals["tokens_in"] == 20
    assert totals["model_calls"] == 1


def test_reopened_turn_counts_only_new_active_interval(mock_db):
    sid = "park-work-time"
    database.create_session(session_id=sid, profile_id="default")
    first = turns.enqueue_turn(sid, "user", prompt="first")
    turns.start_turn(first["id"])
    turns.park_turn(first["id"], ["monitor"])
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET started_at=%s, ended_at=%s WHERE id=%s",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:01+00:00", first["id"]),
        )
        conn.cursor().execute(
            "UPDATE sessions SET work_ms=1000, work_started_at=NULL WHERE id=%s", (sid,),
        )
    second = turns.enqueue_turn(sid, "user", prompt="next")
    resumed = turns.start_turn(second["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked)
    assert resumed["id"] == first["id"]
    assert str(resumed["started_at"]).startswith("2000") is False
    assert database.get_session(sid)["work_ms"] == 1000
    turns.settle_turn(first["id"], "completed")
    assert 1000 <= database.get_session(sid)["work_ms"] < 5000


def test_stop_or_repair_completes_new_format_park_once(mock_db):
    sid = "park-settle"
    database.create_session(session_id=sid, profile_id="default")
    first = turns.enqueue_turn(sid, "user", prompt="first")
    turns.start_turn(first["id"])
    turns.park_turn(first["id"], ["task"], usage={"tokens_in": 10})
    assert turns.settle_parked_turns(sid) == 1
    assert turns.settle_parked_turns(sid) == 0
    rows = session_events.get_events(sid)
    assert len(rows) == 1
    assert rows[0]["turn_id"] == first["id"]
    assert rows[0]["event_type"] == "turn.completed"
    assert rows[0]["payload"]["usage"] == {"tokens_in": 10}


@pytest.mark.asyncio
async def test_each_resumed_attempt_books_once(mock_db):
    sid = "park-booking"
    database.create_session(session_id=sid, profile_id="default")
    first = turns.enqueue_turn(sid, "user", prompt="first")
    turns.start_turn(first["id"], run_id="run-one")
    key1 = await query_persistence.capture_usage_identity(sid)
    turns.park_turn(first["id"], ["task"])
    second = turns.enqueue_turn(sid, "user", prompt="next")
    turns.start_turn(second["id"], run_id="run-two", can_resume=session_runtime.SessionRuntime._can_resume_parked)
    key2 = await query_persistence.capture_usage_identity(sid)
    assert key1 != key2
    assert query_persistence.settlement_turn_id(key1) == query_persistence.settlement_turn_id(key2)
    for key, tokens in ((key1, 10), (key2, 20)):
        for _ in range(2):
            await query_persistence._persist_turn_results(
                sid, "sdk", {"usage_settlement_key": key, "tokens_in": tokens,
                             "num_turns": 1, "total_cost_usd": 0.01},
                "default", interrupted=False,
            )
    assert database.get_session(sid)["total_tokens_in"] == 30
    assert database.get_session(sid)["total_cost_usd"] == pytest.approx(0.02)
