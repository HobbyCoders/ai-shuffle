"""GitService write operations against real temporary repositories.

Branch create/delete/checkout, stash, commit, pull, push, discard and the
worktree mixin run the real git CLI in repositories under ``tmp_path``. Remotes
are local bare repositories or a localhost HTTP endpoint that demands
credentials, so nothing touches the network.
"""

import os

import pytest

from app.core import git_commit_ops
from app.core.git_service import GitService
from tests.git_repo_helpers import (
    auth_required_http_remote,
    clone,
    commit_file,
    git,
    hermetic_git_env,
    init_repo,
    make_origin,
    use_backend,
    write,
)


@pytest.fixture(params=["native", "cli"])
def svc(request, monkeypatch, tmp_path):
    hermetic_git_env(monkeypatch, tmp_path)
    if request.param == "native":
        pytest.importorskip("pygit2")
    use_backend(monkeypatch, request.param)
    return GitService()


@pytest.fixture
def repo(tmp_path, svc):
    return init_repo(tmp_path / "repo", {"a.txt": "a\n", "src/app.py": "print(1)\n"})


def _branches(repo):
    return set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


# =============================================================================
# Branches and checkout
# =============================================================================


def test_create_branch_at_start_point(svc, repo):
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "b.txt", "b\n", "second")

    assert svc.create_branch(str(repo), "from-first", start_point=first) is True
    assert svc.create_branch(str(repo), "from-head") is True

    assert git(repo, "rev-parse", "from-first") == first
    assert git(repo, "rev-parse", "from-head") == git(repo, "rev-parse", "HEAD")


def test_create_branch_rejects_duplicates_bad_start_and_option_names(svc, repo):
    assert svc.create_branch(str(repo), "dup") is True
    assert svc.create_branch(str(repo), "dup") is False
    assert svc.create_branch(str(repo), "x", start_point="no-such-ref") is False
    # "--" ends option parsing: a name that looks like a flag is never one.
    assert svc.create_branch(str(repo), "--force") is False
    assert _branches(repo) == {"main", "dup"}


def test_delete_branch_merged_unmerged_and_current(svc, repo):
    git(repo, "branch", "merged")
    git(repo, "checkout", "-q", "-b", "unmerged")
    commit_file(repo, "u.txt", "u\n", "unmerged work")
    git(repo, "checkout", "-q", "main")

    assert svc.delete_branch(str(repo), "merged") is True
    assert svc.delete_branch(str(repo), "unmerged") is False      # -d refuses
    assert "unmerged" in _branches(repo)
    assert svc.delete_branch(str(repo), "unmerged", force=True) is True
    assert svc.delete_branch(str(repo), "main", force=True) is False  # checked out
    assert svc.delete_branch(str(repo), "ghost") is False
    assert _branches(repo) == {"main"}


def test_checkout_branch_commit_missing_and_dash_prefixed_ref(svc, repo):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "other")

    assert svc.checkout(str(repo), "other") is True
    assert svc.get_current_branch(str(repo)) == "other"
    assert svc.checkout(str(repo), first) is True
    assert svc.get_status(str(repo))["is_detached"] is True
    assert svc.checkout(str(repo), "missing") is False
    # A ref starting with "-" is looked up as refs/heads/<ref>, never parsed
    # as an option (``--orphan=x`` would otherwise create an orphan branch).
    assert svc.checkout(str(repo), "--orphan=evil") is False
    assert "evil" not in _branches(repo)


def test_checkout_invalidates_cached_status(svc, repo):
    git(repo, "branch", "other")
    assert svc.get_status(str(repo))["current_branch"] == "main"
    assert svc.checkout(str(repo), "other") is True
    assert svc.get_status(str(repo))["current_branch"] == "other"


# =============================================================================
# Stash, stage, commit
# =============================================================================


def test_stash_push_and_pop(svc, repo):
    assert svc.stash_push(str(repo)) is False               # nothing to stash
    write(repo / "a.txt", "dirty\n")
    assert svc.stash_push(str(repo), "mine") is True
    assert (repo / "a.txt").read_text() == "a\n"
    assert "mine" in git(repo, "stash", "list")
    assert svc.stash_pop(str(repo)) == (True, None)
    assert (repo / "a.txt").read_text() == "dirty\n"
    ok, err = svc.stash_pop(str(repo))
    assert ok is False and err


