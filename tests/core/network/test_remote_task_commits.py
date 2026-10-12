"""Commits back from a Task child on another worker (S13).

Two in-process workers over the real signed control endpoint: ServerWorker runs the
child, Windows runs the parent chat. Git moves through a local bare repository
in the test's temp directory; nothing is pushed anywhere else and no provider
runs. The child's work is simulated with real commits in its checkout.
"""
import asyncio
import json
import logging
import subprocess
from pathlib import Path

import pytest

from app.core import remote_task, worker_checkout
from app.core.config import settings
from app.db import checkouts, database, projects_v2, remote_tasks
from app.db.base import get_db
from app.network import identity
from tests.core.network.test_remote_task_control import (  # noqa: F401
    finish_child, kicks, network, started_child, wakes,
)

ORIGIN_KEY = "local:remote-commits-test"
SECRET = "s3cr3t-token-value"
TOKEN_URL = f"https://someone:{SECRET}@127.0.0.1:9/owner/repo.git"


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def clone(origin, path):
    git(path.parent, "clone", str(origin), str(path))
    git(path, "config", "user.name", "Remote Test")
    git(path, "config", "user.email", "remote@example.test")
    return path


def commit(path, name, text):
    (Path(path) / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-m", name)
    return git(path, "rev-parse", "HEAD")


def bind_caller(project_id):
    """Put the parent chat in ``project_id`` (None: no project)."""
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET project_v2_id=%s WHERE id='caller'", (project_id,))


def remote_tip(path, branch):
    out = git(path, "ls-remote", "origin", f"refs/heads/{branch}").split()
    return out[0] if out else None


def local_tip(path, branch):
    found = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=path,
                           capture_output=True, text=True)
    return found.stdout.strip() or None


