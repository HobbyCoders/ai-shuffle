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


# ---------------------------------------------------------------------------
# #1419 review: the failed-move notice keeps the chain context any wake of the
# chat inherits, so it cannot start chats or reset a budget the turn that
# called HandOffSession could not.
# ---------------------------------------------------------------------------

def _notice_after(session_id, payload):
    """The turn that called HandOffSession ended carrying ``payload``; its move
    failed and the agent's notice turn is now the running context."""
    import asyncio
    import time

    from app.core import session_handoff as sh

    ended = turns.enqueue_turn(session_id, "unpark", prompt="handing off", profile_id="agent", payload=payload)
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed', ended_at=NOW() WHERE id=%s", (ended["id"],))
    from app.core import wakeup_service
    from app.db import wakeups

    pending = sh.PendingHandoff("dev-b", "Worker B", "", time.time(), stop_seq=0)
    wake = wakeups.create_wakeup(**sh._notice_wake(session_id, pending, "Hand-off to Worker B failed: offline."))
    asyncio.run(wakeup_service._fire(wake))
    [notice] = [row for row in turns.get_queued_turns(session_id) if (row.get("payload") or {}).get(sh.FAILED_PAYLOAD)]
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='running' WHERE id=%s", (notice["id"],))
    return notice


def test_the_failed_move_notice_of_a_remote_origin_chat_cannot_start_chats(peers):
    root = peers[0][1]["id"]
    notice = _notice_after("caller", {"orchestration_root": root, "orchestration_depth": 1,
                                      "orchestration_remote_origin": "dev-a"})
    assert notice["payload"]["orchestration_remote_origin"] == "dev-a"
    with pytest.raises(ValueError, match="started from another worker"):
        orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s1"}, "start")


def test_the_failed_move_notice_continues_the_chain_it_ended(peers):
    root = peers[0][1]["id"]
    notice = _notice_after("caller", {"orchestration_root": root, "orchestration_depth": 2})
    assert notice["payload"]["orchestration_root"] == root
    assert notice["payload"]["orchestration_depth"] == 2
    sent = orch._queue("caller", {"session_id": "peer", "message": "hi", "request_id": "m1"}, "message")
    assert turns.get_turn(sent["turn_id"])["payload"]["orchestration_root"] == root


# ---------------------------------------------------------------------------
# The stand-in stays the chain's root here once a request was counted under
# it, whatever turn carrying the old root arrives later.
# ---------------------------------------------------------------------------

def _older_candidate(session_id, root=MISSING):
    """A turn carrying the old root that arrives later but sorts first: an
    imported launch marker is forced completed and keeps insert_order 0."""
    turn = turns.enqueue_turn(session_id, "agent_message", prompt="launch", profile_id="agent",
                              payload={"orchestration_root": root})
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed', insert_order=0, "
                              "created_at='2000-01-01 00:00:00' WHERE id=%s", (turn["id"],))
    return turn


def test_two_reports_of_one_moved_chain_keep_one_root_when_an_older_turn_arrives(peers, monkeypatch):
    monkeypatch.setattr(orch, "MAX_STARTS", 1)
    _arrived_by_move("caller")
    first = _report_back("caller")
    sent = orch._queue("caller", {"session_id": "peer", "message": "Review it", "request_id": "m1"}, "message")
    started = orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s1"}, "start")
    assert turns.get_turn(started["turn_id"])["payload"]["orchestration_root"] == first["id"]
    _settle(first)
    _older_candidate("caller")
    _report_back("caller")
    again = orch._queue("caller", {"session_id": "peer", "message": "Review it", "request_id": "m1"}, "message")
    assert again["turn_id"] == sent["turn_id"] and again["created"] is False
    # One budget for both reports: the second start is over it.
    with pytest.raises(ValueError, match="budget"):
        orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "s2"}, "start")
    later = orch._queue("caller", {"session_id": "peer", "message": "Next", "request_id": "m2"}, "message")
    assert turns.get_turn(later["turn_id"])["payload"]["orchestration_root"] == first["id"]


async def test_the_report_of_a_started_chat_that_also_moved_can_message(peers):
    """The likely production route: the parent and the chat it started both
    moved here. The started chat's carried start turn names the old root; its
    report after the hand-off resume lands in the parent here."""
    from app.db import database
    from app.core import session_handoff as sh

    _arrived_by_move("caller")
    database.create_session("child", "agent")
    start = turns.enqueue_turn("child", "agent_message", prompt="go", profile_id="agent",
                               command_id="orchestration:start:carried",
                               payload={"orchestration_sender": "caller", "orchestration_root": MISSING,
                                        "orchestration_depth": 1})
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed', started_at=NOW(), ended_at=NOW() "
                              "WHERE id=%s", (start["id"],))
    resume = turns.enqueue_turn("child", "wakeup", prompt="resume", profile_id="agent",
                                payload={sh.RESUME_PAYLOAD: {"handoff_id": "move-1"}})
    assert turns.start_turn(resume["id"])
    settled = turns.settle_turn(resume["id"], "completed")
    await orch.report_settled_start("child", settled, "completed")
    [report] = [row for row in turns.get_queued_turns("caller") if row["source"] == "unpark"]
    assert report["payload"]["orchestration_root"] == MISSING
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='running' WHERE id=%s", (report["id"],))
    sent = orch._queue("caller", {"session_id": "peer", "message": "Review it", "request_id": "m1"}, "message")
    assert sent["delivery_status"] == "queued"
    assert turns.get_turn(sent["turn_id"])["payload"]["orchestration_root"] == report["id"]
