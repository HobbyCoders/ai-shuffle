"""Automatic cleanup clears finished worktrees and branches; nothing unproven goes.

Real temporary Git repositories throughout: squash merges, cherry-picks that
main later edited, reviewer checkouts left on PR commits, regenerable build
output beside owner content, leftover local branches, abandoned worker scratch.
"""
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import worktree_report as wr
from tests.core.test_worktree_reclaim import age_directory, checkout, commit, git, reclaim, rc, repository  # noqa: F401

OLD = datetime.fromtimestamp(time.time() - 8 * 24 * 3600, timezone.utc).isoformat()


def branches(**kwargs):
    return asyncio.run(rc.reclaim_branches("p", **kwargs))


def merge_squash(repo, branch):
    git(repo, "merge", "--squash", branch)
    git(repo, "commit", "-qm", f"squash {branch}")


def edit_on_main(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"main edits {name}")


def squash_then_edit(repo, path, name="shared.txt"):
    """A branch whose single change main squash-merged and then edited again."""
    commit(path, name, "branch version\n")
    merge_squash(repo, git(path, "branch", "--show-current"))
    edit_on_main(repo, name, "main changed it later\n")


def content_commit(repo, name="pr.txt", text="a change only this commit has\n"):
    """A commit on top of main that changes content, made without touching any checkout."""
    env = {**os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "fixture-index")}

    def run(*args, **kwargs):
        return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True,
                              text=True, **kwargs).stdout.strip()

    run("read-tree", "HEAD")
    blob = run("hash-object", "-w", "--stdin", input=text)
    run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    tree = run("write-tree")
    os.remove(env["GIT_INDEX_FILE"])
    return run("commit-tree", tree, "-p", "HEAD", "-m", f"add {name}")


def pushed_commit(repo, ref="refs/remotes/origin/review-pr"):
    sha = content_commit(repo)
    git(repo, "update-ref", ref, sha)
    return sha


def exclude(repo, *patterns):
    path = repo / ".git" / "info" / "exclude"
    path.write_text(path.read_text() + "\n" + "\n".join(patterns) + "\n")


# --- 2. One integration proof ---------------------------------------------------


def test_proof_names_how_a_branch_was_integrated(repository):
    repo, _ = repository
    path = checkout(repository)
    commit(path)
    tip = git(path, "rev-parse", "HEAD")
    assert wr.integration_proof(str(repo), "main", tip) is None
    merge_squash(repo, "feature")
    assert git(repo, "rev-list", "--count", "main..feature") == "1"
    assert wr.integration_proof(str(repo), "main", tip, label="main").startswith("squash-merged")
    other = checkout(repository, "later")
    squash_then_edit(repo, other)
    later = git(other, "rev-parse", "HEAD")
    # merge-tree no longer reproduces main (main edited the same line), but
    # every patch is already there.
    assert wr._git(str(repo), "merge-tree", "--write-tree", "main", later) is None  # a conflict
    assert wr.integration_proof(str(repo), "main", later, label="main") == "every commit's change is already in main"
    assert wr.integration_proof(str(repo), "main", git(repo, "rev-parse", "main~1"), label="main") == "contained in main"


def test_proof_from_an_exact_merged_pr_or_a_commit_github_lists_in_one(repository):
    repo, _ = repository
    path = checkout(repository)
    commit(path, "unique.txt", "never merged locally\n")
    tip = git(path, "rev-parse", "HEAD")
    merged_head = SimpleNamespace(number=7, state="MERGED", head_sha=tip, base_branch="main")
    contains = SimpleNamespace(number=8, state="MERGED", head_sha="0" * 40, contains_commit=tip, base_branch="main")
    still_open = SimpleNamespace(number=9, state="OPEN", head_sha=tip, base_branch="main")

    def proof(pulls, base="main"):
        return wr.integration_proof(str(repo), "main", tip, pulls, base_branch=base)

    assert proof([merged_head]) == "PR #7 merged at this commit"
    assert proof([contains]) == "PR #8 merged with this commit in it"
    assert proof([still_open]) is None
    other = SimpleNamespace(number=10, state="MERGED", head_sha="1" * 40, contains_commit="2" * 40, base_branch="main")
    assert proof([other]) is None
    # Merged into some other branch is not integrated; without a base no PR counts.
    stacked = SimpleNamespace(number=11, state="MERGED", head_sha=tip, base_branch="feat/stack")
    assert proof([stacked]) is None
    assert proof([merged_head], base=None) is None


def test_squash_merged_then_edited_worktree_is_reclaimed(repository):
    repo, _ = repository
    path = checkout(repository)
    squash_then_edit(repo, path)
    assert wr._git(str(repo), "merge-tree", "--write-tree", "main", "feature") is None
    preview = reclaim()["items"][0]
    assert preview["decision"] == "remove" and preview["proof"] == "every commit's change is already in main"
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


def test_report_never_calls_an_integrated_branch_unpushed_work():
    item = {"path": "/wt", "branch": "fix/a", "locked": False}
    row = wr.assess(item, [], 0, 3, "squash-merged (merging it changes nothing in origin/main)")
    assert row["reclaimable"] is True
    assert row["unpushed_commits"] is None
    assert "unpushed" not in row["reason"] and "not merged" not in row["reason"]
    assert "integrated: squash-merged" in row["reason"]
    unproven = wr.assess(item, [], 0, 3, False)
    assert "3 unpushed commit(s)" in unproven["reason"] and unproven["reclaimable"] is False
    pushed = wr.assess(item, [], 0, 3, False, pushed=True)
    assert "3 commit(s) not in the default branch, all pushed" in pushed["reason"]
    assert "unpushed" not in pushed["reason"] and pushed["reclaimable"] is False


