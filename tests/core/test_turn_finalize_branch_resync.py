"""Turn end settles which branch a session is actually on.

``sessions.branch`` was written once, at intake, and never again. A session
whose agent ran a plain ``git checkout -b`` inside its worktree therefore kept
naming a branch it had left, and the sidebar kept resolving the old branch's
merged PR — which is how the session shipping that very fix desynced its own
row.

Real tmp git repos and real ``git worktree add`` (no git mocking), plus the
pg_shim SQLite DB for the rows, matching tests/core/test_checkout_materialization.py.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.core import turn_finalize
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import database
from app.db import projects_v2

RUNNER = "runnerA"


@pytest.fixture(autouse=True)
def pinned_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: RUNNER)
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _base_repo(tmp_path) -> Path:
    repo = Path(tmp_path) / "base_repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def scene(mock_db, tmp_path):
    """A project, its base checkout, a branch worktree, and a session on it."""
    repo = _base_repo(tmp_path)
    project = projects_v2.upsert_project("git:repo", name="Repo")
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))

    worktree = tmp_path / "wt-old"
    _git(repo, "worktree", "add", str(worktree), "-b", "fix/update-session-git-state")
    checkouts_db.upsert_checkout(
        project["id"],
        RUNNER,
        str(worktree),
        branch="fix/update-session-git-state",
        status="active",
    )

    sid = str(uuid.uuid4())
    database.create_session(
        session_id=sid, profile_id="default", project_v2_id=project["id"]
    )
    database.set_session_branch(sid, "fix/update-session-git-state", project["id"])
    return {
        "session_id": sid,
        "project": project,
        "repo": repo,
        "worktree": worktree,
    }


def test_a_checkout_b_in_the_worktree_moves_the_session_row(scene):
    """The audited symptom, end to end."""
    _git(scene["worktree"], "checkout", "-b", "fix/sidebar-second-pr-state")

    moved = turn_finalize.resync_session_branch_from_head(scene["session_id"])

    assert moved == ("fix/update-session-git-state", "fix/sidebar-second-pr-state")
    assert (
        database.get_session(scene["session_id"])["branch"]
        == "fix/sidebar-second-pr-state"
    )


def test_the_checkout_row_moves_with_it(scene):
    """Otherwise the next resolution materializes a second worktree."""
    _git(scene["worktree"], "checkout", "-b", "fix/sidebar-second-pr-state")

    turn_finalize.resync_session_branch_from_head(scene["session_id"])

    row = checkouts_db.get_checkout(
        scene["project"]["id"], RUNNER, branch="fix/sidebar-second-pr-state"
    )
    assert row is not None
    assert row["path"] == str(scene["worktree"])
    assert (
        checkouts_db.get_checkout(
            scene["project"]["id"], RUNNER, branch="fix/update-session-git-state"
        )
        is None
    )


def test_the_session_still_resolves_to_the_same_directory(scene):
    """The resync must not move the agent's cwd out from under it."""
    from app.core.project_resolver import resolve_existing_cwd

    _git(scene["worktree"], "checkout", "-b", "fix/sidebar-second-pr-state")
    turn_finalize.resync_session_branch_from_head(scene["session_id"])

    session = database.get_session(scene["session_id"])
    assert resolve_existing_cwd(session) == str(scene["worktree"])


def test_pr_reconciliation_would_now_look_up_the_new_branch(scene):
    """``reconcile_session_pr`` reads exactly this expression off the row."""
    _git(scene["worktree"], "checkout", "-b", "fix/sidebar-second-pr-state")
    turn_finalize.resync_session_branch_from_head(scene["session_id"])

    session = database.get_session(scene["session_id"])
    branch = session.get("worktree_branch") or session.get("branch")
    assert branch == "fix/sidebar-second-pr-state"


def test_an_unchanged_branch_writes_nothing(scene, monkeypatch):
    def fail(*a, **kw):  # pragma: no cover - asserts it is never reached
        raise AssertionError("resync wrote on an unchanged branch")

    monkeypatch.setattr(database, "resync_session_branch", fail)

    assert turn_finalize.resync_session_branch_from_head(scene["session_id"]) is None


