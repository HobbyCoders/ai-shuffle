"""Task worker branches moving between workers through origin (no network).

Each test builds a local bare repository as "origin" and clones it once per
simulated worker; nothing is pushed anywhere else.
"""
import logging
import subprocess
from pathlib import Path

import pytest

from app.core import checkout_transfer, worker_checkout
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME, pin_runner  # noqa: F401

TARGET = "unraid-test-runner"
SECRET = "s3cr3t-token-value"
TOKEN_URL = f"https://someone:{SECRET}@127.0.0.1:9/owner/repo.git"


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def clone(origin, path):
    git(path.parent, "clone", str(origin), str(path))
    git(path, "config", "user.name", "Remote Test")
    git(path, "config", "user.email", "remote@example.test")
    return path


def commit(path, name, text):
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-m", name)
    return git(path, "rev-parse", "HEAD")


def remote_tip(path, branch):
    out = git(path, "ls-remote", "origin", f"refs/heads/{branch}").split()
    return out[0] if out else None


@pytest.fixture(autouse=True)
def no_credential_prompts(monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = clone(bare, tmp_path / "seed")
    (seed / ".gitignore").write_text(".fluid/\n.env\n")
    git(seed, "add", ".gitignore")
    commit(seed, "code.txt", "on origin\n")
    git(seed, "push", "origin", "HEAD:refs/heads/main")
    return bare


# --- checkout_transfer: Git only ---------------------------------------------

def test_publish_pushes_the_exact_commit_without_force_and_is_retry_safe(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    sha = commit(parent, "local.txt", "only on the parent\n")
    result = checkout_transfer.publish_task_branch(parent, "task/w1", sha)
    assert result["pushed"] is True and result["reason"] is None
    assert result["branch"] == "task/w1" and result["base"] == sha
    assert result["remote"] == checkout_transfer.remote_identity(parent)
    assert remote_tip(parent, "task/w1") == sha
    # The parent's own branch was not pushed and no local task branch exists.
    assert remote_tip(parent, "main") != sha
    assert not git(parent, "branch", "--list", "task/w1")
    # Retrying the same publish is a no-op success.
    again = checkout_transfer.publish_task_branch(parent, "task/w1", sha)
    assert again["pushed"] is True and remote_tip(parent, "task/w1") == sha


def test_publish_of_a_diverged_branch_is_refused_not_forced(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    first = commit(parent, "a.txt", "a\n")
    assert checkout_transfer.publish_task_branch(parent, "task/w1", first)["pushed"]
    git(parent, "reset", "--hard", "HEAD~1")
    diverged = commit(parent, "b.txt", "b\n")
    result = checkout_transfer.publish_task_branch(parent, "task/w1", diverged)
    assert result["pushed"] is False
    assert "another commit" in result["reason"] and "nothing was forced" in result["reason"]
    assert remote_tip(parent, "task/w1") == first


def test_publish_race_with_an_existing_branch_is_rejected_by_origin(origin, tmp_path, monkeypatch):
    # Origin gains the branch between the check and the push: plain push only.
    parent = clone(origin, tmp_path / "parent")
    other = clone(origin, tmp_path / "other")
    theirs = commit(other, "theirs.txt", "theirs\n")
    git(other, "push", "origin", f"{theirs}:refs/heads/task/w1")
    mine = commit(parent, "mine.txt", "mine\n")
    monkeypatch.setattr(checkout_transfer, "_remote_tip", lambda cwd, branch: (None, True))
    result = checkout_transfer.publish_task_branch(parent, "task/w1", mine)
    assert result["pushed"] is False and "changed meanwhile" in result["reason"]
    assert remote_tip(parent, "task/w1") == theirs


def test_publish_without_origin_reports_a_plain_reason(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.test")
    sha = commit(repo, "x.txt", "x\n")
    result = checkout_transfer.publish_task_branch(repo, "task/w1", sha)
    assert result["pushed"] is False and result["remote"] is None
    assert "no usable origin remote" in result["reason"]


def test_publish_refuses_non_task_branches(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    sha = git(parent, "rev-parse", "HEAD")
    for branch in ("main", "feature/x", "task/../main"):
        with pytest.raises(ValueError, match="Only task/"):
            checkout_transfer.publish_task_branch(parent, branch, sha)
    assert remote_tip(parent, "main") == sha


def test_fetch_verifies_the_sha_and_detects_a_moved_tip(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    target = clone(origin, tmp_path / "target")
    sha = commit(parent, "local.txt", "parent\n")
    remote = checkout_transfer.publish_task_branch(parent, "task/w1", sha)["remote"]
    assert checkout_transfer.fetch_task_branch(target, "task/w1", sha, remote) == sha
    assert git(target, "rev-parse", "refs/remotes/origin/task/w1") == sha
    # Someone pushes onto the task branch: the target refuses the old SHA.
    git(parent, "checkout", "-b", "task/w1")
    moved = commit(parent, "more.txt", "more\n")
    git(parent, "push", "origin", "task/w1")
    with pytest.raises(ValueError, match="not at the commit the calling worker sent"):
        checkout_transfer.fetch_task_branch(target, "task/w1", sha, remote)
    assert checkout_transfer.fetch_task_branch(target, "task/w1", moved, remote) == moved


def test_fetch_refuses_a_different_origin(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    target = clone(origin, tmp_path / "target")
    sha = git(parent, "rev-parse", "HEAD")
    remote = checkout_transfer.publish_task_branch(parent, "task/w1", sha)["remote"]
    git(target, "remote", "set-url", "origin", "https://github.com/someone-else/repo.git")
    with pytest.raises(ValueError, match="different repository"):
        checkout_transfer.fetch_task_branch(target, "task/w1", sha, remote)
    assert "task/w1" not in git(target, "branch", "-a")


def test_compare_and_delete_removes_only_an_untouched_branch(origin, tmp_path):
    parent = clone(origin, tmp_path / "parent")
    other = clone(origin, tmp_path / "other")
    base = git(parent, "rev-parse", "HEAD")
    assert checkout_transfer.publish_task_branch(parent, "task/clean", base)["pushed"]
    assert checkout_transfer.publish_task_branch(parent, "task/worked", base)["pushed"]
    git(other, "fetch", "origin")
    git(other, "checkout", "-b", "task/worked", "origin/task/worked")
    work = commit(other, "work.txt", "a worker commit\n")
    git(other, "push", "origin", "task/worked")

    assert checkout_transfer.delete_untouched_task_branch(parent, "task/clean", base) == {
        "deleted": True, "reason": None}
    assert remote_tip(parent, "task/clean") is None
    kept = checkout_transfer.delete_untouched_task_branch(parent, "task/worked", base)
    assert kept["deleted"] is False and "left alone" in kept["reason"]
    assert remote_tip(parent, "task/worked") == work
    gone = checkout_transfer.delete_untouched_task_branch(parent, "task/clean", base)
    assert gone["deleted"] is False and "nothing to delete" in gone["reason"]
    with pytest.raises(ValueError, match="Only task/"):
        checkout_transfer.delete_untouched_task_branch(parent, "main", base)


def test_compare_and_delete_is_enforced_by_origin_not_just_the_precheck(origin, tmp_path, monkeypatch):
    # A commit lands after the tip check: the lease makes origin refuse.
    parent = clone(origin, tmp_path / "parent")
    base = git(parent, "rev-parse", "HEAD")
    assert checkout_transfer.publish_task_branch(parent, "task/w1", base)["pushed"]
    git(parent, "checkout", "-b", "task/w1")
    work = commit(parent, "late.txt", "late commit\n")
    git(parent, "push", "origin", "task/w1")
    monkeypatch.setattr(checkout_transfer, "_remote_tip", lambda cwd, branch: (base, True))
    result = checkout_transfer.delete_untouched_task_branch(parent, "task/w1", base)
    assert result["deleted"] is False and "left in place" in result["reason"]
    assert remote_tip(parent, "task/w1") == work


def test_errors_never_carry_credentials(origin, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    repo = clone(origin, tmp_path / "tokened")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "set-url", "origin", TOKEN_URL)
    published = checkout_transfer.publish_task_branch(repo, "task/w1", sha)
    assert published["pushed"] is False
    remote = published["remote"]
    assert remote and SECRET not in remote
    with pytest.raises(ValueError) as fetched:
        checkout_transfer.fetch_task_branch(repo, "task/w1", sha, remote)
    deleted = checkout_transfer.delete_untouched_task_branch(repo, "task/w1", sha)
    with pytest.raises(ValueError) as mismatched:
        checkout_transfer.fetch_task_branch(repo, "task/w1", sha, "0" * 64)
    texts = [str(published), str(fetched.value), str(deleted), str(mismatched.value), caplog.text]
    for text in texts:
        assert SECRET not in text and "127.0.0.1" not in text and "someone" not in text


# --- worker_checkout: parent publishes, target receives ----------------------

def use_runner(monkeypatch, runner):
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: runner))


@pytest.fixture
def workers(mock_db, origin, tmp_path, monkeypatch):
    """A project checked out on the parent (LOCAL_ME) and on TARGET."""
    parent = clone(origin, tmp_path / "parent")
    target = clone(origin, tmp_path / "target")
    tip = commit(parent, "parent.txt", "committed on the parent only\n")
    (parent / "dirty.txt").write_text("uncommitted parent work\n")
    project = projects_v2.upsert_project("local:remote-worker-test", name="Remote repo")
    checkouts.upsert_checkout(project["id"], LOCAL_ME, str(parent))
    checkouts.upsert_checkout(project["id"], TARGET, str(target))
    database.create_profile("coder", "Coder", None, {"allowed_tools": ["Read", "Edit", "Bash"]})
    database.create_profile("research", "Research", None, {"allowed_tools": ["Read", "Grep", "Glob"]})
    database.create_session("parent", "coder", project_v2_id=project["id"])
    return database.get_session("parent"), parent, target, tip, project


def test_parent_publish_then_target_receive_matches_the_parent_head(workers, monkeypatch):
    row, parent, target, tip, project = workers
    state = worker_checkout.publish_worker_branch(row, "remote-a", "coder")
    assert state.mode == worker_checkout.REMOTE_BRANCH and state.pushed, state.reason
    assert (state.branch, state.base_commit, state.parent_branch) == ("task/remote-a", tip, "main")
    wire = state.to_wire()
    assert str(parent) not in str(wire) and str(target) not in str(wire)
    assert remote_tip(parent, "task/remote-a") == tip

    use_runner(monkeypatch, TARGET)
    received = worker_checkout.receive_worker_branch(wire, "remote-a")
    assert received.mode == worker_checkout.REMOTE_BRANCH
    path = Path(received.cwd)
    assert received.checkout.path == str(path) and received.checkout.base_commit == tip
    assert received.checkout.parent_branch == "main"
    assert path != target and path.is_dir()
    assert git(path, "rev-parse", "HEAD") == tip
    assert git(path, "branch", "--show-current") == "task/remote-a"
    assert (path / "parent.txt").read_text() == "committed on the parent only\n"
    assert not (path / "dirty.txt").exists()
    assert git(target, "branch", "--show-current") == "main"
    # The receiving checkout is an ordinary worker checkout: reclaimable when untouched.
    assert worker_checkout.discard_unused_worker_checkout(received.checkout)
    assert not path.exists() and not git(target, "branch", "--list", "task/remote-a")

    use_runner(monkeypatch, LOCAL_ME)
    assert worker_checkout.release_worker_branch(wire) == {"deleted": True, "reason": None}
    assert remote_tip(parent, "task/remote-a") is None


def test_target_refuses_a_sha_mismatch_and_leaves_nothing_behind(workers, monkeypatch):
    row, parent, target, tip, _ = workers
    wire = worker_checkout.publish_worker_branch(row, "remote-b", "coder").to_wire()
    wire["base_commit"] = git(parent, "rev-parse", "HEAD~1")
    use_runner(monkeypatch, TARGET)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="not at the commit"):
        worker_checkout.receive_worker_branch(wire, "remote-b")
    assert not git(target, "branch", "--list", "task/remote-b")
    assert "task/remote-b" not in git(target, "worktree", "list")


def test_target_refuses_a_different_origin_by_name(workers, monkeypatch):
    row, _, target, *_ = workers
    wire = worker_checkout.publish_worker_branch(row, "remote-c", "coder").to_wire()
    git(target, "remote", "set-url", "origin", "https://github.com/someone-else/repo.git")
    use_runner(monkeypatch, TARGET)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="different repository"):
        worker_checkout.receive_worker_branch(wire, "remote-c")
    assert not git(target, "branch", "--list", "task/remote-c")


def test_target_without_the_project_refuses_by_name(workers, monkeypatch):
    row, *_ = workers
    wire = worker_checkout.publish_worker_branch(row, "remote-d", "coder").to_wire()
    use_runner(monkeypatch, "worker-without-the-project")
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="not checked out on this worker"):
        worker_checkout.receive_worker_branch(wire, "remote-d")


def test_target_refuses_a_branch_for_another_worker_or_an_unpublished_one(workers, monkeypatch):
    row, *_ = workers
    wire = worker_checkout.publish_worker_branch(row, "remote-e", "coder").to_wire()
    use_runner(monkeypatch, TARGET)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="does not match this worker"):
        worker_checkout.receive_worker_branch(wire, "someone-else")
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="did not publish"):
        worker_checkout.receive_worker_branch({**wire, "pushed": False}, "remote-e")
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="invalid"):
        worker_checkout.receive_worker_branch({**wire, "mode": "path"}, "remote-e")


