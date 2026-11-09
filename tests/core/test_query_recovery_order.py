"""Recovered history cannot be an input to its own ordering or hide a reply hint."""
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from app.db import composer_suggestions, database, query_recovery, session_events, turns
from app.db.base import get_db


@pytest.fixture
def session():
    return database.create_session(session_id="recovery-order", profile_id="default")


def normal(session, stamp, *, turn_id=None, caller=None, **record):
    row = turns.record_adhoc_turn(session["id"], "api_query", turn_id=f"{session['id']}-{turn_id}" if turn_id else None,
                                  overrides={"_caller": caller} if caller else {}, **record)
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET started_at=%s, ended_at=%s, created_at=%s WHERE id=%s",
                              (stamp - timedelta(seconds=1), stamp, stamp, row["id"]))
    return turns.get_turn(row["id"])


def receipt(session, stamp, *, turn_id, caller=None):
    return query_recovery.begin(session["id"], "api_query", started_at=stamp, turn_id=f"{session['id']}-{turn_id}",
                                instance_id="order-install", process_id="old-process",
                                overrides={"_caller": caller} if caller else {})


def replay():
    return query_recovery.recover(instance_id="order-install", process_id="new-process")


def history(session):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, insert_order, position, created_at FROM turns WHERE session_id=%s "
                    "ORDER BY insert_order, created_at, position, id", (session["id"],))
        return [dict(row) for row in cur.fetchall()]


@pytest.mark.parametrize("count", [2, 5])
def test_reverse_time_replay_uses_only_immutable_history_boundaries(session, count):
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    first = normal(session, stamp, caller="stale-first")
    last = normal(session, stamp + timedelta(hours=2))
    saved = []
    for index in range(count):
        # Recovery reads ascending IDs: the latest receipt is replayed first.
        saved.append(receipt(session, stamp + timedelta(minutes=10 * (index + 1)),
                             turn_id=f"turn-reverse-{count - index}",
                             caller=f"receipt-{index}" if index != count - 1 else None))
    assert replay() == count
    assert [r["id"] for r in history(session)] == [first["id"], *[r["id"] for r in saved], last["id"]]
    assert turns.latest_caller(session["id"]) == f"receipt-{count - 2}"
    assert turns.get_latest_turn(session["id"])["id"] == last["id"]
    for existing in (first, last):
        assert turns.get_turn(existing["id"])["insert_order"] == existing["insert_order"]
    assert replay() == 0


@pytest.mark.parametrize("count", [1, 2], ids=["two-way", "three-way"])
def test_exact_tie_preserves_real_suggestion_save_and_get(session, count):
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    completed = normal(session, stamp, turn_id="turn-a-observed")
    session_events.append_events(session["id"], [{"type": "turn.completed", "turn_id": completed["id"],
                                                 "payload": {"stop_reason": "end_turn"}}])
    assert composer_suggestions.save(session["id"], completed["id"], "Keep this reply")
    assert composer_suggestions.get(session["id"])["suggestion"] == "Keep this reply"
    for index in range(count):
        receipt(session, stamp, turn_id=f"turn-z-unknown-{index}")
    assert replay() == count
    assert turns.get_latest_turn(session["id"])["id"] == completed["id"]
    assert composer_suggestions.get(session["id"]) == {
        "session_id": session["id"], "turn_id": completed["id"], "suggestion": "Keep this reply"}
    assert composer_suggestions.save(session["id"], completed["id"], "Still current")
    assert composer_suggestions.get(session["id"])["suggestion"] == "Still current"