def test_a_detached_head_records_nothing(scene):
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(scene["worktree"]),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(scene["worktree"], "checkout", "--detach", sha)

    assert turn_finalize.resync_session_branch_from_head(scene["session_id"]) is None
    assert (
        database.get_session(scene["session_id"])["branch"]
        == "fix/update-session-git-state"
    )


def test_a_base_checkout_session_is_never_resynced(mock_db, tmp_path):
    """A base checkout has no branch of its own; adopting one breaks its cwd."""
    repo = _base_repo(tmp_path)
    project = projects_v2.upsert_project("git:base", name="Base")
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))

    sid = str(uuid.uuid4())
    database.create_session(
        session_id=sid, profile_id="default", project_v2_id=project["id"]
    )
    _git(repo, "checkout", "-b", "scratch/agent-branch")

    assert turn_finalize.resync_session_branch_from_head(sid) is None
    assert not (database.get_session(sid)["branch"] or "")
    base = checkouts_db.get_checkout(project["id"], RUNNER, branch=None)
    assert base is not None and base["path"] == str(repo)


def test_a_vanished_checkout_directory_records_nothing(scene):
    import shutil

    _git(scene["repo"], "worktree", "remove", str(scene["worktree"]), "--force")
    shutil.rmtree(scene["worktree"], ignore_errors=True)

    assert turn_finalize.resync_session_branch_from_head(scene["session_id"]) is None


def test_a_name_another_checkout_already_claims_is_left_alone(scene, tmp_path):
    """Two checkouts on one branch would make the resolver's answer arbitrary."""
    other = tmp_path / "wt-other"
    other.mkdir()
    checkouts_db.upsert_checkout(
        scene["project"]["id"], RUNNER, str(other), branch="feat/taken"
    )
    _git(scene["worktree"], "checkout", "-b", "feat/taken")

    assert turn_finalize.resync_session_branch_from_head(scene["session_id"]) is None
    assert (
        database.get_session(scene["session_id"])["branch"]
        == "fix/update-session-git-state"
    )


def test_an_unknown_session_records_nothing(mock_db):
    assert turn_finalize.resync_session_branch_from_head("no-such-session") is None


@pytest.mark.asyncio
async def test_turn_end_runs_the_resync_before_the_pr_reconcile(monkeypatch):
    """Ordering matters: the reconcile looks the PR up by the resynced branch."""
    order = []

    monkeypatch.setattr(
        turn_finalize,
        "resync_session_branch_from_head",
        lambda sid: order.append("resync") or None,
    )

    import app.core.session_pr_state as pr_state

    async def fake_reconcile(session_id):
        order.append("reconcile")
        return True

    monkeypatch.setattr(pr_state, "reconcile_session_pr", fake_reconcile)
    monkeypatch.setattr(
        "app.db.database.set_session_parked_task_kind", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "app.core.pg_session_store.flush_summary_folds", lambda *a, **kw: None
    )

    await turn_finalize.persist_turn_end_state("s-1")
    # The reconcile is fire-and-forget; let its task run.
    import asyncio

    await asyncio.sleep(0)

    assert order[0] == "resync"
    assert "reconcile" in order


@pytest.mark.asyncio
async def test_a_failing_resync_never_breaks_turn_teardown(monkeypatch):
    def boom(_sid):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(turn_finalize, "resync_session_branch_from_head", boom)

    import app.core.session_pr_state as pr_state

    async def noop_reconcile(session_id):
        return True

    monkeypatch.setattr(pr_state, "reconcile_session_pr", noop_reconcile)
    flushed = []
    monkeypatch.setattr(
        "app.db.database.set_session_parked_task_kind", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "app.core.pg_session_store.flush_summary_folds",
        lambda sid: flushed.append(sid),
    )

    await turn_finalize.persist_turn_end_state("s-2")

    assert flushed == ["s-2"]
