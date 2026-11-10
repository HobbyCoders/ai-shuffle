"""#1417: a report-back turn whose chain root stayed on the previous worker.

A report from a chat the coordinator started carries the ``orchestration_root``
of the original StartSession: a turn row on the worker the coordinator ran on.
After a hand-off that row is not in this worker's ``turns`` table.
"""
import json

import pytest

from app.core import session_orchestration as orch
from app.db import turns
from app.db.base import get_db
from tests.core.test_session_orchestration import peers  # noqa: F401

MISSING = "root-on-the-previous-worker"


def _arrived_by_move(session_id):
    """The receiver's ownership row after a hand-off (``_accept_owner_row``)."""
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET placement=%s WHERE id=%s",
                              (json.dumps({"where": "runner:this-worker", "handoff": {"token": "move-1"}}),
                               session_id))


def _report_back(session_id, root=MISSING):
    turn = turns.enqueue_turn(session_id, "unpark", prompt="report", profile_id="agent",
                              payload={"orchestration_root": root, "orchestration_depth": 1})
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='running' WHERE id=%s", (turn["id"],))
    return turn


def _settle(turn):
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))


def test_report_back_after_a_hand_off_can_message_and_start(peers):
    _arrived_by_move("caller")
    first = _report_back("caller")
    sent = orch._queue("caller", {"session_id": "peer", "message": "Review it", "request_id": "m1"}, "message")
    assert sent["delivery_status"] == "queued"
    # The chain continues here under the report-back turn as its local root.
    assert turns.get_turn(sent["turn_id"])["payload"]["orchestration_root"] == first["id"]
    started = orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s1"}, "start")
    assert started["created"] is True
    assert turns.get_turn(started["turn_id"])["payload"]["orchestration_root"] == first["id"]
    # A second report of the same chain shares that root: same dedupe and
    # budget scope, not a fresh budget per report.
    _settle(first)
    _report_back("caller")
    again = orch._queue("caller", {"session_id": "peer", "message": "Review it", "request_id": "m1"}, "message")
    assert again["turn_id"] == sent["turn_id"] and again["created"] is False


def test_start_budget_of_the_local_root_still_applies(peers, monkeypatch):
    _arrived_by_move("caller")
    _report_back("caller")
    monkeypatch.setattr(orch, "MAX_STARTS", 1)
    orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s1"}, "start")
    with pytest.raises(ValueError, match="budget"):
        orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s2"}, "start")


def test_an_existing_root_is_kept_after_a_hand_off(peers):
    _arrived_by_move("caller")
    root = peers[0][1]
    _report_back("caller", root=root["id"])
    sent = orch._queue("caller", {"session_id": "peer", "message": "hi", "request_id": "m1"}, "message")
    assert turns.get_turn(sent["turn_id"])["payload"]["orchestration_root"] == root["id"]


def test_a_missing_root_without_a_hand_off_still_refuses_and_says_what_to_do(peers):
    _report_back("caller")
    with pytest.raises(ValueError, match="origin no longer exists") as refused:
        orch._queue("caller", {"session_id": "peer", "message": "hi", "request_id": "m1"}, "message")
    assert "owner's next message" in str(refused.value)
    assert turns.get_queued_turns("peer") == []
