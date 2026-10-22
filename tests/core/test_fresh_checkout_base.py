"""Fresh bases through real Git producers, including two-worker negotiation."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import checkout_manager, git_fork_base, worker_checkout
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME, pin_runner  # noqa: F401
from tests.core.test_remote_worker_checkout import clone, commit, git

TARGET = "fresh-target"


@pytest.fixture
def world(mock_db, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = clone(origin, tmp_path / "seed")
    (seed / ".gitignore").write_text(".fluid/\n")
    git(seed, "add", ".gitignore")
    old = commit(seed, "code.txt", "old\n")
    git(seed, "push", "origin", "main")
    base = clone(origin, tmp_path / "base")
    target = clone(origin, tmp_path / "target")
    caller = tmp_path / "caller"
    git(base, "worktree", "add", "-b", "caller", str(caller))
    fresh = commit(seed, "new.txt", "new main work\n")
    git(seed, "push", "origin", "main")
    project = projects_v2.upsert_project("local:fresh-base", name="Fresh base")
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(base))
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(caller), branch="caller")
    checkouts.upsert_checkout(project["id"], TARGET, str(target))
    database.create_profile("coder", "Coder", None, {"allowed_tools": ["Read", "Edit", "Bash"]})
    database.create_session("parent", "coder", project_v2_id=project["id"])
    database.set_session_branch("parent", "caller", project["id"])
    return dict(row=database.get_session("parent"), origin=origin, seed=seed, base=base,
                target=target, caller=caller, project=project, old=old, fresh=fresh)


def prepare(w, **kwargs):
    return worker_checkout.prepare_worker_checkout(w["row"], "fresh-test", "coder", **kwargs)


def test_behind_caller_gets_fresh_default_without_touching_caller(world):
    w = world
    (w["caller"] / "dirty.txt").write_text("keep me")
    before = git(w["caller"], "status", "--porcelain")
    checkout = prepare(w)
    assert checkout.base_commit == w["fresh"] == git(checkout.path, "rev-parse", "HEAD")
    assert "Fresh origin/main." in checkout.instructions()
    assert git(w["caller"], "rev-parse", "HEAD") == w["old"]
    assert git(w["caller"], "branch", "--show-current") == "caller"
    assert git(w["caller"], "status", "--porcelain") == before


def test_unique_caller_commits_stay_and_report_distance(world):
    w = world
    own = commit(w["caller"], "own.txt", "unique work")
    checkout = prepare(w)
    assert checkout.base_commit == own == git(checkout.path, "rev-parse", "HEAD")
    assert "Caller HEAD; 1 commit behind origin/main." in checkout.instructions()
    assert git(w["caller"], "rev-parse", "HEAD") == own


def test_explicit_base_wins_even_when_caller_is_only_behind(world):
    checkout = prepare(world, base_commit=world["old"])
    assert checkout.base_commit == world["old"]
    assert git(checkout.path, "rev-parse", "HEAD") == world["old"]
    assert "Explicit base_commit." in checkout.instructions()
    assert git(world["caller"], "rev-parse", "origin/main") == world["fresh"]


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_or_timed_out_fetch_falls_back(world, monkeypatch, tmp_path, caplog, timeout):
    w = world
    if timeout:
        slow = tmp_path / "slow.py"
        slow.write_text("import time; time.sleep(0.5)\n")
        argv = [sys.executable, str(slow)]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        git(w["base"], "config", "remote.origin.uploadpack", command)
        monkeypatch.setattr(git_fork_base, "FETCH_TIMEOUT", 0.05)
    else:
        git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    checkout = prepare(w)
    assert checkout.base_commit == w["old"] == git(checkout.path, "rev-parse", "HEAD")
    assert "Caller HEAD; fetch-failed fallback." in checkout.instructions()
    if timeout:
        assert "Git command timed out" in caplog.text


@pytest.mark.parametrize("explicit_branch", [False, True])
def test_new_chat_fetches_before_forking(world, explicit_branch):
    w = world
    kwargs = {"base_branch": "main"} if explicit_branch else {}
    path = checkout_manager.ensure_checkout(w["project"], "chore/chat-fresh", create_new_branch=True,
                                            provision_runtime=False, **kwargs)
    assert path and git(path, "rev-parse", "HEAD") == w["fresh"]
    assert git(w["base"], "rev-parse", "HEAD") == w["old"]


def test_new_chat_fetch_failure_keeps_cached_base(world, tmp_path):
    w = world
    git(w["base"], "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    path = checkout_manager.ensure_checkout(w["project"], "chore/chat-offline", base_branch="main",
                                            create_new_branch=True, provision_runtime=False)
    assert path and git(path, "rev-parse", "HEAD") == w["old"]


def test_remote_default_fetch_failure_falls_back_after_task_branch_fetch(world, monkeypatch):
    w = world
    state = worker_checkout.publish_worker_branch(w["row"], "remote-offline", "coder", fresh_base=True)
    assert state.pushed, state.reason
    # Task publication remains fetchable, but the default branch is unavailable.
    git(w["origin"], "update-ref", "-d", "refs/heads/main")
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _: TARGET))
    received = worker_checkout.receive_worker_branch(state.to_wire(), "remote-offline")
    assert received.checkout.base_commit == w["old"] == git(received.cwd, "rev-parse", "HEAD")
    assert "Caller HEAD; fetch-failed fallback." in received.checkout.instructions()


@pytest.mark.parametrize("new_peer", [False, True])
@pytest.mark.parametrize("own_commit", [False, True])
def test_remote_receiver_fetches_only_for_negotiated_policy(world, monkeypatch, new_peer, own_commit):
    w = world
    caller = commit(w["caller"], "own.txt", "caller work") if own_commit else w["old"]
    state = worker_checkout.publish_worker_branch(w["row"], "remote-fresh", "coder", fresh_base=new_peer)
    assert state.pushed, state.reason
    wire = state.to_wire()
    assert ("base_policy" in wire) == new_peer
    # Advance origin AFTER publication: the receiving worker must do its own fetch.
    fresh = commit(w["seed"], "later.txt", "later main")
    git(w["seed"], "push", "origin", "main")
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda _: TARGET))
    received = worker_checkout.receive_worker_branch(wire, "remote-fresh")
    expected = fresh if new_peer and not own_commit else caller
    assert received.checkout.base_commit == expected == git(received.cwd, "rev-parse", "HEAD")
    if new_peer:
        assert git(w["target"], "rev-parse", "origin/main") == fresh
        note = "Caller HEAD; 2 commits behind origin/main." if own_commit else "Fresh origin/main."
        assert note in received.checkout.instructions()
    else:
        assert git(w["target"], "rev-parse", "origin/main") == w["old"]
    assert git(w["target"], "rev-parse", "HEAD") == w["old"]
    assert git(w["caller"], "rev-parse", "HEAD") == caller
