"""One durable usage booking across normal and killed finalizers."""
import asyncio
import pytest
from app.core import query_persistence as qp
from app.db import database, turns, session_events


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_first", [False, True, "concurrent"])
async def test_normal_and_kill_share_claim(mock_db, kill_first):
    sid = "settlement-race"
    database.create_session(session_id=sid, profile_id="default")
    turn = turns.enqueue_turn(sid, "user", prompt="go")
    turns.start_turn(turn["id"], instance_id="test")
    key = await qp.capture_usage_identity(sid)
    session_events.append_events(sid, [{
        "type": "turn.usage", "turn_id": turn["id"],
        "payload": {"usage": {"model_call_index": 1, "input_tokens": 17,
                              "output_tokens": 4, "cost_usd": 0.01}},
    }])
    async def normal():
        await qp._persist_turn_results(
            sid, "sdk", {"usage_settlement_key": key, "tokens_in": 17,
            "tokens_out": 4, "total_cost_usd": 0.01, "num_turns": 1},
            "default", interrupted=True,
        )
    if kill_first == "concurrent":
        await asyncio.gather(normal(), qp.settle_killed_session(sid))
    elif kill_first:
        await qp.settle_killed_session(sid)
        await normal()
    else:
        await normal()
        await qp.settle_killed_session(sid)
    row = database.get_session(sid)
    assert row["total_tokens_in"] == 17
    assert row["total_tokens_out"] == 4
    assert row["total_model_calls"] == 1
    assert row["total_cost_usd"] == pytest.approx(0.01)
    assert mock_db.execute("SELECT COUNT(*) FROM usage_log WHERE session_id=?", (sid,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_legitimate_continuation_books_separately(mock_db):
    """A retried/continued turn is NEW paid work: it gets its own identity
    (its own turns row, or a legacy UUID key) and books separately — the
    claim dedupes an execution attempt, never the user's retry."""
    sid = "settlement-continuation"
    database.create_session(session_id=sid, profile_id="default")

    # Attempt 1: a queued turn that gets killed, booked by the kill path.
    turn1 = turns.enqueue_turn(sid, "user", prompt="first try")
    turns.start_turn(turn1["id"], instance_id="inst-1")
    session_events.append_events(sid, [{
        "type": "turn.usage", "turn_id": turn1["id"],
        "payload": {"usage": {"model_call_index": 1, "input_tokens": 10,
                              "output_tokens": 1, "cost_usd": 0.002}},
    }])
    assert await qp.settle_killed_session(sid) is True

    # Attempt 2: a legitimate retry/continuation — a NEW turn, a new claim.
    turn2 = turns.enqueue_turn(sid, "user", prompt="second try")
    turns.start_turn(turn2["id"], instance_id="inst-2")
    key2 = await qp.capture_usage_identity(sid)
    assert key2 != qp.usage_settlement_key(sid, turn1)
    await qp._persist_turn_results(
        sid, "sdk",
        {"usage_settlement_key": key2, "tokens_in": 20, "tokens_out": 2,
         "total_cost_usd": 0.004, "num_turns": 1},
        "default", interrupted=False,
    )

    row = database.get_session(sid)
    assert row["total_tokens_in"] == 30  # 10 + 20: both attempts booked
    assert row["total_model_calls"] == 2
    assert row["total_cost_usd"] == pytest.approx(0.006)
    assert mock_db.execute(
        "SELECT COUNT(*) FROM usage_log WHERE session_id=?", (sid,)
    ).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_subsequent_turns_and_lease_attempts_count_separately(mock_db):
    sid = "separate-attempts"
    database.create_session(session_id=sid, profile_id="default")
    turn = turns.enqueue_turn(sid, "user", prompt="go")
    turns.start_turn(turn["id"], instance_id="test")
    first = await qp.capture_usage_identity(sid)
    assert database.settle_turn_usage(sid, first, tokens_in=17, model_calls=1)
    assert not database.settle_turn_usage(sid, first, tokens_in=17, model_calls=1)
    turns.requeue_turn(turn["id"])
    turns.start_turn(turn["id"], instance_id="retry", run_id="new-attempt")
    retry = await qp.capture_usage_identity(sid)
    assert retry != first
    assert database.settle_turn_usage(sid, retry, tokens_in=19, model_calls=1)
    turns.settle_turn(turn["id"], "completed")
    second_turn = turns.enqueue_turn(sid, "user", prompt="again")
    turns.start_turn(second_turn["id"], instance_id="test")
    second = await qp.capture_usage_identity(sid)
    assert second not in {first, retry}
    assert database.settle_turn_usage(sid, second, tokens_in=23, model_calls=1)
    row = database.get_session(sid)
    assert row["total_tokens_in"] == 59
    assert row["total_model_calls"] == 3


def test_counter_failure_rolls_back_claim(mock_db, monkeypatch):
    from app.db import sessions
    sid = "rollback-attempt"
    database.create_session(session_id=sid, profile_id="default")
    original = sessions.update_session
    def fail(*args, **kwargs):
        raise RuntimeError("counter write failed")
    monkeypatch.setattr(sessions, "update_session", fail)
    with pytest.raises(RuntimeError, match="counter write failed"):
        database.settle_turn_usage(sid, "rollback-key", tokens_in=17)
    monkeypatch.setattr(sessions, "update_session", original)
    assert database.settle_turn_usage(sid, "rollback-key", tokens_in=17)
    assert database.get_session(sid)["total_tokens_in"] == 17


@pytest.mark.asyncio
async def test_finalizer_without_terminal_leaves_coalesced_records_to_the_kill_path(mock_db):
    """Delegated-leg ``turn.usage`` rows carry no call index and are coalesced
    gauges, so a cancelled pump must not sum them. It books nothing and the
    claim stays open for the kill path's own recovery."""
    sid = "settlement-unindexed"
    database.create_session(session_id=sid, profile_id="default")
    turn = turns.enqueue_turn(sid, "user", prompt="go")
    turns.start_turn(turn["id"], instance_id="test")
    key = await qp.capture_usage_identity(sid)
    session_events.append_events(sid, [{
        "type": "turn.usage", "turn_id": turn["id"],
        "payload": {"usage": {"input_tokens": 17, "output_tokens": 4}},
    }])
    await qp._persist_turn_results(
        sid, "sdk", {"usage_settlement_key": key}, "default",
        interrupted=True,
    )
    assert mock_db.execute(
        "SELECT COUNT(*) FROM usage_log WHERE session_id=?", (sid,)
    ).fetchone()[0] == 0
    assert await qp.settle_killed_session(sid) is True
    assert database.get_session(sid)["total_tokens_in"] == 17


@pytest.mark.asyncio
async def test_finalizer_without_terminal_or_turn_books_nothing(mock_db):
    sid = "settlement-legacy-empty"
    database.create_session(session_id=sid, profile_id="default")
    key = await qp.capture_usage_identity(sid)
    assert qp.settlement_turn_id(key) is None
    await qp._persist_turn_results(
        sid, "sdk", {"usage_settlement_key": key}, "default",
        interrupted=True,
    )
    assert mock_db.execute(
        "SELECT COUNT(*) FROM usage_log WHERE session_id=?", (sid,)
    ).fetchone()[0] == 0