# --- 1. Disposable ignored files ------------------------------------------------


def merged_with(repository, files):
    repo, _ = repository
    path = checkout(repository)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
        git(path, "add", name)
    git(path, "commit", "-qm", "tracked files")
    git(repo, "merge", "-q", "--ff-only", "feature")
    return repo, path


@pytest.mark.parametrize("ignored", ["frontend/build/app.js", "mobile/dist/bundle.js", ".ruff_cache/0.1/x",
                                     ".mypy_cache/3.12/x.json", "frontend/.eslintcache"])
def test_regenerable_output_and_tool_caches_do_not_keep_a_checkout(repository, ignored):
    repo, path = merged_with(repository, {"frontend/package.json": "{}", "mobile/package.json": "{}"})
    exclude(repo, "build/", "dist/", ".ruff_cache/", ".mypy_cache/", ".eslintcache")
    (path / ignored).parent.mkdir(parents=True, exist_ok=True)
    (path / ignored).write_text("regenerated by every UI check")
    assert rc._ignored_reason(str(path)) is None
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


def test_self_ignoring_pytest_cache_listed_file_by_file_is_disposable(repository):
    repo, path = merged_with(repository, {"README.md": "x"})
    cache = path / "agent" / ".pytest_cache"
    (cache / "v" / "cache").mkdir(parents=True)
    (cache / ".gitignore").write_text("# created by pytest\n*\n")
    (cache / "README.md").write_text("pytest cache")
    (cache / "v" / "cache" / "nodeids").write_text("[]")
    listed = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")
    assert "agent/.pytest_cache/README.md" in listed  # not collapsed into one directory
    assert rc._ignored_reason(str(path)) is None


def test_build_dir_without_package_json_and_owner_content_keep_the_checkout_by_name(repository):
    repo, path = merged_with(repository, {"README.md": "x"})
    exclude(repo, "build/", "uploads/", "mockups/")
    for name in ("build/out.bin", "uploads/a.png", "uploads/b.pdf", "mockups/one.html"):
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("owner content")
    reason = rc._ignored_reason(str(path))
    assert reason == "holds ignored files: build/ (1 file), mockups/ (1 file), uploads/ (2 files)"
    preview = reclaim()["items"][0]
    assert preview["decision"] == "keep" and preview["reason"] == reason
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert (path / "uploads" / "a.png").read_text() == "owner content"


def test_package_json_link_does_not_make_build_disposable(repository, tmp_path):
    repo, path = merged_with(repository, {"README.md": "x"})
    exclude(repo, "build/", "package.json")
    (tmp_path / "elsewhere.json").write_text("{}")
    (path / "package.json").symlink_to(tmp_path / "elsewhere.json")
    (path / "build").mkdir()
    (path / "build" / "out.js").write_text("x")
    assert "build/ (1 file)" in rc._ignored_reason(str(path))


# --- 3. Stranded reviewer checkouts ---------------------------------------------