def test_stage_unstage_and_commit(svc, repo):
    write(repo / "a.txt", "changed\n")
    write(repo / "new.txt", "n\n")

    assert svc.stage_files(str(repo), ["a.txt", "new.txt"]) is True
    assert {s["file"] for s in svc.get_status(str(repo))["staged"]} == {"a.txt", "new.txt"}
    assert svc.unstage_files(str(repo), ["new.txt"]) is True
    status = svc.get_status(str(repo))
    assert [s["file"] for s in status["staged"]] == ["a.txt"]
    assert status["untracked"] == ["new.txt"]
    assert svc.stage_files(str(repo), ["does-not-exist.txt"]) is False

    ok, sha = svc.commit(str(repo), "Change a")
    assert ok is True and sha == git(repo, "rev-parse", "HEAD")[:12]
    assert git(repo, "log", "-1", "--format=%s") == "Change a"
    ok, err = svc.commit(str(repo), "nothing staged")
    assert ok is False and err


# =============================================================================
# Discard (untracked deletion must stay inside the checkout)
# =============================================================================


def test_discard_modified_staged_and_untracked(svc, repo):
    write(repo / "a.txt", "modified\n")
    write(repo / "src/app.py", "staged\n")
    git(repo, "add", "src/app.py")
    write(repo / "junk.txt", "j\n")
    write(repo / "junkdir/x.txt", "x\n")

    # The UI passes back the paths status reported (the CLI lists an untracked
    # directory as "junkdir/", libgit2 lists the files inside it).
    untracked = svc.get_status(str(repo))["untracked"]
    assert set(untracked) in ({"junk.txt", "junkdir/"}, {"junk.txt", "junkdir/x.txt"})

    ok, failed = svc.discard_changes(str(repo), ["a.txt", "src/app.py", *untracked])

    assert (ok, failed) == (True, [])
    assert (repo / "a.txt").read_text() == "a\n"
    assert (repo / "src/app.py").read_text() == "print(1)\n"
    assert not (repo / "junk.txt").exists() and not (repo / "junkdir/x.txt").exists()
    assert svc.get_status(str(repo))["is_clean"] is True


def _assert_discard_cannot_escape(svc, repo, tmp_path):
    """Client-supplied 'untracked' entries never delete the checkout, its .git,
    anything outside it, or the target of an untracked symlink."""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    os.symlink(repo / "src", repo / "src-link")          # untracked link to tracked dir
    victims = [".", ".git", ".git/config", "../outside.txt", str(outside), "src-link"]

    ok, failed = svc.discard_changes(
        str(repo), victims, {v: "untracked" for v in victims}
    )

    assert repo.is_dir() and (repo / ".git" / "config").is_file()
    assert git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    assert outside.read_text() == "keep\n"
    assert (repo / "src" / "app.py").read_text() == "print(1)\n"
    assert not os.path.lexists(repo / "src-link")         # the link itself is gone
    assert ok is False
    assert set(failed) == {".", ".git", ".git/config", "../outside.txt", str(outside)}


def test_discard_refuses_checkout_root_git_dir_and_outside_paths(svc, repo, tmp_path):
    _assert_discard_cannot_escape(svc, repo, tmp_path)


def test_discard_guard_bites_when_disabled(svc, repo, tmp_path, monkeypatch):
    """The pre-fix target resolution deletes the checkout root."""

    def unguarded(working_dir, real_working_dir, rel_path):
        full = os.path.realpath(os.path.join(working_dir, rel_path))
        if full.startswith(real_working_dir + os.sep) or full == real_working_dir:
            return full
        return None

    monkeypatch.setattr(git_commit_ops, "_untracked_discard_target", unguarded)
    with pytest.raises(AssertionError):
        _assert_discard_cannot_escape(svc, repo, tmp_path)


@pytest.mark.parametrize(
    "alias",
    ["GIT~1", "git~1/config", ".git::$INDEX_ALLOCATION", ".git:x/config", ".git.", ".GIT . ."],
)
def test_discard_target_refuses_ntfs_spellings_of_git_dir(tmp_path, alias):
    """NTFS reads each of these as .git (8.3 short name, alternate data stream,
    trailing dots and spaces); git's is_ntfs_dotgit refuses the same set."""
    root = str(tmp_path)
    assert git_commit_ops._untracked_discard_target(root, root, alias) is None


def test_discard_refuses_an_alias_that_resolves_to_the_git_dir(svc, repo, monkeypatch):
    """The .git guard compares resolved identity, not spelling: an untracked
    entry whose real path is the repository's .git (as GetFinalPathNameByHandle
    expands an NTFS short name) is never deleted."""
    alias = repo / "ALIAS"
    write(alias / "HEAD", "keep\n")
    real = os.path.realpath

    def ntfs_realpath(path, *args, **kwargs):
        if os.path.normpath(os.fspath(path)) == str(alias):
            return str(repo / ".git")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", ntfs_realpath)
    ok, failed = svc.discard_changes(str(repo), ["ALIAS"], {"ALIAS": "untracked"})

    assert (ok, failed) == (False, ["ALIAS"])
    assert (alias / "HEAD").read_text() == "keep\n"