def test_touched_remote_branch_survives_release(workers, tmp_path):
    row, parent, *_ = workers
    state = worker_checkout.publish_worker_branch(row, "remote-f", "coder")
    other = clone(tmp_path / "origin.git", tmp_path / "other")
    git(other, "checkout", "-b", "task/remote-f", "origin/task/remote-f")
    work = commit(other, "child.txt", "the child's commit\n")
    git(other, "push", "origin", "task/remote-f")
    result = worker_checkout.release_worker_branch(state)
    assert result["deleted"] is False and "left alone" in result["reason"]
    assert remote_tip(parent, "task/remote-f") == work


def test_failed_publish_returns_a_reason_and_the_target_refuses(workers, monkeypatch):
    row, parent, target, *_ = workers
    git(parent, "remote", "set-url", "origin", TOKEN_URL)
    state = worker_checkout.publish_worker_branch(row, "remote-g", "coder")
    assert state.pushed is False and state.reason and SECRET not in state.reason
    assert SECRET not in str(state.to_wire())
    use_runner(monkeypatch, TARGET)
    with pytest.raises(worker_checkout.WorkerCheckoutError):
        worker_checkout.receive_worker_branch(state.to_wire(), "remote-g")
    assert worker_checkout.release_worker_branch(state)["deleted"] is False


