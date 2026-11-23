"""Every fork of a project chat gets its own worktree, and can go to another worker.

Two chats in one folder switch and commit over each other, and a fork moved
to another worker would push to its parent's branch from a second checkout.
A fork now gets a ``chore/fork-<id>`` worktree made from the parent's commit,
with the parent's uncommitted changes carried over, and a fork made for
another worker is moved there the way "Move to another worker" moves a chat.
"""
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from psycopg2.extras import Json

from app.core.config import settings
from app.db import checkouts, database, projects_v2
from app.db import sessions as sessions_db

PARENT_SID = "sess-fork-checkout-parent"
PARENT_SDK = "ses_forkcheckout01"
TARGET = "dev-target-0001"


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _msg(role, text):
    return {"role": role, "content": [{"kind": "text", "text": text}], "provider_meta": {}}


@pytest.fixture
def fork_client(mock_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_auth
    from app.api.sessions_fork import fork_router

    application = FastAPI()
    application.include_router(fork_router)
    application.dependency_overrides[require_auth] = lambda: "t"
    with TestClient(application) as c:
        yield c


@pytest.fixture
def repo(mock_db, tmp_path):
    """A parent chat on its own branch worktree, with a fluid transcript."""
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-b", "main")
    git(base, "config", "user.name", "Fork Test")
    git(base, "config", "user.email", "fork@example.test")
    (base / ".gitignore").write_text(".env\n")
    (base / "code.txt").write_text("main\n")
    git(base, "add", ".gitignore", "code.txt")
    git(base, "commit", "-m", "main")
    parent = tmp_path / "parent"
    git(base, "worktree", "add", "-b", "parent-branch", str(parent))
    (parent / "code.txt").write_text("parent commit\n")
    git(parent, "commit", "-am", "parent only")
    tip = git(parent, "rev-parse", "HEAD")
    project = projects_v2.upsert_project("local:fork-checkout-test", name="Fork repo")
    runner = settings.effective_instance_id
    checkouts.upsert_checkout(project["id"], runner, str(base))
    checkouts.upsert_checkout(project["id"], runner, str(parent), branch="parent-branch")
    sessions_db.create_session(PARENT_SID, "default", title="parent", model="sonnet",
                               project_v2_id=project["id"])
    sessions_db.set_session_branch(PARENT_SID, "parent-branch", project["id"])
    sessions_db.update_session(PARENT_SID, sdk_session_id=PARENT_SDK)
    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cur = get_cursor(conn)
        cur.execute("INSERT INTO fluid_sessions (session_id, head_node_id, created_at) VALUES (%s,%s,%s)",
                    (PARENT_SDK, "n2", 1000.0))
        for i, (nid, pid, m) in enumerate([("n1", None, _msg("user", "hello")),
                                           ("n2", "n1", _msg("assistant", "hi"))]):
            cur.execute("INSERT INTO fluid_session_nodes (session_id,node_id,parent_id,ts,message) "
                        "VALUES (%s,%s,%s,%s,%s)", (PARENT_SDK, nid, pid, 1000.0 + i, Json(m)))
    return parent, base, tip


def _fork_branches(base):
    return git(base, "branch", "--list", "chore/fork-*")


def test_fork_gets_its_own_worktree_with_the_parents_uncommitted_work(fork_client, repo):
    parent, base, tip = repo
    (parent / "code.txt").write_text("uncommitted edit\n")
    (parent / "notes.txt").write_text("untracked file\n")
    (parent / ".env").write_text("ignored, never copied\n")
    before = git(parent, "status", "--porcelain")

    resp = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = sessions_db.get_session(body["session_id"])

    assert body["branch"].startswith("chore/fork-") and row["branch"] == body["branch"]
    assert row["worktree_branch"] == body["branch"] and row["worktree_id"] is None
    fork_path = Path(row["worktree_path"])
    assert fork_path != parent and fork_path.is_dir()
    assert git(fork_path, "branch", "--show-current") == body["branch"]
    assert git(fork_path, "rev-parse", "HEAD") == tip
    assert (fork_path / "code.txt").read_text() == "uncommitted edit\n"
    assert (fork_path / "notes.txt").read_text() == "untracked file\n"
    assert not (fork_path / ".env").exists()
    # The parent's folder, branch and changes are untouched.
    assert git(parent, "status", "--porcelain") == before
    assert git(parent, "branch", "--show-current") == "parent-branch"
    from app.core import project_resolver

    assert project_resolver.resolve_existing_cwd(row) == str(fork_path)


def test_carry_keeps_links_as_links_and_the_staged_copy(fork_client, repo):
    parent, _base, _tip = repo
    (parent / ".env").write_text("SECRET=1\n")
    (parent / "dev-config").symlink_to(".env")
    # Staged edit, then the working copy put back to the commit's text.
    (parent / "code.txt").write_text("staged edit\n")
    git(parent, "add", "code.txt")
    (parent / "code.txt").write_text("parent commit\n")

    resp = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert resp.status_code == 200, resp.text
    fork_path = Path(sessions_db.get_session(resp.json()["session_id"])["worktree_path"])

    link = fork_path / "dev-config"
    assert link.is_symlink() and str(link.readlink()) == ".env"
    assert not (fork_path / ".env").exists()
    assert git(fork_path, "show", ":code.txt") == "staged edit"
    assert (fork_path / "code.txt").read_text() == "parent commit\n"


def test_fork_here_gets_its_own_worktree_from_this_workers_checkout(repo):
    """The offline-owner copy never runs in the project's base checkout."""
    from app.core import project_resolver
    from app.network import control

    parent, base, _tip = repo
    result = control.fork_here(PARENT_SID)
    row = sessions_db.get_session(result["session_id"])

    assert row["worktree_branch"].startswith("chore/fork-")
    fork_path = Path(row["worktree_path"])
    assert fork_path.is_dir() and fork_path not in (parent, base)
    assert git(fork_path, "branch", "--show-current") == row["worktree_branch"]
    assert git(fork_path, "rev-parse", "HEAD") == git(base, "rev-parse", "HEAD")
    assert project_resolver.resolve_existing_cwd(row) == str(fork_path)


def test_fork_here_starts_at_the_local_commit_not_a_newer_remote_one(repo, tmp_path):
    from app.network import control

    _parent, base, _tip = repo
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(base), str(origin))
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    git(other, "config", "user.name", "Fork Test")
    git(other, "config", "user.email", "fork@example.test")
    (other / "code.txt").write_text("newer on origin\n")
    git(other, "commit", "-am", "ahead")
    git(other, "push", "origin", "main")
    git(base, "remote", "add", "origin", str(origin))
    git(base, "fetch", "origin")
    local_head = git(base, "rev-parse", "HEAD")
    assert git(base, "rev-parse", "origin/main") != local_head

    row = sessions_db.get_session(control.fork_here(PARENT_SID)["session_id"])
    assert git(Path(row["worktree_path"]), "rev-parse", "HEAD") == local_head