@pytest.mark.parametrize("move", ["detach", "pr-branch"])
def test_reviewer_checkout_on_a_pushed_commit_is_reclaimed(repository, move):
    repo, source = repository
    path = checkout(repository, "chore/task-reviewer")
    own = git(repo, "rev-parse", "chore/task-reviewer")
    pr = pushed_commit(repo)
    git(path, "checkout", "-q", *(["--detach", pr] if move == "detach" else ["-b", "pr702b", pr]))
    age_directory(path)
    preview = reclaim()["items"][0]
    assert preview["decision"] == "remove", preview["reason"]
    assert preview["head_sha"] == pr and preview["branch_sha"] == own
    assert "reviewer" in preview["reason"] and "pushed commit" in preview["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()
    assert not git(repo, "branch", "--list", "chore/task-reviewer")
    assert git(repo, "rev-parse", "refs/remotes/origin/review-pr") == pr


@pytest.mark.parametrize("problem", ["local-commit", "own-branch-touched", "dirty", "not-a-worker"])
def test_reviewer_checkout_is_kept_when_anything_local_is_at_stake(repository, problem):
    repo, _ = repository
    name = "feature" if problem == "not-a-worker" else "chore/task-reviewer"
    path = checkout(repository, name)
    if problem == "own-branch-touched":
        commit(path, "own.txt", "worker's own commit")
    if problem == "local-commit":
        target = content_commit(repo, "local.txt", "exists only in this repository\n")
    else:
        target = pushed_commit(repo)
    git(path, "checkout", "-q", "--detach", target)
    if problem == "dirty":
        (path / "notes.txt").write_text("review notes")
    age_directory(path)
    row = reclaim()["items"][0]
    assert row["decision"] == "keep"
    expected = "uncommitted changes (1 file)" if problem == "dirty" else "not its registered branch"
    assert expected in row["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 0 and path.exists()
    assert git(repo, "rev-parse", name)


def test_reviewer_head_github_lists_in_a_pr_counts_as_pushed(repository, monkeypatch):
    from app.core.github_service import github_service

    repo, _ = repository
    path = checkout(repository, "chore/task-reviewer")
    pr = content_commit(repo)  # its remote branch was already pruned
    git(path, "checkout", "-q", "--detach", pr)
    age_directory(path)
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **k: [])
    monkeypatch.setattr(github_service, "list_commit_pulls", lambda repo, sha: [SimpleNamespace(
        number=702, state="OPEN", head_sha=sha, contains_commit=sha)])
    row = reclaim()["items"][0]
    assert row["decision"] == "remove" and "PR #702 on GitHub" in row["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 1


# --- 4. Local branches with no worktree ----------------------------------------


def test_orphan_branches_go_only_when_proven(repository):
    repo, _ = repository
    git(repo, "branch", "pr670")  # at main: contained
    git(repo, "branch", "chore/task-wip", content_commit(repo, "wip.txt"))
    git(repo, "branch", "experiment")  # hand-made, at main: never a candidate
    git(repo, "branch", "fix/old-pr")  # hand-made, at main: never a candidate
    path = checkout(repository, "chore/task-squashed")
    squash_then_edit(repo, path)
    git(repo, "worktree", "remove", str(path))  # disposable fixture: leaves the branch behind
    repository[1]["registered"].pop()  # ... and its registration was already retired
    checkout(repository, "chore/task-in-use")
    rows = {r["branch"]: r for r in branches()["items"]}
    assert set(rows) == {"pr670", "chore/task-wip", "chore/task-squashed"}  # never main or a checked-out branch
    assert rows["pr670"]["decision"] == "delete"
    squashed = rows["chore/task-squashed"]
    assert squashed["decision"] == "delete" and "every commit's change" in squashed["reason"]
    wip = rows["chore/task-wip"]
    assert wip["decision"] == "keep" and wip["reason"] == "unmerged and not pushed; integration not proven"
    result = branches(dry_run=False)
    assert result["deleted"] == 2
    listed = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert sorted(listed) == ["chore/task-in-use", "chore/task-wip", "experiment", "fix/old-pr", "main"]


@pytest.mark.parametrize("name,app_made", [("chore/task-1", True), ("chore/chat-ab12", True),
                                           ("task/0b9f2c1e-1234-4abc-8def-0123456789ab", True),
                                           ("pr702", True), ("pr702b", True), ("pr-702", True),
                                           ("experiment", False), ("main2", False), ("pr702/x", False),
                                           ("fix/pr702", False), ("chore/task-", False), ("prx", False),
                                           ("task/foo", False), ("task/1", False), ("chore/task-a/b", False)])
def test_branch_sweep_only_considers_app_and_gh_names(repository, name, app_made):
    repo, _ = repository
    git(repo, "branch", name)  # at main: integrated either way
    rows = branches()["items"]
    assert [r["branch"] for r in rows] == ([name] if app_made else [])
    branches(dry_run=False, observations=None)
    assert bool(git(repo, "branch", "--list", name)) is (not app_made)
    if not app_made:
        source = repository[1]
        context = rc._context(source)
        row = rc._plan_branch(source, context, name, git(repo, "rev-parse", name), pulls=[])
        assert row["decision"] == "keep" and row["reason"] == "not a branch the app or gh pr checkout created"


def test_pushed_branch_needs_a_finished_pr(repository, monkeypatch):
    from app.core.github_service import github_service

    repo, _ = repository
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    tip = pushed_commit(repo, "refs/remotes/origin/feat/x")
    git(repo, "branch", "pr113", tip)
    state = {"pulls": []}
    monkeypatch.setattr(github_service, "list_pulls", lambda repo, state_="all", head=None, limit=100, **k: [
        p for p in state["pulls"] if p.head_branch == head])
    monkeypatch.setattr(github_service, "list_commit_pulls", lambda *a, **k: [])
    row = branches()["items"][0]
    assert row["decision"] == "keep" and row["reason"].startswith("pushed, but no merged or closed PR")
    state["pulls"] = [SimpleNamespace(number=113, state="OPEN", head_branch="feat/x", head_sha=tip)]
    assert branches()["items"][0]["reason"].startswith("open PR")
    # A finished PR whose head is another commit: the remote ref may be stale.
    state["pulls"] = [SimpleNamespace(number=113, state="CLOSED", head_branch="feat/x", head_sha="3" * 40)]
    assert branches()["items"][0]["reason"].startswith("pushed, but no merged or closed PR")
    state["pulls"] = [SimpleNamespace(number=113, state="CLOSED", head_branch="feat/x", head_sha=tip)]
    row = branches()["items"][0]
    assert row["decision"] == "delete" and row["reason"] == "tip is pushed (origin/feat/x); PR #113 closed"
    assert branches(dry_run=False)["deleted"] == 1
    assert git(repo, "rev-parse", "refs/remotes/origin/feat/x") == tip


@pytest.mark.parametrize("session", [{"id": "s", "branch": "chore/task-done", "is_active": True},
                                     {"id": "s", "worktree_branch": "chore/task-done", "running": True},
                                     {"id": "s", "branch": "chore/task-done", "pr_state": "open", "pr_number": 3,
                                      "updated_at": OLD, "finished_at": OLD}])
def test_orphan_branch_bound_to_live_session_or_open_pr_is_kept(repository, monkeypatch, session):
    repo, _ = repository
    git(repo, "branch", "chore/task-done")
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [session])
    assert branches(dry_run=False)["deleted"] == 0
    assert git(repo, "rev-parse", "chore/task-done")


def test_branch_of_a_detached_registered_checkout_is_left_to_the_worktree_sweep(repository):
    repo, _ = repository
    path = checkout(repository, "chore/task-reviewer")
    git(path, "checkout", "-q", "--detach", content_commit(repo))
    row = branches()["items"][0]
    assert row["branch"] == "chore/task-reviewer" and row["reason"] == "bound to a registered checkout"


def test_branch_delete_is_compare_and_delete(repository, monkeypatch):
    repo, _ = repository
    git(repo, "branch", "chore/task-done")
    moved = content_commit(repo, "late.txt", "new work after the check\n")
    assert rc._delete_branch(str(repo), "chore/task-done", moved) is False  # wrong expected tip: refused
    assert git(repo, "rev-parse", "chore/task-done") == git(repo, "rev-parse", "main")
    original = rc._prepare_branch

    def race(source, name, preview):
        result = original(source, name, preview)
        git(repo, "update-ref", "refs/heads/chore/task-done", moved)
        return result

    monkeypatch.setattr(rc, "_prepare_branch", race)
    calls = []
    real = rc.git_service._run_git

    def record(cwd, args, **kwargs):
        if args[:2] == ["update-ref", "-d"]:
            calls.append(args)
        return real(cwd, args, **kwargs)

    monkeypatch.setattr(rc.git_service, "_run_git", record)
    row = branches(dry_run=False)["items"][0]
    assert row["applied"] is False and git(repo, "rev-parse", "chore/task-done") == moved
    assert all(len(args) == 4 for args in calls)  # every delete names the expected SHA


def test_automatic_branch_grace(repository, monkeypatch):
    repo, _ = repository
    git(repo, "branch", "chore/task-done")
    observations = {}
    now = time.time()
    monkeypatch.setattr(rc.time, "time", lambda: now)
    first = branches(dry_run=False, observations=observations)
    assert first["deleted"] == 0 and "grace" in first["items"][0]["reason"]
    now += rc.GRACE_SECONDS + 1
    assert branches(dry_run=False, observations=observations)["deleted"] == 1


# --- 8. Abandoned worker scratch: archive, never delete --------------------------


def abandoned(repository, monkeypatch, name="chore/task-w2", worker_age=OLD, parent_age=OLD, **session):
    repo, _ = repository
    path = checkout(repository, name)
    commit(path, "tracked.txt", "committed but never merged\n")
    head = git(path, "rev-parse", "HEAD")
    (path / "tracked.txt").write_text("edited after the commit\n")
    (path / "notes.txt").write_text("untracked findings\n")
    age_directory(path)
    worker = {"id": name.rsplit("-", 1)[-1] if name.startswith("chore/chat-") else name.removeprefix("chore/task-"),
              "branch": name, "updated_at": worker_age, "finished_at": worker_age,
              "parent_session_id": "parent", **session}
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [worker])
    monkeypatch.setattr(rc, "_session_activity", lambda sid: {"id": sid, "updated_at": parent_age,
                                                              "finished_at": parent_age, "running": False})
    return repo, path, head


