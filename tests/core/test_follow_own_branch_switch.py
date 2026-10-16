"""A session that switches or renames its own worktree's branch keeps its tools (#2369).

The resolvers refuse a branch checkout whose HEAD is not the session's bound
branch, and only turn end corrected the binding. An agent that ran
``git switch`` (or ``git branch -m``) in its own host-assigned worktree lost
its browser and its Task launcher for the rest of the turn. Mid-turn,
agent-facing paths now apply the turn-end resync early, with its checks.

Real tmp git repos and real ``git worktree add`` plus the pg_shim DB, matching
tests/core/test_turn_finalize_branch_resync.py.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.core import project_resolver, turn_finalize
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import database
from app.db import projects_v2
from app.db import turns as turns_db

RUNNER = "runnerA"
OLD = "task/host-assigned"
NEW = "fix/one-branch-per-request"


@pytest.fixture(autouse=True)
def pinned_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: RUNNER)
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _new_session(project_id, branch=None, **fields):
    sid = str(uuid.uuid4())
    database.create_session(
        session_id=sid, profile_id="default", project_v2_id=project_id, **fields
    )
    if branch:
        database.set_session_branch(sid, branch, project_id)
    return sid


def _start_turn(session_id):
    turn = turns_db.enqueue_turn(session_id, "user", prompt="go", profile_id="default")
    assert turns_db.start_turn(turn["id"], None, RUNNER)


@pytest.fixture
def scene(mock_db, tmp_path):
    repo = Path(tmp_path) / "base_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    project = projects_v2.upsert_project("git:follow", name="Follow")
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))

    worktree = Path(tmp_path) / "wt-own"
    _git(repo, "worktree", "add", str(worktree), "-b", OLD)
    checkouts_db.upsert_checkout(
        project["id"], RUNNER, str(worktree), branch=OLD, status="active"
    )
    sid = _new_session(project["id"], OLD)
    return {"sid": sid, "project": project, "repo": repo, "worktree": worktree}


def _row(sid):
    return database.get_session(sid)


def test_a_switch_mid_turn_is_followed_and_the_checkout_resolves(scene):
    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)
    row = _row(scene["sid"])
    assert project_resolver.resolve_existing_cwd(dict(row)) is None

    assert project_resolver.follow_own_branch_switch(row) is True

    assert row["branch"] == NEW
    assert _row(scene["sid"])["branch"] == NEW
    assert project_resolver.resolve_existing_cwd(_row(scene["sid"])) == str(scene["worktree"])
    assert project_resolver.resolve_cwd(_row(scene["sid"])) == str(scene["worktree"])


def test_a_rename_of_the_checked_out_branch_is_followed(scene):
    """``git branch -m`` leaves HEAD attached, on a name the row never saw."""
    _start_turn(scene["sid"])
    _git(scene["worktree"], "branch", "-m", NEW)

    assert project_resolver.follow_own_branch_switch(_row(scene["sid"])) is True
    assert project_resolver.resolve_existing_cwd(_row(scene["sid"])) == str(scene["worktree"])


def test_between_turns_a_switch_is_not_followed(scene):
    """Not this session's agent: the spawn refusal (and its test) stand."""
    _git(scene["worktree"], "switch", "-c", NEW)

    assert project_resolver.follow_own_branch_switch(_row(scene["sid"])) is False
    assert _row(scene["sid"])["branch"] == OLD


def test_a_checkout_another_session_shares_is_never_taken(scene):
    peer = _new_session(scene["project"]["id"], OLD)
    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)

    assert project_resolver.follow_own_branch_switch(_row(scene["sid"])) is False
    assert _row(scene["sid"])["branch"] == OLD
    assert _row(peer)["branch"] == OLD
    row = checkouts_db.get_checkout(scene["project"]["id"], RUNNER, branch=OLD)
    assert row is not None and row["path"] == str(scene["worktree"])


def test_own_workers_and_archived_sessions_do_not_block_the_follow(scene):
    _new_session(scene["project"]["id"], OLD, parent_session_id=scene["sid"])
    gone = _new_session(scene["project"]["id"], OLD)
    database.update_session(gone, status="archived")
    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)

    assert project_resolver.follow_own_branch_switch(_row(scene["sid"])) is True


def test_the_main_checkout_is_never_adopted(mock_db, tmp_path):
    """Even a row that binds a branch to the main working tree stays refused."""
    repo = Path(tmp_path) / "main_repo"
    repo.mkdir()
    _git(repo, "init", "-b", OLD)
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    project = projects_v2.upsert_project("git:main-only", name="Main")
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo), branch=OLD)
    sid = _new_session(project["id"], OLD)
    _start_turn(sid)
    _git(repo, "switch", "-c", NEW)

    assert turn_finalize.resync_session_branch_from_head(sid) is None
    assert project_resolver.follow_own_branch_switch(_row(sid)) is False
    assert _row(sid)["branch"] == OLD


def test_the_browser_config_follows_a_mid_turn_switch(scene, monkeypatch):
    from app.core import browser_runtime, environment, mcp_config, profiles

    monkeypatch.setattr(profiles, "get_profile", lambda pid: {"config": {"env": {}, "ai_tools": {}}})
    monkeypatch.setattr(environment, "_build_env_with_ai_tools", lambda *a: {})
    monkeypatch.setattr(mcp_config, "_build_mcp_servers_dict",
                        lambda cwd, env, sid: {"browser": {"command": "node", "env": {}}})
    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)

    config = browser_runtime.resolve_config(scene["sid"])

    assert config["cwd"] == str(scene["worktree"])
    assert _row(scene["sid"])["branch"] == NEW


def test_the_browser_still_refuses_a_shared_checkout_by_name(scene, monkeypatch):
    from app.core import browser_runtime, profiles

    monkeypatch.setattr(profiles, "get_profile", lambda pid: {"config": {"env": {}, "ai_tools": {}}})
    _new_session(scene["project"]["id"], OLD)
    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)

    with pytest.raises(browser_runtime.RuntimeUnavailable) as refused:
        browser_runtime.resolve_config(scene["sid"])
    assert str(refused.value) == browser_runtime.CHECKOUT_MISMATCH_DETAIL


def test_the_task_launcher_branches_workers_off_the_followed_branch(scene):
    from app.core import worker_checkout

    _start_turn(scene["sid"])
    _git(scene["worktree"], "switch", "-c", NEW)
    worker_id = "worker" + uuid.uuid4().hex[:8]

    checkout = worker_checkout.prepare_worker_checkout(
        _row(scene["sid"]), worker_id, "default", ["Bash"]
    )

    assert checkout is not None
    assert checkout.parent_branch == NEW
    assert _git(checkout.path, "rev-parse", "HEAD") == _git(scene["worktree"], "rev-parse", "HEAD")