def test_discard_removes_an_untracked_junction_as_a_link(svc, repo, monkeypatch):
    """A Windows junction is not islink(); it is removed like a link rather than
    handed to rmtree (which refuses junctions on Python 3.12+)."""
    import shutil

    junction = repo / "junc"
    junction.mkdir()
    rmtree_calls = []

    def refusing_rmtree(path, *args, **kwargs):
        rmtree_calls.append(path)
        raise OSError("Cannot call rmtree on a symbolic link")

    monkeypatch.setattr(
        os.path, "isjunction", lambda p: os.fspath(p) == str(junction), raising=False
    )
    monkeypatch.setattr(shutil, "rmtree", refusing_rmtree)
    ok, failed = svc.discard_changes(str(repo), ["junc"], {"junc": "untracked"})

    assert (ok, failed) == (True, [])
    assert rmtree_calls == []
    assert not os.path.lexists(junction)


def test_discard_autodetects_statuses(svc, repo):
    write(repo / "a.txt", "modified\n")
    write(repo / "loose.txt", "l\n")

    ok, failed = svc.discard_changes(str(repo), ["a.txt", "loose.txt"])

    assert (ok, failed) == (True, [])
    assert svc.get_status(str(repo))["is_clean"] is True


# =============================================================================
# Pull / push / fetch
# =============================================================================


@pytest.fixture
def remote_pair(tmp_path, repo):
    bare = make_origin(tmp_path, repo)
    other = clone(tmp_path, bare, "other")
    return bare, other


def test_fetch_and_pull_without_remote(svc, repo):
    assert svc.fetch(str(repo)) is False
    ok, err, conflicts = svc.pull(str(repo))
    assert (ok, err, conflicts) == (False, "Failed to fetch from 'origin'", [])


def test_pull_on_detached_head_is_refused(svc, repo):
    git(repo, "checkout", "-q", "--detach")
    assert svc.pull(str(repo)) == (False, "Cannot pull: HEAD is detached", [])


@pytest.mark.parametrize("strategy", ["merge", "rebase"])
def test_pull_fast_forward_and_rebase(svc, repo, remote_pair, strategy):
    _, other = remote_pair
    commit_file(other, "remote.txt", "r\n", "remote work")
    git(other, "push", "-q", "origin", "main")
    if strategy == "rebase":
        commit_file(repo, "local.txt", "l\n", "local work")

    ok, err, conflicts = svc.pull(str(repo), strategy=strategy)

    assert (ok, err, conflicts) == (True, None, [])
    assert (repo / "remote.txt").read_text() == "r\n"
    if strategy == "rebase":
        assert git(repo, "log", "-1", "--format=%s") == "local work"
        assert git(repo, "rev-list", "--count", "HEAD") == "3"