def test_abandoned_worker_checkout_is_snapshotted_then_removed(repository, monkeypatch):
    repo, path, head = abandoned(repository, monkeypatch)
    original = rc.git_service.remove_worktree
    forced = []

    def remove(main, target, force=False):
        archived = rc.wr._git(str(repo), "rev-parse", "--verify", "--quiet", "refs/fluid-archive/chore/task-w2")
        forced.append((force, bool(archived)))
        return original(main, target, force)

    monkeypatch.setattr(rc.git_service, "remove_worktree", remove)
    preview = reclaim()["items"][0]
    assert preview["decision"] == "archive"
    assert "uncommitted changes (2 files)" in preview["reason"] and "refs/fluid-archive/chore/task-w2" in preview["reason"]
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 1, result["items"][0]["reason"]
    assert forced == [(True, True)]  # forced only once the snapshot ref existed
    assert not path.exists() and not git(repo, "branch", "--list", "chore/task-w2")
    snapshot = git(repo, "rev-parse", "refs/fluid-archive/chore/task-w2")
    assert git(repo, "rev-parse", snapshot + "^") == head
    assert git(repo, "show", f"{snapshot}:tracked.txt") == "edited after the commit"
    assert git(repo, "show", f"{snapshot}:notes.txt") == "untracked findings"
    assert result["items"][0]["snapshot_sha"] == snapshot
    assert git(repo, "branch", "--list") == "* main"  # the archive is not a branch
    stamped = rc._stamp_head.call_args.args[1]
    assert stamped["head_sha"] == snapshot  # resume restores the snapshot


@pytest.mark.parametrize("case,expected", [
    ("worker-recent", "not archived: worker last active 3 day(s) ago"),
    ("parent-recent", "not archived: parent session last active 1 day(s) ago"),
    ("chat", "uncommitted changes (2 files)"),
    ("open-pr", "open PR head branch"),
    ("active", "bound to an active or running session"),
])
def test_checkouts_in_use_or_not_worker_scratch_are_never_archived(repository, monkeypatch, case, expected):
    recent = lambda days: datetime.fromtimestamp(time.time() - days * 86400 - 60, timezone.utc).isoformat()  # noqa: E731
    kwargs = {
        "worker-recent": {"worker_age": recent(3)},
        "parent-recent": {"parent_age": recent(1)},
        "chat": {"name": "chore/chat-abcdef12"},
        "open-pr": {"pr_state": "open", "pr_number": 5},
        "active": {"is_active": True},
    }[case]
    repo, path, _ = abandoned(repository, monkeypatch, **kwargs)
    remove = Mock(wraps=rc.git_service.remove_worktree)
    monkeypatch.setattr(rc.git_service, "remove_worktree", remove)
    row = reclaim(dry_run=False)["items"][0]
    assert row["decision"] == "keep" and expected in row["reason"]
    remove.assert_not_called()
    assert (path / "notes.txt").read_text() == "untracked findings\n"
    assert not rc.wr._git(str(repo), "for-each-ref", "refs/fluid-archive")