def test_research_surface_shares_the_target_base_checkout_without_a_push(workers, monkeypatch):
    row, parent, target, *_ = workers
    state = worker_checkout.publish_worker_branch(row, "reader", "research")
    assert state.mode == worker_checkout.REMOTE_SHARED and not state.pushed
    assert remote_tip(parent, "task/reader") is None
    use_runner(monkeypatch, TARGET)
    received = worker_checkout.receive_worker_branch(state.to_wire(), "reader")
    assert received.mode == worker_checkout.REMOTE_SHARED and received.cwd == str(target)
    assert received.checkout is None


def test_no_project_or_non_git_means_nothing_to_transfer(workers, monkeypatch, tmp_path):
    from app.core import project_resolver

    row, *_ = workers
    database.create_session("loose", "coder")
    loose = worker_checkout.publish_worker_branch(database.get_session("loose"), "loose-w", "coder")
    assert loose.mode == worker_checkout.REMOTE_NONE
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    folder = worker_checkout.publish_worker_branch(row, "folder-w", "coder")
    assert folder.mode == worker_checkout.REMOTE_NONE
    use_runner(monkeypatch, "worker-without-the-project")
    received = worker_checkout.receive_worker_branch(folder.to_wire(), "folder-w")
    assert received.mode == worker_checkout.REMOTE_NONE and received.cwd is None