def test_a_failed_fork_here_removes_its_checkout(repo, monkeypatch):
    from app.network import control

    _parent, base, _tip = repo

    def broken(*_args, **_kwargs):
        raise RuntimeError("row failed")

    monkeypatch.setattr(database, "create_session", broken)
    with pytest.raises(RuntimeError):
        control.fork_here(PARENT_SID)
    assert _fork_branches(base) == ""


def test_a_failed_fork_removes_its_checkout(fork_client, repo, monkeypatch):
    parent, base, _tip = repo
    (parent / "code.txt").write_text("uncommitted edit\n")
    from app.core.provider import fluid_transcript

    def broken(*_args, **_kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(fluid_transcript, "fork_fluid_session", broken)
    resp = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert resp.status_code == 500
    assert _fork_branches(base) == ""
    assert (parent / "code.txt").read_text() == "uncommitted edit\n"


@pytest.fixture
def target_online(monkeypatch):
    from app.core import placement

    real_install = database.get_install
    monkeypatch.setattr(database, "get_install",
                        lambda device: {"device_id": TARGET, "device_name": "Target"} if device == TARGET
                        else real_install(device))
    monkeypatch.setattr(placement, "capable_runners", lambda *_a, **_kw: [TARGET])


def test_fork_to_another_worker_is_moved_there(fork_client, repo, target_online, monkeypatch):
    from app.api import sessions, sessions_fork

    moves = []
    replies = iter([HTTPException(409, "Unknown session"), None])

    async def move(session_id, session, target):
        moves.append((session_id, session["id"], target))
        reply = next(replies)
        if reply is not None:
            raise reply  # Not replicated to the destination yet: asked again.

    monkeypatch.setattr(sessions, "move_session_to", move)
    monkeypatch.setattr(sessions_fork, "_FORK_MOVE_POLL_SECONDS", 0)
    resp = fork_client.post(f"/{PARENT_SID}/fork", json={"target_instance_id": TARGET})
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert resp.json()["move_error"] is None
    assert moves == [(new_id, new_id, TARGET)] * 2


def test_a_refused_move_keeps_the_fork_and_says_why(fork_client, repo, target_online, monkeypatch):
    from app.api import sessions

    async def move(session_id, session, target):
        raise HTTPException(409, "This session's project isn't checked out on Target yet.")

    monkeypatch.setattr(sessions, "move_session_to", move)
    resp = fork_client.post(f"/{PARENT_SID}/fork", json={"target_instance_id": TARGET})
    assert resp.status_code == 200, resp.text
    assert "isn't checked out on Target" in resp.json()["move_error"]
    assert sessions_db.get_session(resp.json()["session_id"]) is not None


def test_an_offline_target_forks_nothing(fork_client, repo, monkeypatch):
    from app.core import placement

    parent, base, _tip = repo
    monkeypatch.setattr(database, "get_install",
                        lambda device: {"device_id": TARGET, "device_name": "Target"})
    monkeypatch.setattr(placement, "capable_runners", lambda *_a, **_kw: [])
    resp = fork_client.post(f"/{PARENT_SID}/fork", json={"target_instance_id": TARGET})
    assert resp.status_code == 409
    assert "Target is offline" in resp.json()["detail"]
    assert _fork_branches(base) == ""
    assert [r for r in sessions_db.get_sessions() if r.get("parent_session_id") == PARENT_SID] == []