def test_unverified_snapshot_never_forces_removal(repository, monkeypatch):
    repo, path, _ = abandoned(repository, monkeypatch)
    real = rc._worktree_tree
    calls = []

    def changed(path_, head):
        calls.append(1)
        return real(path_, head) if len(calls) == 1 else "0" * 40  # files changed after the snapshot

    monkeypatch.setattr(rc, "_worktree_tree", changed)
    remove = Mock(wraps=rc.git_service.remove_worktree)
    monkeypatch.setattr(rc.git_service, "remove_worktree", remove)
    row = reclaim(dry_run=False)["items"][0]
    assert row["decision"] == "keep" and row.get("error")
    remove.assert_not_called()
    rc._stamp_head.assert_not_called()  # an unverified snapshot is never recorded as the tip
    assert (path / "notes.txt").exists() and git(repo, "rev-parse", "chore/task-w2")
    assert not rc.wr._git(str(repo), "for-each-ref", "refs/fluid-archive")  # its ref was dropped


def test_archive_removal_severs_links_without_touching_targets(repository, monkeypatch, tmp_path):
    repo, path, _ = abandoned(repository, monkeypatch)
    target = tmp_path / "shared-runtime"
    target.mkdir()
    (target / "valuable").write_text("keep")
    (path / "node_modules").symlink_to(target, target_is_directory=True)
    exclude(repo, "node_modules")
    age_directory(path)
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert (target / "valuable").read_text() == "keep"


@pytest.mark.parametrize("scenario", ["dirty-feature", "integrated", "reviewer"])
def test_force_is_never_used_outside_the_verified_snapshot_path(repository, monkeypatch, scenario):
    repo, _ = repository
    if scenario == "dirty-feature":
        path = checkout(repository)
        (path / "draft.txt").write_text("work")
    elif scenario == "integrated":
        path = checkout(repository)
    else:
        path = checkout(repository, "chore/task-reviewer")
        git(path, "checkout", "-q", "--detach", pushed_commit(repo))
    age_directory(path)
    calls = []
    original = rc.git_service.remove_worktree
    monkeypatch.setattr(rc.git_service, "remove_worktree",
                        lambda main, target, force=False: calls.append(force) or original(main, target, force))
    reclaim(dry_run=False)
    assert True not in calls
    assert path.exists() is (scenario == "dirty-feature")


# --- 6. Wrong base checkout ------------------------------------------------------


def test_base_row_choice_prefers_the_real_checkout(tmp_path, monkeypatch):
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    real = init_repo(tmp_path / "real")
    missing = {"id": "a", "path": str(tmp_path / "checkouts" / "__base__"), "branch": None,
               "status": "active", "created_at": "2026-01-01"}
    present = {"id": "b", "path": str(real), "branch": None, "status": "active", "created_at": "2026-02-01"}
    branch = {"id": "c", "path": str(real / ".fluid" / "worktrees" / "x"), "branch": "x"}
    assert wr.choose_base_checkout([missing, present, branch])["id"] == "b"
    assert wr.choose_base_checkout([present, missing])["id"] == "b"
    plain = tmp_path / "plain"
    plain.mkdir()
    other = init_repo(tmp_path / "other")
    rows = [{"id": "d", "path": str(other), "branch": None, "created_at": "2026-03-01"}, present, branch]
    assert wr.choose_base_checkout(rows)["id"] == "b"  # holds the registered worktrees
    assert wr.choose_base_checkout([{**present, "id": "e"}, {**present, "id": "f"}])["id"] == "e"  # deterministic


def test_project_with_a_stale_base_row_is_swept(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, projects_v2
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "toolkit")
    project = projects_v2.upsert_project("local:two-bases", name="Two bases")
    runner = rc.settings.effective_instance_id
    checkouts.upsert_checkout(project["id"], runner, str(tmp_path / "checkouts" / "toolkit" / "__base__"))
    checkouts.upsert_checkout(project["id"], runner, str(repo))
    path = repo / ".fluid" / "worktrees" / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(path))
    checkouts.upsert_checkout(project["id"], runner, str(path), branch="feature")
    source = next(s for s in wr._project_repos() if project["id"] in s["aliases"])
    assert source["path"] == str(repo.resolve())
    result = asyncio.run(rc.reclaim(project["id"]))
    assert result["total"] == 1 and result["items"][0]["path"] == str(path.resolve())


# --- 5. Report: no ghosts, small, and the sweeper's own verdict ------------------


