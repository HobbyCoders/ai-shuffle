"""The targeted write that corrects a session's recorded branch.

``set_session_branch`` is intake — the user picks a branch. ``resync_session_branch``
is the correction turn end applies when the agent moved the checkout somewhere
else with a plain ``git checkout -b``.
"""

import uuid

from app.db import database


def _mk(**kwargs):
    session_id = str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default", **kwargs)
    return session_id


def test_resync_writes_the_new_branch(mock_db):
    sid = _mk()
    database.set_session_branch(sid, "fix/update-session-git-state")

    row = database.resync_session_branch(sid, "fix/sidebar-second-pr-state")

    assert row["branch"] == "fix/sidebar-second-pr-state"
    assert database.get_session(sid)["branch"] == "fix/sidebar-second-pr-state"


def test_resync_also_moves_a_legacy_worktree_branch(mock_db):
    """PR reconciliation reads ``worktree_branch`` first, so it must move too."""
    sid = _mk(worktree_branch="wt/old")

    row = database.resync_session_branch(sid, "wt/new")

    assert row["worktree_branch"] == "wt/new"
    assert row["branch"] == "wt/new"


def test_resync_does_not_give_a_v2_session_a_worktree_branch(mock_db):
    """An empty ``worktree_branch`` is how the v2 intake path marks itself."""
    sid = _mk()
    database.set_session_branch(sid, "feat/a")

    row = database.resync_session_branch(sid, "feat/b")

    assert not row["worktree_branch"]
    assert row["branch"] == "feat/b"


def test_resync_of_an_unknown_session_returns_none(mock_db):
    assert database.resync_session_branch("no-such-session", "feat/x") is None


def test_resync_bumps_updated_at_and_leaves_the_rest_alone(mock_db):
    sid = _mk()
    database.set_session_branch(sid, "feat/a")
    database.update_session_pr_state(sid, pr_number=11, pr_state="merged")
    before = database.get_session(sid)

    row = database.resync_session_branch(sid, "feat/b")

    assert row["pr_number"] == 11
    assert row["pr_state"] == "merged"
    assert row["profile_id"] == before["profile_id"]
    assert row["updated_at"] >= before["updated_at"]
