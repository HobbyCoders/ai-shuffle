"""Unit tests for app/db/interactions.py — pending interaction state machine."""

from app.db import interactions as ix


def test_create_lists_as_pending(mock_db):
    created = ix.create_interaction("s1", "permission", {"tool": "Bash"}, 60)
    assert created["state"] == "pending"
    assert created["payload"] == {"tool": "Bash"}
    pending = ix.get_pending_interactions("s1")
    assert len(pending) == 1
    assert pending[0]["id"] == created["id"]
    assert pending[0]["kind"] == "permission"


def test_answer_once_true_second_false(mock_db):
    created = ix.create_interaction("s1", "permission", {}, 60)
    assert ix.answer_interaction(created["id"], {"allow": True}, "device-1") is True
    assert ix.answer_interaction(created["id"], {"allow": False}) is False
    row = ix.get_interaction(created["id"])
    assert row["state"] == "answered"
    assert row["answer"] == {"allow": True}
    assert row["answered_by_device"] == "device-1"
    # No longer listed as pending.
    assert ix.get_pending_interactions("s1") == []


def test_answer_after_expiry_false(mock_db):
    created = ix.create_interaction("s1", "permission", {}, -1)
    assert ix.answer_interaction(created["id"], {"allow": True}) is False
    # Expired interactions are not listed as pending.
    assert ix.get_pending_interactions("s1") == []


def test_cancel_pending(mock_db):
    created = ix.create_interaction("s1", "permission", {}, 60)
    assert ix.cancel_interaction(created["id"]) is True
    assert ix.get_interaction(created["id"])["state"] == "cancelled"
    # Cancelling again (no longer pending) is a no-op.
    assert ix.cancel_interaction(created["id"]) is False


def test_expire_due_only_flips_overdue_pendings(mock_db):
    live = ix.create_interaction("s1", "a", {}, 60)
    overdue = ix.create_interaction("s1", "b", {}, -1)
    count = ix.expire_due_interactions()
    assert count == 1
    assert ix.get_interaction(live["id"])["state"] == "pending"
    assert ix.get_interaction(overdue["id"])["state"] == "expired"


def test_get_interaction_missing_returns_none(mock_db):
    assert ix.get_interaction("nope") is None


def test_expire_due_detailed_returns_swept_rows(mock_db):
    live = ix.create_interaction("s1", "permission", {}, 60)
    overdue_a = ix.create_interaction("s1", "permission", {"request_id": "req-a"}, -1)
    overdue_b = ix.create_interaction("s2", "question", {"request_id": "req-b"}, -1)
    rows = ix.expire_due_interactions_detailed()
    swept = {r["id"]: r for r in rows}
    assert overdue_a["id"] in swept
    assert overdue_b["id"] in swept
    assert live["id"] not in swept
    # Each row carries session_id + kind so the sweeper can NOTIFY per session.
    assert swept[overdue_a["id"]]["session_id"] == "s1"
    assert swept[overdue_b["id"]]["kind"] == "question"
    # Each row also carries its parsed payload so the sweeper can emit a durable
    # interaction.expired carrying the row's request_id (PR-B).
    assert swept[overdue_a["id"]]["payload"] == {"request_id": "req-a"}
    assert swept[overdue_b["id"]]["payload"] == {"request_id": "req-b"}
    assert ix.get_interaction(live["id"])["state"] == "pending"


def test_get_answered_interactions_filters_by_kind(mock_db):
    perm = ix.create_interaction("s1", "permission", {"p": 1}, 60)
    ques = ix.create_interaction("s1", "question", {"q": 1}, 60)
    ix.answer_interaction(perm["id"], {"decision": "defer"})
    ix.answer_interaction(ques["id"], {"answers": {}})
    perms = ix.get_answered_interactions("s1", "permission")
    assert [r["id"] for r in perms] == [perm["id"]]
    assert perms[0]["answer"] == {"decision": "defer"}
    # No kind filter -> both answered rows, oldest first.
    both = ix.get_answered_interactions("s1")
    assert {r["id"] for r in both} == {perm["id"], ques["id"]}
    # Still-pending rows are excluded.
    pending = ix.create_interaction("s1", "permission", {}, 60)
    assert pending["id"] not in {r["id"] for r in ix.get_answered_interactions("s1")}


def test_mark_interaction_requeued_stamps_answer(mock_db):
    created = ix.create_interaction("s1", "permission", {}, 60)
    ix.answer_interaction(created["id"], {"decision": "defer"})
    assert ix.mark_interaction_requeued(created["id"]) is True
    row = ix.get_interaction(created["id"])
    assert row["answer"]["requeued"] is True
    assert row["answer"]["decision"] == "defer"
    # Requeued deferred rows drop out of the derived deferred set.
    remaining = [
        r for r in ix.get_answered_interactions("s1", "permission")
        if (r.get("answer") or {}).get("decision") == "defer"
        and not (r.get("answer") or {}).get("requeued")
    ]
    assert remaining == []


def test_mark_interaction_requeued_missing_false(mock_db):
    assert ix.mark_interaction_requeued("nope") is False