def test_report_folds_history_drops_retired_rows_and_shows_the_sweeper_verdict(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, projects_v2
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rc.settings, "data_dir", tmp_path / "state")
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    project = projects_v2.upsert_project("local:report-ghosts", name="Ghosts")
    runner = rc.settings.effective_instance_id
    checkouts.upsert_checkout(project["id"], runner, str(repo))
    live = repo / ".fluid" / "worktrees" / "squashed"
    git(repo, "worktree", "add", "-q", "-b", "fix/squashed", str(live))
    commit(live, "feature.txt", "work\n")
    merge_squash(repo, "fix/squashed")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    age_directory(live)
    checkouts.upsert_checkout(project["id"], runner, str(live), branch="fix/squashed")
    for index in range(5):  # 5 retired (archived) rows and 3 ghosts with no folder or branch
        archived = repo / ".fluid" / "worktrees" / f"old-{index}"
        checkouts.upsert_checkout(project["id"], runner, str(archived), branch=f"old-{index}")
        checkouts.archive_checkout_by_path(project["id"], str(archived), runner_id=runner)
    for index in range(3):
        checkouts.upsert_checkout(project["id"], runner, str(repo / ".fluid" / "worktrees" / f"ghost-{index}"),
                                  branch=f"ghost-{index}")
    git(repo, "update-ref", "refs/fluid-archive/chore/task-gone", "HEAD")

    result = wr.report(project["id"])
    project_row = result["projects"][0]
    assert result["total"] == 1 and project_row["history_folded"] == 3
    row = project_row["worktrees"][0]
    assert row["merged_into_default"].startswith("squash-merged")
    assert "unpushed" not in row["reason"] and "not merged" not in row["reason"]
    assert row["reclaimable"] is True
    assert row["cleanup"].startswith("removed automatically after the 24h grace")
    preview = asyncio.run(rc.reclaim(project["id"]))
    planned = next(i for i in preview["items"] if i["path"] == str(live.resolve()))
    assert planned["decision"] == "remove"  # the report's verdict is the sweeper's
    assert project_row["archived"]["count"] == 1
    assert "refs/fluid-archive/<branch>" in project_row["archived"]["restore"]
    assert "registry_ids" not in row and "state" not in row  # compact default
    assert wr.report(project["id"], include_retired=True)["total"] == 9


def test_report_stays_small(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, projects_v2
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rc.settings, "data_dir", tmp_path / "state")
    repo = init_repo(tmp_path / "repo")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    project = projects_v2.upsert_project("local:report-size", name="Size")
    runner = rc.settings.effective_instance_id
    checkouts.upsert_checkout(project["id"], runner, str(repo))
    for index in range(12):
        path = repo / ".fluid" / "worktrees" / f"chore-task-{index:02d}"
        git(repo, "worktree", "add", "-q", "-b", f"chore/task-{index:02d}", str(path))
        checkouts.upsert_checkout(project["id"], runner, str(path), branch=f"chore/task-{index:02d}")
    for index in range(200):  # history the old report listed row by row
        ghost = repo / ".fluid" / "worktrees" / f"gone-{index}"
        checkouts.upsert_checkout(project["id"], runner, str(ghost), branch=f"gone-{index}")
        if index % 2:
            checkouts.archive_checkout_by_path(project["id"], str(ghost), runner_id=runner)
    result = wr.report(project["id"])
    assert result["total"] == 12 and result["projects"][0]["history_folded"] == 100
    assert len(json.dumps(result)) < 12 * 400 + 1000


# --- 7. Cadence ------------------------------------------------------------------


def test_every_pass_visits_every_project(repository, tmp_path, monkeypatch):
    from tests.git_repo_helpers import init_repo

    repo, source = repository
    first = checkout(repository)
    other = init_repo(tmp_path / "second")
    second_source = {"project_id": "q", "project": "second", "path": str(other), "aliases": ["q"], "registered": []}
    path = other / ".fluid" / "worktrees" / "done"
    git(other, "worktree", "add", "-q", "-b", "done", str(path))
    age_directory(path)
    second_source["registered"].append({"path": str(path), "branch": "done", "registry_ids": ["ck-done"]})
    broken = {"project_id": "x", "project": "broken", "path": str(tmp_path / "broken"), "aliases": ["x"],
              "registered": []}
    monkeypatch.setattr(rc, "_local_sources", lambda: [broken, source, second_source])
    monkeypatch.setattr(rc.settings, "data_dir", tmp_path / "state")
    now = time.time()
    monkeypatch.setattr(rc.time, "time", lambda: now)
    asyncio.run(rc.automatic_pass())
    assert first.exists() and path.exists()  # both observed in ONE pass, inside the grace
    state = json.loads((tmp_path / "state" / "checkout-reclaim.json").read_text())
    assert {rc.wr._path_key(str(first)), rc.wr._path_key(str(path))} <= set(state["observations"])
    now += rc.GRACE_SECONDS + 1
    asyncio.run(rc.automatic_pass())
    assert not first.exists() and not path.exists()  # one broken project starved neither
    assert rc.INTERVAL_SECONDS == 2 * 3600



# --- Review regressions -----------------------------------------------------------


def test_partial_forced_removal_keeps_the_archive_ref(repository, monkeypatch):
    """Once forced removal starts, the snapshot may be the only copy: keep its ref."""
    repo, path, head = abandoned(repository, monkeypatch)

    def partial(main, target, force=False):
        (path / "notes.txt").unlink()  # forced removal got this far ...
        return False  # ... then failed (busy file, timeout)

    monkeypatch.setattr(rc.git_service, "remove_worktree", partial)
    row = reclaim(dry_run=False)["items"][0]
    assert row["decision"] == "keep" and row.get("error")
    snapshot = git(repo, "rev-parse", "refs/fluid-archive/chore/task-w2")
    assert git(repo, "show", f"{snapshot}:notes.txt") == "untracked findings"
    assert rc._stamp_head.call_args.args[1]["head_sha"] == snapshot  # stamped only after verification
    # The next pass refuses to overwrite the surviving snapshot.
    again = reclaim(dry_run=False)["items"][0]
    assert again["decision"] == "keep"
    assert git(repo, "rev-parse", "refs/fluid-archive/chore/task-w2") == snapshot