@pytest.fixture(autouse=True)
def no_credential_prompts(monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")


class World:
    def __init__(self, a, b, router, origin, parent, target, project_id):
        self.a, self.b, self.router = a, b, router
        self.origin, self.parent, self.target, self.project_id = origin, parent, target, project_id

    def start(self):
        return started_child(self.a, self.b)

    def child_path(self, child):
        with self.a.active():
            return Path(database.get_session(child)["worktree_path"])

    def deliver(self, child, **kwargs):
        with self.a.active():
            finish_child(child, **kwargs)
            asyncio.run(remote_task.on_child_settled(child))
            asyncio.run(remote_task.deliver_due())
            assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED

    def owe(self, child, **kwargs):
        """Finish the child and store its report (the push happens here), unsent."""
        with self.a.active():
            finish_child(child, **kwargs)
            assert remote_task.owe_if_finished(child)

    def send(self):
        with self.a.active():
            asyncio.run(remote_task.deliver_due())

    def sent_block(self, child):
        with self.a.active():
            return remote_tasks.get_child(child)["report"]["git"]

    def wake(self):
        with self.b.active():
            [wake] = wakes()
            return wake["prompt"]

    def parent_state(self):
        """What the parent's checkout looks like: branch, HEAD, index, files."""
        return (git(self.parent, "branch", "--show-current"), git(self.parent, "rev-parse", "HEAD"),
                git(self.parent, "diff", "--cached", "--name-status"),
                git(self.parent, "status", "--porcelain=v1", "--untracked-files=all"))


@pytest.fixture
def world(workers, monkeypatch, kicks, tmp_path):  # noqa: F811 — the imported fixture
    """The parent chat's project is checked out on both workers, from one origin."""
    a, b, router = network(workers, monkeypatch)
    runners = {a.device_id: "unraid-runner", b.device_id: "windows-runner"}
    monkeypatch.setattr(type(settings), "effective_instance_id",
                        property(lambda self: runners.get(getattr(identity._cached, "device_id", None),
                                                          "unknown-runner")))
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = clone(origin, tmp_path / "seed")
    (seed / ".gitignore").write_text(".fluid/\n")
    git(seed, "add", ".gitignore")
    commit(seed, "code.txt", "on origin\n")
    git(seed, "push", "origin", "HEAD:refs/heads/main")
    parent = clone(origin, tmp_path / "windows-repo")
    target = clone(origin, tmp_path / "unraid-repo")
    commit(parent, "parent.txt", "committed on the parent only\n")
    # The parent has its own uncommitted and staged work, which must survive.
    (parent / "notes.txt").write_text("the owner's unsaved notes\n")
    (parent / "staged.txt").write_text("staged by the owner\n")
    git(parent, "add", "staged.txt")
    with b.active():
        project = projects_v2.upsert_project(ORIGIN_KEY, name="Remote repo")
        checkouts.upsert_checkout(project["id"], "windows-runner", str(parent))
        bind_caller(project["id"])
    with a.active():
        assert projects_v2.upsert_project(ORIGIN_KEY, name="Remote repo")["id"] == project["id"]
        checkouts.upsert_checkout(project["id"], "unraid-runner", str(target))
    return World(a, b, router, origin, parent, target, project["id"])


# --- the normal path ------------------------------------------------------------

def test_commits_are_pushed_there_and_fetched_here_without_touching_the_parent(world):
    before = world.parent_state()
    child = world.start()
    branch = f"task/{child}"
    base = git(world.parent, "rev-parse", "HEAD")
    path = world.child_path(child)
    assert git(path, "rev-parse", "HEAD") == base
    commit(path, "fix.txt", "the fix\n")
    head = commit(path, "test.txt", "the test\n")

    world.deliver(child, text="Fixed it in two commits.")
    block = world.sent_block(child)
    assert block == {"mode": "branch", "branch": branch, "base": base, "head": head, "commits": 2,
                     "pushed": True, "uncommitted_files": 0, "remote": block["remote"], "reason": None}
    assert remote_tip(world.parent, branch) == head
    # The parent can git log the branch, and nothing else in its checkout moved.
    assert local_tip(world.parent, branch) == head
    assert git(world.parent, "log", "--format=%s", branch, "-2").splitlines() == ["test.txt", "fix.txt"]
    assert world.parent_state() == before
    prompt = world.wake()
    assert "Fixed it in two commits." in prompt
    assert f"Git: 2 commits on {branch} (head {head[:12]}), pushed to origin from ServerWorker." in prompt
    assert f"`git log {branch}` shows them" in prompt
    # The child's checkout holds work, so it stays on the target for review.
    assert path.is_dir()


def test_a_child_with_no_commits_pushes_nothing_and_its_branch_is_cleaned_up(world):
    child = world.start()
    branch = f"task/{child}"
    path = world.child_path(child)
    base = git(path, "rev-parse", "HEAD")
    assert remote_tip(world.parent, branch) == base  # published by the parent at dispatch
    world.deliver(child, text="Nothing needed changing.")
    block = world.sent_block(child)
    assert block["commits"] == 0 and block["pushed"] is False and block["reason"] is None
    assert block["head"] == base
    # The untouched checkout is reclaimed there; the untouched branch on
    # origin is removed (compare-and-delete); nothing was fetched here.
    assert not path.exists() and local_tip(world.target, branch) is None
    assert remote_tip(world.parent, branch) is None
    assert local_tip(world.parent, branch) is None
    assert f"Git: it made no commits on {branch}." in world.wake()


def test_uncommitted_changes_are_reported_not_committed(world):
    child = world.start()
    branch = f"task/{child}"
    path = world.child_path(child)
    head = commit(path, "done.txt", "committed\n")
    (path / "half-done.txt").write_text("not committed\n")
    (path / "done.txt").write_text("edited after the commit\n")
    world.deliver(child)
    block = world.sent_block(child)
    assert block["pushed"] is True and block["head"] == head and block["uncommitted_files"] == 2
    assert git(path, "rev-parse", "HEAD") == head  # nothing was committed for the child
    assert "half-done.txt" in git(path, "status", "--porcelain")
    assert remote_tip(world.parent, branch) == head and local_tip(world.parent, branch) == head
    prompt = world.wake()
    assert "1 commit on" in prompt
    assert "uncommitted changes in 2 files in its checkout on ServerWorker; they were not committed or copied" in prompt


def test_uncommitted_changes_without_commits_keep_the_checkout(world):
    child = world.start()
    path = world.child_path(child)
    (path / "draft.txt").write_text("work in progress\n")
    world.deliver(child)
    block = world.sent_block(child)
    assert block["commits"] == 0 and block["uncommitted_files"] == 1 and not block["pushed"]
    assert path.is_dir() and (path / "draft.txt").exists()
    prompt = world.wake()
    assert "it made no commits" in prompt and "uncommitted changes in 1 file" in prompt


# --- failures: the report is delivered every time ------------------------------------

def test_a_refused_push_is_reported_and_nothing_is_fetched(world):
    child = world.start()
    branch = f"task/{child}"
    base = git(world.parent, "rev-parse", "HEAD")
    hook = world.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'pushes are not allowed here' >&2\nexit 1\n")
    hook.chmod(0o755)
    commit(world.child_path(child), "fix.txt", "the fix\n")
    world.deliver(child, text="Done, but see the git note.")
    block = world.sent_block(child)
    assert block["pushed"] is False and block["commits"] == 1
    assert "Origin refused" in block["reason"] and "push rights" in block["reason"]
    assert remote_tip(world.parent, branch) == base  # origin kept what it had
    assert local_tip(world.parent, branch) is None
    prompt = world.wake()
    assert "Done, but see the git note." in prompt
    assert f"1 commit on {branch}" in prompt and "were not pushed and stay in its checkout on ServerWorker" in prompt
    assert "Origin refused" in prompt


def test_a_target_without_an_origin_remote_says_so(world):
    child = world.start()
    commit(world.child_path(child), "fix.txt", "the fix\n")
    git(world.target, "remote", "remove", "origin")
    world.deliver(child)
    block = world.sent_block(child)
    assert block["pushed"] is False and "no usable origin remote" in block["reason"]
    prompt = world.wake()
    assert "were not pushed" in prompt and "no usable origin remote on this worker" in prompt


def test_a_parent_without_an_origin_remote_says_the_fetch_failed(world):
    child = world.start()
    branch = f"task/{child}"
    head = commit(world.child_path(child), "fix.txt", "the fix\n")
    world.owe(child)
    git(world.parent, "remote", "remove", "origin")
    world.send()
    assert world.sent_block(child)["pushed"] is True
    assert local_tip(world.parent, branch) is None
    prompt = world.wake()
    assert f"(head {head[:12]}), pushed to origin from ServerWorker." in prompt
    assert "It was not fetched here: This project has no usable origin remote on this worker" in prompt
    assert f"git fetch origin {branch}" in prompt


def test_a_project_with_no_git_checkout_says_there_is_nothing_to_bring_back(world, tmp_path):
    folder = tmp_path / "plain-folder"
    folder.mkdir()
    with world.b.active():
        checkouts.upsert_checkout(world.project_id, "windows-runner", str(folder), status="archived")
        project = projects_v2.upsert_project("local:plain-folder", name="Plain")
        checkouts.upsert_checkout(project["id"], "windows-runner", str(folder))
        bind_caller(project["id"])
    child = world.start()
    world.deliver(child, text="Read the folder.")
    assert world.sent_block(child)["mode"] == "none"
    assert "did not run in a Git checkout" in world.wake()


def test_a_chat_with_no_project_gets_the_same_note(world):
    with world.b.active():
        bind_caller(None)
    child = world.start()
    world.deliver(child)
    assert world.sent_block(child) == worker_checkout.empty_git_block("none")
    assert "there are no commits to bring back" in world.wake()


def test_a_failed_fetch_still_delivers_the_report_once(world):
    child = world.start()
    branch = f"task/{child}"
    commit(world.child_path(child), "fix.txt", "the fix\n")
    world.owe(child)
    # The branch disappears from origin between the push and the fetch.
    git(world.parent, "push", "origin", f":refs/heads/{branch}")
    world.send()
    with world.a.active():
        assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED
    prompt = world.wake()
    assert f"It was not fetched here: Origin no longer has {branch}" in prompt
    assert local_tip(world.parent, branch) is None


def test_an_unexpected_fetch_error_still_delivers_the_report(world, monkeypatch):
    child = world.start()
    commit(world.child_path(child), "fix.txt", "the fix\n")

    def broken(*args, **kwargs):
        raise RuntimeError(f"boom with {TOKEN_URL}")
    monkeypatch.setattr(worker_checkout, "fetch_worker_commits", broken)
    world.deliver(child, text="Report survives.")
    prompt = world.wake()
    assert "Report survives." in prompt and "failed unexpectedly" in prompt
    assert SECRET not in prompt


def test_an_unexpected_push_error_still_delivers_the_report(world, monkeypatch):
    child = world.start()
    commit(world.child_path(child), "fix.txt", "the fix\n")

    def broken(*args, **kwargs):
        raise RuntimeError(f"boom with {TOKEN_URL}")
    monkeypatch.setattr(worker_checkout, "push_worker_commits", broken)
    world.deliver(child, text="Still reported.")
    block = world.sent_block(child)
    assert block["mode"] == "branch" and block["pushed"] is False
    assert "could not check the subagent's branch" in block["reason"]
    prompt = world.wake()
    assert "Still reported." in prompt and "could not check" in prompt and SECRET not in prompt


# --- branch-name collisions ----------------------------------------------------------

def test_someone_elses_branch_of_the_same_name_on_origin_is_never_forced(world, tmp_path):
    child = world.start()
    branch = f"task/{child}"
    other = clone(world.origin, tmp_path / "someone-else")
    theirs = commit(other, "theirs.txt", "someone else's work\n")
    git(other, "push", "--force", "origin", f"{theirs}:refs/heads/{branch}")
    commit(world.child_path(child), "mine.txt", "the child's work\n")
    world.deliver(child, text="Done.")
    block = world.sent_block(child)
    assert block["pushed"] is False and "Origin already has a different" in block["reason"]
    assert remote_tip(world.parent, branch) == theirs
    assert local_tip(world.parent, branch) is None
    assert "nothing was forced" in world.wake()


def test_a_different_local_branch_of_the_same_name_here_is_left_alone(world):
    child = world.start()
    branch = f"task/{child}"
    # A commit of the owner's own that the child's branch does not contain.
    mine = git(world.parent, "commit-tree", "HEAD^{tree}", "-p", "HEAD~1", "-m", "the owner's own")
    git(world.parent, "branch", branch, mine)
    head = commit(world.child_path(child), "fix.txt", "the fix\n")
    world.deliver(child)
    assert remote_tip(world.parent, branch) == head
    assert local_tip(world.parent, branch) == mine
    assert git(world.parent, "rev-parse", f"refs/remotes/origin/{branch}") == head
    prompt = world.wake()
    assert f"A different local {branch} already exists here, so it was left alone" in prompt
    assert f"origin/{branch}" in prompt


def test_a_local_branch_behind_the_childs_head_is_fast_forwarded(world):
    child = world.start()
    branch = f"task/{child}"
    git(world.parent, "branch", branch, "HEAD~1")
    head = commit(world.child_path(child), "fix.txt", "the fix\n")
    world.deliver(child)
    assert local_tip(world.parent, branch) == head
    assert "`git log" in world.wake()


def test_a_checked_out_branch_here_is_never_moved(world, tmp_path):
    child = world.start()
    branch = f"task/{child}"
    base = git(world.parent, "rev-parse", "HEAD")
    git(world.parent, "worktree", "add", "-b", branch, str(tmp_path / "review"), base)
    commit(world.child_path(child), "fix.txt", "the fix\n")
    world.deliver(child)
    assert local_tip(world.parent, branch) == base
    assert git(tmp_path / "review", "status", "--porcelain") == ""
    assert "is checked out in a worktree here, so it was left alone" in world.wake()


# --- credentials -------------------------------------------------------------------

def test_credentials_in_a_remote_url_never_reach_the_report_or_logs(world, caplog):
    caplog.set_level(logging.DEBUG)
    child = world.start()
    commit(world.child_path(child), "fix.txt", "the fix\n")
    git(world.target, "remote", "set-url", "origin", TOKEN_URL)
    world.deliver(child, text="Pushed?")
    block = world.sent_block(child)
    assert block["pushed"] is False and "Could not reach origin" in block["reason"]
    with world.a.active():
        stored = json.dumps(remote_tasks.get_child(child)["report"])
    prompt = world.wake()
    for text in (stored, prompt, caplog.text):
        assert SECRET not in text and "someone:" not in text


def test_the_git_block_is_bounded_and_redacted_on_arrival():
    worker = "abc-123"
    raw = {"mode": "branch", "branch": "task/abc-123", "base": "a" * 40, "head": "b" * 40,
           "commits": 10 ** 9, "pushed": True, "uncommitted_files": -4, "remote": "nothex",
           "reason": f"see {TOKEN_URL} " + "x" * 1000, "extra": "dropped"}
    block = worker_checkout.bound_git_block(raw, worker)
    assert set(block) == set(worker_checkout.empty_git_block("branch"))
    assert block["commits"] == 1_000_000 and block["uncommitted_files"] == 0 and block["remote"] is None
    assert SECRET not in block["reason"] and len(block["reason"]) <= worker_checkout.MAX_GIT_REASON_CHARS
    # Another worker's branch, or the default branch, is never taken as this child's.
    for branch in ("task/someone-else", "main"):
        other = worker_checkout.bound_git_block({**raw, "branch": branch}, worker)
        assert other["branch"] is None and other["pushed"] is False and other["head"] is None
    assert worker_checkout.bound_git_block({"mode": "path"}, worker) is None
    assert worker_checkout.bound_git_block(None, worker) is None


def test_an_older_target_without_a_git_block_adds_no_git_note(world):
    child = world.start()
    with world.b.active():
        row = database.get_session("caller")
        assert asyncio.run(remote_task.bring_commits_here(row, child, "ServerWorker", None)) == ""