def test_pull_conflict_is_aborted_and_reported(svc, repo, remote_pair):
    _, other = remote_pair
    commit_file(other, "a.txt", "theirs\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    commit_file(repo, "a.txt", "ours\n", "local edit")
    before = git(repo, "rev-parse", "HEAD")

    ok, err, conflicts = svc.pull(str(repo))

    assert (ok, err, conflicts) == (False, "Merge conflicts detected", ["a.txt"])
    assert git(repo, "rev-parse", "HEAD") == before
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_pull_auto_stash_keeps_local_edits(svc, repo, remote_pair):
    _, other = remote_pair
    commit_file(other, "remote.txt", "r\n", "remote work")
    git(other, "push", "-q", "origin", "main")
    write(repo / "src/app.py", "print(2)\n")

    ok, err, _ = svc.pull(str(repo), auto_stash=True)

    assert (ok, err) == (True, None)
    assert (repo / "remote.txt").exists()
    assert (repo / "src/app.py").read_text() == "print(2)\n"
    assert git(repo, "stash", "list") == ""


def test_pull_auto_stash_restored_when_fetch_fails(svc, repo):
    write(repo / "src/app.py", "print(2)\n")

    ok, err, _ = svc.pull(str(repo), auto_stash=True)

    assert (ok, err) == (False, "Failed to fetch from 'origin'")
    assert (repo / "src/app.py").read_text() == "print(2)\n"
    assert git(repo, "stash", "list") == ""


def test_push_without_remote_reports_git_error(svc, repo):
    ok, err = svc.push(str(repo), branch="main")
    assert ok is False
    assert "origin" in err


def test_push_set_upstream_rejection_and_force_with_lease(svc, repo, remote_pair):
    bare, other = remote_pair
    git(repo, "checkout", "-q", "-b", "feature")
    commit_file(repo, "f.txt", "f\n", "feature")

    assert svc.push(str(repo), branch="feature", set_upstream=True) == (True, None)
    assert git(repo, "rev-parse", "--abbrev-ref", "feature@{upstream}") == "origin/feature"
    assert git(bare, "rev-parse", "feature") == git(repo, "rev-parse", "HEAD")

    # Someone else pushes to main: our stale main is rejected, not forced.
    commit_file(other, "o.txt", "o\n", "other")
    git(other, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "mine.txt", "m\n", "mine")
    ok, err = svc.push(str(repo), branch="main")
    assert ok is False and "rejected" in err

    # After rewriting our own pushed branch, --force-with-lease succeeds.
    git(repo, "checkout", "-q", "feature")
    git(repo, "commit", "-q", "--amend", "-m", "feature (amended)")
    assert svc.push(str(repo), branch="feature")[0] is False
    assert svc.push(str(repo), branch="feature", force=True) == (True, None)


def test_network_auth_failure_fails_fast(svc, repo):
    with auth_required_http_remote() as url:
        git(repo, "remote", "add", "origin", url)
        assert svc.fetch(str(repo)) is False
        ok, err = svc.push(str(repo), branch="main")
        assert ok is False
        assert "could not read Username" in err or "Authentication failed" in err
        assert svc.pull(str(repo))[:2] == (False, "Failed to fetch from 'origin'")


def test_run_git_disables_terminal_prompts(svc, repo, monkeypatch):
    import subprocess

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    seen = {}

    def fake_run(args, **kwargs):
        seen.clear()
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("app.core.git_service.subprocess.run", fake_run)
    svc._run_git(str(repo), ["fetch", "origin"])
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    svc._run_git(str(repo), ["fetch"], env={"PATH": os.environ["PATH"]})
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    svc._run_git(str(repo), ["fetch"], env={"GIT_TERMINAL_PROMPT": "1"})
    assert seen["GIT_TERMINAL_PROMPT"] == "1"  # an explicit caller value wins


# =============================================================================
# Worktree mixin
# =============================================================================


def test_worktree_add_list_remove_and_prune(svc, tmp_path, repo):
    wt_new = tmp_path / "wt-new"
    wt_existing = tmp_path / "wt-existing"
    wt_detached = tmp_path / "wt-detached"
    git(repo, "branch", "existing")

    assert svc.add_worktree(str(repo), str(wt_new), "fresh", new_branch=True, base_branch="main") == (True, None)
    assert svc.add_worktree(str(repo), str(wt_existing), "existing") == (True, None)
    git(repo, "worktree", "add", "-q", "--detach", str(wt_detached))
    ok, err = svc.add_worktree(str(repo), str(tmp_path / "again"), "existing")
    assert ok is False and "already" in err

    listed = {os.path.realpath(w["path"]): w for w in svc.list_worktrees(str(repo))}
    main = listed[os.path.realpath(repo)]
    assert main["is_main"] is True and main["branch"] == "main"
    assert listed[os.path.realpath(wt_new)]["branch"] == "fresh"
    assert listed[os.path.realpath(wt_existing)]["is_main"] is False
    detached = listed[os.path.realpath(wt_detached)]
    assert detached["is_detached"] is True and detached["branch"] is None
    assert all(w["is_bare"] is False for w in listed.values())

    write(wt_existing / "dirty.txt", "d\n")
    assert svc.remove_worktree(str(repo), str(wt_existing)) is False     # dirty
    assert svc.remove_worktree(str(repo), str(wt_existing), force=True) is True
    assert not wt_existing.exists()

    import shutil
    shutil.rmtree(wt_new)
    assert svc.prune_worktrees(str(repo)) is True
    remaining = {os.path.realpath(w["path"]) for w in svc.list_worktrees(str(repo))}
    assert os.path.realpath(wt_new) not in remaining
    assert svc.delete_branch(str(repo), "fresh", force=True) is True


def test_add_worktree_prefers_origin_base(svc, tmp_path, repo, remote_pair):
    _, other = remote_pair
    tip = commit_file(other, "remote.txt", "r\n", "only on origin")
    git(other, "push", "-q", "origin", "main")
    local_only = tmp_path / "local-base"
    git(repo, "branch", "local-base-branch")

    assert svc.add_worktree(str(repo), str(tmp_path / "wt"), "feat", new_branch=True, base_branch="main") == (True, None)
    # The fetch-before-branch picks up origin/main's newer tip.
    assert git(tmp_path / "wt", "rev-parse", "HEAD") == tip
    # No origin counterpart -> falls back to the local branch.
    assert svc.add_worktree(
        str(repo), str(local_only), "feat2", new_branch=True, base_branch="local-base-branch"
    ) == (True, None)
    assert git(local_only, "rev-parse", "HEAD") == git(repo, "rev-parse", "local-base-branch")


def test_list_worktrees_of_missing_or_non_repo_dir(svc, tmp_path):
    assert svc.list_worktrees(str(tmp_path / "nope")) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert svc.list_worktrees(str(plain)) == []