def test_nested_repository_is_never_archived(repository, monkeypatch):
    """git add records a nested repository as a gitlink only: archiving would lose it."""
    repo, path, _ = abandoned(repository, monkeypatch)
    nested = path / "vendorlib"
    nested.mkdir()
    git(nested, "init", "-q")
    git(nested, "config", "user.name", "Test")
    git(nested, "config", "user.email", "test@example.invalid")
    (nested / "lib.txt").write_text("local-only nested work\n")
    git(nested, "add", ".")
    git(nested, "commit", "-qm", "nested")
    (nested / "lib.txt").write_text("uncommitted nested edit\n")
    age_directory(path)
    preview = reclaim()["items"][0]
    assert preview["decision"] == "keep"
    assert "not archived: holds a nested Git repository: vendorlib/" in preview["reason"]
    remove = Mock(wraps=rc.git_service.remove_worktree)
    monkeypatch.setattr(rc.git_service, "remove_worktree", remove)
    assert reclaim(dry_run=False)["reclaimed"] == 0
    remove.assert_not_called()
    assert (nested / "lib.txt").read_text() == "uncommitted nested edit\n"
    assert not rc.wr._git(str(repo), "for-each-ref", "refs/fluid-archive")
    # The snapshot step itself refuses a gitlink too (second line of defence).
    with pytest.raises(ValueError, match="nested Git repository: vendorlib"):
        rc._worktree_tree(path, git(path, "rev-parse", "HEAD"))


def test_patch_equivalence_never_proves_a_branch_with_merge_commits(repository):
    """git cherry skips merges, so content added while resolving a merge would pass."""
    repo, _ = repository
    path = checkout(repository)
    commit(path, "a.txt", "feature change\n")
    feature = git(path, "rev-parse", "HEAD")
    edit_on_main(repo, "b.txt", "main moved on\n")
    git(path, "merge", "-q", "--no-ff", "--no-commit", "main")
    (path / "resolution.txt").write_text("only exists in the merge commit\n")
    git(path, "add", "resolution.txt")
    git(path, "commit", "-qm", "merge main with extra content")
    git(repo, "cherry-pick", feature)  # main now holds the feature patch, not the merge content
    tip = git(path, "rev-parse", "HEAD")
    cherry = git(repo, "cherry", "main", tip)
    assert cherry and all(line.startswith("- ") for line in cherry.splitlines())  # cherry alone would "prove" it
    assert wr.integration_proof(str(repo), "main", tip, label="main") is None
    row = reclaim()["items"][0]
    assert row["decision"] == "keep" and "integration not proven" in row["reason"]