@pytest.mark.parametrize("count", [2, 5])
@pytest.mark.parametrize("placement", ["gap", "before", "tail", "empty"])
@pytest.mark.parametrize("order", ["forward", "reverse", "mixed"])
def test_replay_order_cannot_change_a_bucket_or_existing_watermarks(session, count, placement, order):
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    existing = [] if placement == "empty" else [normal(session, stamp), normal(session, stamp + timedelta(hours=2))]
    minutes = {"gap": 10, "empty": 10, "before": -100, "tail": 180}[placement]
    times = [stamp + timedelta(minutes=minutes + index * 10) for index in range(count)]
    indices = (list(range(count)) if order == "forward" else list(reversed(range(count)))
               if order == "reverse" else ([4, 0, 3, 1, 2] if count == 5 else [1, 0])) if isinstance(order, str) else list(order)
    saved = [receipt(session, at, turn_id=f"turn-order-{indices.index(index)}")
             for index, at in enumerate(times)]
    assert replay() == count
    # begin() returns its serialized record, not a turn row: use known timestamps.
    chronology = [(turns._to_utc(row["created_at"]), row["id"]) for row in existing] + [(at, row["id"]) for at, row in zip(times, saved)]
    assert [r["id"] for r in history(session)] == [tid for _, tid in sorted(chronology)]
    rows = [turns.get_turn(r["id"]) for r in saved]
    assert len({r["insert_order"] for r in rows}) == 1
    assert len({r["position"] for r in rows}) == 1
    for row in existing:
        current = turns.get_turn(row["id"])
        assert (current["insert_order"], current["position"]) == (row["insert_order"], row["position"])
    assert replay() == 0


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
def test_unknown_start_anchor_ignores_recovered_history(session, order):
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    first, last = normal(session, stamp), normal(session, stamp + timedelta(hours=2))
    times = [stamp - timedelta(hours=1), None, stamp + timedelta(hours=1)]
    saved = [receipt(session, at, turn_id=f"turn-mixed-{order.index(index)}") for index, at in enumerate(times)]
    assert replay() == 3
    assert [r["id"] for r in history(session)] == [saved[0]["id"], saved[1]["id"], first["id"], saved[2]["id"], last["id"]]
    assert turns._to_utc(turns.get_turn(saved[1]["id"])["created_at"]) == turns._to_utc(first["started_at"])
    assert turns.get_latest_turn(session["id"])["id"] == last["id"]


def test_old_recovery_marker_is_not_a_boundary_even_with_observed_end(session):
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    first, last = normal(session, stamp), normal(session, stamp + timedelta(hours=2))
    later = receipt(session, stamp + timedelta(minutes=20), turn_id="turn-legacy-replay")
    assert replay() == 1
    # The first receipt design wrote this same durable marker with ended_at=NOW().
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET ended_at=NOW() WHERE id=%s", (later["id"],))
    earlier = receipt(session, stamp + timedelta(minutes=10), turn_id="turn-earlier-replay")
    assert replay() == 1
    assert [r["id"] for r in history(session)] == [first["id"], earlier["id"], later["id"], last["id"]]


def test_stop_reader_uses_latest_history_not_mutable_queue_position(session):
    from app.core import owner_services
    stopped = turns.record_adhoc_turn(session["id"], "api_query", outcome="aborted", stop_reason="user_stopped")
    assert owner_services.session_stopped(session["id"])
    newest = turns.enqueue_turn(session["id"], "user", prompt="New input", at_front=True)
    assert newest["position"] < stopped["position"]
    assert turns.get_latest_turn(session["id"])["id"] == newest["id"]
    assert not owner_services.session_stopped(session["id"])


def test_latest_reply_credit_uses_history_not_created_time(session):
    from app.core.session_orchestration import _reply_credit
    older = turns.enqueue_turn(session["id"], "agent_message", payload={"orchestration_sender": "peer"})
    newer = turns.enqueue_turn(session["id"], "agent_message", payload={"orchestration_sender": "peer"})
    # Exact tie with IDs deliberately opposing immutable admission order.
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE turns SET id='turn-z-older', created_at=%s WHERE id=%s", (newer["created_at"], older["id"]))
        cur.execute("UPDATE turns SET id='turn-a-newer' WHERE id=%s", (newer["id"],))
        assert _reply_credit(cur, session["id"], "peer") == "turn-a-newer"


def test_held_progress_does_not_hide_an_observed_saved_suggestion(session):
    completed = turns.record_adhoc_turn(session["id"], "api_query")
    session_events.append_events(session["id"], [{"type": "turn.completed", "turn_id": completed["id"], "payload": {}}])
    assert composer_suggestions.save(session["id"], completed["id"], "Reply")
    turns.record_adhoc_turn(session["id"], "agent_message", outcome="aborted", stop_reason="held_progress")
    assert composer_suggestions.get(session["id"])["suggestion"] == "Reply"


@pytest.mark.parametrize("order", list(permutations(range(5))))
def test_all_five_receipt_replay_orders(session, order):
    test_replay_order_cannot_change_a_bucket_or_existing_watermarks(session, 5, "gap", order)


def test_a_normal_shutdown_terminal_remains_an_immutable_boundary(session):
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    first = normal(session, stamp)
    last = normal(session, stamp + timedelta(hours=2), outcome="aborted", stop_reason="server_shutdown",
                  error="Normal finalization observed the shutdown")
    one = receipt(session, stamp + timedelta(minutes=10), turn_id="turn-z-first")
    two = receipt(session, stamp + timedelta(minutes=20), turn_id="turn-a-second")
    assert replay() == 2
    assert [r["id"] for r in history(session)] == [first["id"], one["id"], two["id"], last["id"]]
    assert turns.get_turn(one["id"])["insert_order"] == first["insert_order"]
    assert turns.get_turn(two["id"])["insert_order"] == first["insert_order"]