def test_reviewer_checkout_with_a_detached_local_commit_in_its_reflog_is_kept(repository):
    repo, _ = repository
    path = checkout(repository, "chore/task-reviewer")
    git(path, "checkout", "-q", "--detach")
    (path / "fixup.txt").write_text("review fixup that was never pushed\n")
    git(path, "add", "fixup.txt")
    git(path, "commit", "-qm", "local-only fixup")
    lost = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "--detach", pushed_commit(repo))
    age_directory(path)
    assert rc._unpublished_history(str(repo), str(path), "main", exempt=(git(path, "rev-parse", "HEAD"),)) == [lost]
    row = reclaim()["items"][0]
    assert row["decision"] == "keep" and "not its registered branch" in row["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 0 and path.exists()
    assert git(repo, "cat-file", "-t", lost) == "commit"


def test_report_cache_serves_repeat_lookups_and_the_sweeper_never_uses_it(monkeypatch):
    from app.core.github_service import github_service

    rc._report_cache.clear()
    calls, timeouts = [], []
    monkeypatch.setattr(github_service, "list_commit_pulls",
                        lambda repo, sha, **k: timeouts.append(k.get("timeout")) or calls.append(sha) or [])
    monkeypatch.setattr(github_service, "list_pulls",
                        lambda *a, **k: timeouts.append(k.get("timeout")) or calls.append(k.get("head")) or [])
    report_context = {"github_repo": "example/repo", "report_cache": True}
    sweeper_context = {"github_repo": "example/repo"}
    for _ in range(3):
        rc._commit_pulls(report_context, "a" * 40)
        rc._pulls(report_context, "chore/task-x")
    assert calls == ["a" * 40, "chore/task-x"]
    rc._commit_pulls(sweeper_context, "a" * 40)
    rc._pulls(sweeper_context, "chore/task-x")
    assert len(calls) == 4
    # The report's lookups give up quickly; the sweeper keeps the service default.
    assert timeouts == [rc.REPORT_GH_TIMEOUT, rc.REPORT_GH_TIMEOUT, None, None]
    now = time.monotonic()
    monkeypatch.setattr(rc.time, "monotonic", lambda: now + rc.REPORT_CACHE_SECONDS + 1)
    rc._commit_pulls(report_context, "a" * 40)
    assert len(calls) == 5  # expired
    rc._report_cache.clear()


def test_report_stops_at_its_deadline(repository, monkeypatch):
    repo, source = repository
    checkout(repository, "one")
    checkout(repository, "two")
    rows = rc.plan_preview(source, deadline=time.monotonic() - 1)
    assert {r["cleanup"] for r in rows.values()} == {rc.NOT_CHECKED}
    assert all(r["decision"] == "keep" for r in rows.values())
    monkeypatch.setattr(wr, "REPORT_SECONDS", 0)
    monkeypatch.setattr(wr, "_project_repos", lambda **k: [source])
    result = wr.report("p")
    worktrees = result["projects"][0]["worktrees"]
    assert len(worktrees) == 2
    assert all(w["reason"].startswith("not checked (time limit)") and w["reclaimable"] is False for w in worktrees)



def test_reviewer_checkout_whose_starting_commit_only_its_branch_holds_is_kept(repository):
    """Reviewer probe (b): a parent's unpublished commit was the worker's base,
    then the parent deleted its branch. Deleting the worker branch would lose it."""
    repo, source = repository
    parent_tip = content_commit(repo, "parent.txt", "parent's unpublished work\n")
    git(repo, "branch", "parent-feat", parent_tip)
    path = repo / ".fluid" / "worktrees" / "chore" / "task-rv"
    git(repo, "worktree", "add", "-q", "-b", "chore/task-rv", str(path), parent_tip)
    source["registered"].append({"path": str(path), "branch": "chore/task-rv", "registry_ids": ["ck-rv"]})
    git(path, "checkout", "-q", "--detach", pushed_commit(repo))
    age_directory(path)
    assert reclaim()["items"][0]["decision"] == "remove"  # while the parent's branch still holds it
    git(repo, "branch", "-D", "parent-feat")  # the parent later deletes its branch
    row = reclaim()["items"][0]
    assert row["decision"] == "keep"
    assert f"its starting commit {parent_tip[:12]} is held only by its own branch chore/task-rv" in row["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert git(repo, "rev-parse", "chore/task-rv") == parent_tip


def test_failed_archive_never_deletes_a_snapshot_an_earlier_pass_left(repository, monkeypatch):
    repo, path, head = abandoned(repository, monkeypatch)
    snapshot = rc._snapshot(path, head, "chore/task-w2")
    git(repo, "update-ref", "refs/fluid-archive/chore/task-w2", snapshot)  # left by an earlier pass
    calls = []
    real = rc.git_service._run_git
    monkeypatch.setattr(rc.git_service, "_run_git",
                        lambda cwd, args, **k: calls.append(args) or real(cwd, args, **k))
    monkeypatch.setattr(rc, "_worktree_tree", lambda *a: "0" * 40)  # verification fails
    row = {"path": str(path), "branch": "chore/task-w2", "head_sha": head}
    monkeypatch.setattr(rc, "_snapshot", lambda *a: snapshot)
    with pytest.raises(ValueError, match="snapshot verification failed"):
        rc._archive_checkout(str(repo), row)
    assert not [args for args in calls if args[:2] == ["update-ref", "-d"]]
    assert git(repo, "rev-parse", "refs/fluid-archive/chore/task-w2") == snapshot


def test_surviving_folder_of_a_stopped_archive_names_the_snapshot(repository, monkeypatch):
    repo, path, _ = abandoned(repository, monkeypatch)
    monkeypatch.setattr(rc.git_service, "remove_worktree", lambda main, target, force=False: False)
    row = reclaim(dry_run=False)["items"][0]
    ref = "refs/fluid-archive/chore/task-w2"
    restore = f"git -C {repo} branch chore/task-w2-restored {ref}"
    assert row["reason"].startswith("archived checkout removal stopped part-way")
    assert ref in row["reason"] and restore in row["reason"]
    again = reclaim()["items"][0]
    assert again["decision"] == "keep" and again["reason"].startswith("archived earlier but the folder remains")
    assert restore in again["reason"]



def test_each_project_gets_a_fair_share_of_the_report_budget(repository, tmp_path, monkeypatch):
    from tests.git_repo_helpers import init_repo

    repo, first = repository
    checkout(repository, "one")
    other = init_repo(tmp_path / "second")
    second = {"project_id": "q", "project": "second", "path": str(other), "aliases": ["q"], "registered": []}
    path = other / ".fluid" / "worktrees" / "two"
    git(other, "worktree", "add", "-q", "-b", "two", str(path))
    second["registered"].append({"path": str(path), "branch": "two", "registry_ids": ["ck-two"]})
    monkeypatch.setattr(rc, "_local_sources", lambda: [first, second])
    monkeypatch.setattr(wr, "_project_repos", lambda **k: [first, second])
    deadlines = []
    real = rc.plan_preview

    def slow_first(source, deadline=None):
        deadlines.append(deadline - time.monotonic())
        result = real(source, deadline)
        if source is first:
            time.sleep(0.6)  # the first project uses most of its share
        return result

    # Real git runs inside plan_preview, so its cost varies by runner; a wide
    # budget keeps the fairness assertions independent of that cost.
    monkeypatch.setattr(rc, "plan_preview", slow_first)
    monkeypatch.setattr(wr, "REPORT_SECONDS", 6)
    result = wr.report()
    assert len(deadlines) == 2
    assert deadlines[0] <= 3.05  # half of the budget, not all of it
    assert deadlines[1] > 1.0  # the second project still has time
    assert not any(w["cleanup"].startswith("kept: not checked") for p in result["projects"] for w in p["worktrees"])
