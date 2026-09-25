"""GitService read paths against real repositories, through both backends.

Every read has two implementations: pygit2 (``git_native.GitNativeReader``) and
the git CLI fallback in the service mixins. Each test here runs against a real
temporary repository once per backend, so the two stay interchangeable: same
keys, same paths, same edge-case behaviour (empty repo, detached HEAD,
conflicts, renames, binary and non-UTF-8 file names).
"""

import json
import os
import shutil

import pytest

from app.core import git_native
from app.core.git_native import HAS_PYGIT2, GitNativeReader
from app.core.git_service import GitService
from tests.git_repo_helpers import (
    clone,
    commit_file,
    git,
    hermetic_git_env,
    init_repo,
    make_origin,
    non_utf8_name_supported,
    use_backend,
    write,
)

BACKENDS = [
    pytest.param("native", marks=pytest.mark.skipif(not HAS_PYGIT2, reason="pygit2 unavailable")),
    "cli",
]

STATUS_KEYS = {
    "is_git_repo", "current_branch", "is_detached", "head_commit", "remote_url",
    "is_clean", "staged", "modified", "untracked", "ahead", "behind",
    "differs_from_base", "unpushed", "matches_base_tree", "conflicts",
}


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch, tmp_path):
    hermetic_git_env(monkeypatch, tmp_path)
    use_backend(monkeypatch, request.param)
    return request.param


@pytest.fixture
def svc():
    return GitService()


@pytest.fixture
def repo(tmp_path, backend):
    return init_repo(tmp_path / "repo", {
        "a.txt": "".join(f"line {i}\n" for i in range(20)),
        "del.txt": "delete me\n",
        "mod.txt": "one\n",
        "keep.txt": "keep\n",
        "img.bin": b"\x00\x01\x02" * 10,
    })


def _fresh(svc, path):
    svc.invalidate_status_cache(str(path))
    return svc.get_status(str(path))


def _staged(status):
    return {(s["file"], s["status"]) for s in status["staged"]}


# =============================================================================
# Status parsing
# =============================================================================


def test_status_reports_index_codes_worktree_changes_and_untracked(svc, repo, backend):
    git(repo, "rm", "-q", "del.txt")                     # D  (staged delete)
    write(repo / "mod.txt", "one\ntwo\n")
    git(repo, "add", "mod.txt")
    write(repo / "mod.txt", "one\ntwo\nthree\n")          # MM (staged + unstaged)
    write(repo / "new.txt", "new\n")
    git(repo, "add", "new.txt")                          # A
    os.remove(repo / "keep.txt")                         # " D" unstaged delete
    write(repo / "loose.txt", "untracked\n")             # ??

    status = _fresh(svc, repo)

    assert set(status) == STATUS_KEYS
    assert status["is_git_repo"] is True
    assert status["current_branch"] == "main"
    assert status["is_detached"] is False
    assert len(status["head_commit"]) == 12
    assert _staged(status) == {("del.txt", "D"), ("mod.txt", "M"), ("new.txt", "A")}
    # An unstaged deletion is a worktree modification in both backends.
    assert sorted(status["modified"]) == ["keep.txt", "mod.txt"]
    assert status["untracked"] == ["loose.txt"]
    assert status["conflicts"] == []
    assert status["is_clean"] is False
    json.dumps(status)  # the API serializes this dict verbatim


def test_clean_repository_is_clean(svc, repo, backend):
    status = _fresh(svc, repo)
    assert status["is_clean"] is True
    assert status["staged"] == status["modified"] == status["untracked"] == []


def test_staged_rename_reports_usable_paths(svc, repo, backend):
    git(repo, "mv", "a.txt", "renamed.txt")

    staged = _staged(_fresh(svc, repo))

    if backend == "cli":
        # Porcelain -z gives the destination path with the R code; the old
        # "a.txt -> renamed.txt" display string is not a path at all.
        assert staged == {("renamed.txt", "R")}
    else:
        # libgit2 status does not pair renames: delete + add.
        assert staged == {("a.txt", "D"), ("renamed.txt", "A")}
    for path, _code in staged:
        assert " -> " not in path


def test_cli_status_reports_copies_with_the_copy_destination(svc, repo, backend):
    if backend != "cli":
        pytest.skip("libgit2 status has no copy detection")
    git(repo, "config", "status.renames", "copies")
    shutil.copy(repo / "a.txt", repo / "copy.txt")
    write(repo / "a.txt", (repo / "a.txt").read_text() + "more\n")
    git(repo, "add", "-A")

    staged = _staged(_fresh(svc, repo))

    assert staged == {("a.txt", "M"), ("copy.txt", "C")}


def test_non_ascii_path_is_reported_verbatim(svc, repo, backend):
    write(repo / "café menu.txt", "x\n")
    write(repo / "日本.md", "y\n")

    status = _fresh(svc, repo)

    assert sorted(status["untracked"]) == sorted(["café menu.txt", "日本.md"])


def test_non_utf8_path_is_serializable(svc, repo, backend):
    if not non_utf8_name_supported(repo):
        pytest.skip("filesystem rejects non-UTF-8 names")
    with open(os.path.join(os.fsencode(repo), b"bad\xff.txt"), "wb") as fh:
        fh.write(b"x\n")

    status = _fresh(svc, repo)

    assert status["untracked"] == ["bad�.txt"]
    # A lone surrogate would make every JSON response carrying it fail.
    json.dumps(status, ensure_ascii=False).encode("utf-8")


def test_non_utf8_guard_bites_when_disabled(svc, repo, backend, monkeypatch):
    """With the display-path guard disabled the native status is unserializable."""
    if backend != "native":
        pytest.skip("guard lives in the native reader")
    if not non_utf8_name_supported(repo):
        pytest.skip("filesystem rejects non-UTF-8 names")
    monkeypatch.setattr(git_native, "_display_path", lambda p: p)
    with open(os.path.join(os.fsencode(repo), b"bad\xff.txt"), "wb") as fh:
        fh.write(b"x\n")

    status = _fresh(svc, repo)

    with pytest.raises(UnicodeEncodeError):
        json.dumps(status, ensure_ascii=False).encode("utf-8")


def _conflicting_merge(repo, base_rel="c.txt"):
    git(repo, "checkout", "-q", "-b", "other")
    commit_file(repo, base_rel, "other side\n", "other")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, base_rel, "main side\n", "main")
    git(repo, "merge", "other", check=False)


def test_content_conflict_is_listed_only_as_conflict(svc, repo, backend):
    commit_file(repo, "c.txt", "base\n", "base")
    _conflicting_merge(repo)

    status = _fresh(svc, repo)

    assert status["conflicts"] == ["c.txt"]
    assert status["staged"] == [] and status["modified"] == []
    assert status["is_clean"] is False
    assert svc._get_conflict_files(str(repo)) == ["c.txt"]


def test_add_add_and_delete_modify_conflicts(svc, repo, backend):
    # add/add: both branches create the same path.
    _conflicting_merge(repo, "both.txt")
    add_add = _fresh(svc, repo)
    assert add_add["conflicts"] == ["both.txt"]
    assert add_add["modified"] == [] and _staged(add_add) == set()
    git(repo, "merge", "--abort")
    # delete/modify: main deletes, other modifies.
    git(repo, "checkout", "-q", "-b", "mod-side")
    commit_file(repo, "keep.txt", "changed\n", "modify keep")
    git(repo, "checkout", "-q", "main")
    git(repo, "rm", "-q", "keep.txt")
    git(repo, "commit", "-q", "-m", "delete keep")
    git(repo, "merge", "mod-side", check=False)

    status = _fresh(svc, repo)

    assert status["conflicts"] == ["keep.txt"]
    assert status["modified"] == []
    assert _staged(status) == set()


def test_detached_head(svc, repo, backend):
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "later.txt", "x\n", "later")
    git(repo, "checkout", "-q", first)

    status = _fresh(svc, repo)

    assert status["is_detached"] is True
    assert status["current_branch"] is None
    assert status["head_commit"] == first[:12]
    assert status["ahead"] == 0 and status["unpushed"] is None
    assert svc.get_current_branch(str(repo)) is None
    assert svc.get_ahead_behind_counts(str(repo)) == (0, 0)


def test_empty_repository_status_and_stats(svc, tmp_path, backend):
    repo = init_repo(tmp_path / "empty", commit=False)
    write(repo / "first.txt", "a\nb\n")
    write(repo / "loose.txt", "c\n")
    git(repo, "add", "first.txt")

    status = _fresh(svc, repo)

    assert status["is_git_repo"] is True
    assert status["current_branch"] is None
    assert status["head_commit"] is None
    assert status["is_detached"] is False
    assert _staged(status) == {("first.txt", "A")}
    assert status["untracked"] == ["loose.txt"]
    # Regression: pygit2 ``Index.diff_to_tree()`` needs a tree; with an unborn
    # HEAD the native reader silently reported zero staged changes.
    assert svc.get_diff_stats(str(repo)) == {"additions": 2, "deletions": 0, "files_changed": 1}
    assert svc.get_per_file_diff_stats(str(repo)) == [
        {"file": "first.txt", "additions": 2, "deletions": 0, "is_binary": False, "staged": True},
    ]
    staged_diff = svc.get_file_diff(str(repo), "first.txt", staged=True)
    assert "+a" in staged_diff["diff"] and "+b" in staged_diff["diff"]
    assert svc.get_commit_graph(str(repo)) == []
    assert svc.list_branches(str(repo)) == []
    assert svc.get_branch_log(str(repo)) == ("", "")


def test_empty_tree_guard_bites_when_disabled(svc, tmp_path, backend, monkeypatch):
    """Without the empty-tree base, an unborn repo's staged file vanishes."""
    if backend != "native":
        pytest.skip("guard lives in the native reader")

    def _broken(repo):
        raise TypeError("Index.diff_to_tree() missing 1 required positional argument")

    monkeypatch.setattr(git_native, "_empty_tree", _broken)
    repo = init_repo(tmp_path / "empty", commit=False)
    write(repo / "first.txt", "a\nb\n")
    git(repo, "add", "first.txt")

    assert svc.get_diff_stats(str(repo))["files_changed"] == 0


def test_non_repository_and_nested_directory(svc, tmp_path, repo, backend):
    plain = tmp_path / "plain"
    plain.mkdir()
    nested = repo / "sub"
    nested.mkdir()
    write(nested / "x.txt", "x\n")

    assert _fresh(svc, plain)["is_git_repo"] is False
    assert svc.is_git_repo(str(plain)) is False
    # A folder inside another repository is not a repository root: it must not
    # report (or let the UI act on) the enclosing repository's changes.
    nested_status = _fresh(svc, nested)
    assert nested_status["is_git_repo"] is False
    assert nested_status["untracked"] == []
    assert svc.is_git_repo(str(nested)) is False
    assert svc.is_git_repo(str(repo)) is True
    assert svc.get_diff_stats(str(plain)) == {"additions": 0, "deletions": 0, "files_changed": 0}


# =============================================================================
# Ahead / behind against origin
# =============================================================================


def test_ahead_behind_unpushed_and_base_tree(svc, tmp_path, repo, backend):
    bare = make_origin(tmp_path, repo)
    git(repo, "checkout", "-q", "-b", "feature")
    commit_file(repo, "f.txt", "1\n", "f1")
    commit_file(repo, "f.txt", "2\n", "f2")

    status = _fresh(svc, repo)
    assert (status["ahead"], status["behind"]) == (2, 0)
    assert status["differs_from_base"] is True
    assert status["matches_base_tree"] is False
    assert status["unpushed"] is None           # never pushed
    assert status["remote_url"] == str(bare)

    git(repo, "push", "-q", "-u", "origin", "feature")
    assert _fresh(svc, repo)["unpushed"] == 0
    commit_file(repo, "f.txt", "3\n", "f3")
    assert _fresh(svc, repo)["unpushed"] == 1
    assert svc.get_ahead_behind_counts(str(repo)) == (1, 0)

    # Advance origin/main from another clone: the feature branch falls behind.
    other = clone(tmp_path, bare, "other")
    commit_file(other, "o.txt", "o\n", "other work")
    git(other, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    status = _fresh(svc, repo)
    assert (status["ahead"], status["behind"]) == (3, 1)


def test_commits_that_cancel_out_do_not_differ_from_base(svc, repo, backend):
    git(repo, "checkout", "-q", "-b", "noop")
    commit_file(repo, "mod.txt", "changed\n", "change")
    commit_file(repo, "mod.txt", "one\n", "revert")

    status = _fresh(svc, repo)

    assert status["ahead"] == 2
    assert status["differs_from_base"] is False
    assert status["matches_base_tree"] is True


# =============================================================================
# Diff statistics
# =============================================================================


def test_diff_stats_staged_unstaged_and_binary(svc, repo, backend):
    write(repo / "mod.txt", "one\ntwo\n")
    git(repo, "add", "mod.txt")                                  # staged +1
    write(repo / "mod.txt", "one\ntwo\nthree\n")                 # unstaged +1
    write(repo / "a.txt", "replaced\n")                          # unstaged +1 -20
    write(repo / "img.bin", b"\x00\x09" * 10)                    # binary

    both = svc.get_diff_stats(str(repo))
    staged_only = svc.get_diff_stats(str(repo), include_unstaged=False)

    assert both == {"additions": 3, "deletions": 20, "files_changed": 3}
    assert staged_only == {"additions": 1, "deletions": 0, "files_changed": 1}


def test_per_file_stats_split_staged_and_flag_binary(svc, repo, backend):
    write(repo / "mod.txt", "one\ntwo\n")
    git(repo, "add", "mod.txt")
    write(repo / "mod.txt", "one\ntwo\nthree\n")
    write(repo / "img.bin", b"\x00\x09" * 10)

    rows = sorted(
        svc.get_per_file_diff_stats(str(repo)), key=lambda r: (r["file"], r["staged"])
    )

    assert rows == [
        {"file": "img.bin", "additions": 0, "deletions": 0, "is_binary": True, "staged": False},
        {"file": "mod.txt", "additions": 1, "deletions": 0, "is_binary": False, "staged": False},
        {"file": "mod.txt", "additions": 1, "deletions": 0, "is_binary": False, "staged": True},
    ]


def test_per_file_stats_use_status_paths_for_renames_and_unicode(svc, repo, backend):
    (repo / "dir with space").mkdir()
    git(repo, "mv", "a.txt", "dir with space/rénamed.txt")
    write(repo / "café.txt", "x\n")
    git(repo, "add", "café.txt")

    stats = svc.get_per_file_diff_stats(str(repo))
    status_paths = {s["file"] for s in _fresh(svc, repo)["staged"]}

    stat_paths = {r["file"] for r in stats}
    assert "dir with space/rénamed.txt" in stat_paths
    assert "café.txt" in stat_paths
    # Every stats row keys onto a path the status reports (changed-files joins on it).
    assert stat_paths <= status_paths
    assert not any("=>" in p or p.startswith('"') for p in stat_paths)


def test_diff_stats_are_cached_until_invalidated(svc, repo, backend):
    first = svc.get_diff_stats(str(repo))
    write(repo / "mod.txt", "one\ntwo\n")
    assert svc.get_diff_stats(str(repo)) == first
    svc.invalidate_status_cache(str(repo))
    assert svc.get_diff_stats(str(repo))["additions"] == 1


# =============================================================================
# Per-file diffs
# =============================================================================


def test_file_diff_tracked_staged_and_unstaged(svc, repo, backend):
    write(repo / "mod.txt", "one\nstaged\n")
    git(repo, "add", "mod.txt")
    write(repo / "mod.txt", "one\nstaged\nunstaged\n")

    unstaged = svc.get_file_diff(str(repo), "mod.txt")
    staged = svc.get_file_diff(str(repo), "mod.txt", staged=True)

    assert set(unstaged) == {"file", "diff", "is_binary", "truncated"}
    assert unstaged["file"] == "mod.txt"
    assert "+unstaged" in unstaged["diff"] and "+staged" not in unstaged["diff"]
    assert "+staged" in staged["diff"] and "+unstaged" not in staged["diff"]
    assert unstaged["is_binary"] is staged["is_binary"] is False


def test_file_diff_binary_tracked_and_untracked(svc, repo, backend):
    write(repo / "img.bin", b"\x00\x09" * 10)
    write(repo / "new.bin", b"\x00\xff" * 10)

    for rel in ("img.bin", "new.bin"):
        result = svc.get_file_diff(str(repo), rel)
        assert result["is_binary"] is True, rel
        assert result["diff"] == "Binary file (not displayable)"


def test_file_diff_untracked_text_is_synthesized(svc, repo, backend):
    write(repo / "fresh.txt", "alpha\nbeta\n")

    diff = svc.get_file_diff(str(repo), "fresh.txt")["diff"]

    assert "--- /dev/null" in diff and "+++ b/fresh.txt" in diff
    assert "@@ -0,0 +1,2 @@" in diff
    assert "+alpha\n+beta\n" in diff


def test_file_diff_large_file_is_truncated(svc, repo, backend):
    write(repo / "a.txt", "changed line\n" * 5000)

    result = svc.get_file_diff(str(repo), "a.txt", max_chars=1000)

    assert result["truncated"] is True
    assert result["diff"].endswith("[... diff truncated ...]")
    assert len(result["diff"]) < 1100


def test_file_diff_of_staged_rename(svc, repo, backend):
    git(repo, "mv", "a.txt", "renamed.txt")

    result = svc.get_file_diff(str(repo), "renamed.txt", staged=True)

    assert result["is_binary"] is False
    assert "renamed.txt" in result["diff"]


def test_file_diff_never_reads_outside_the_repository(svc, tmp_path, repo, backend):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    os.symlink(secret, repo / "leak.txt")  # untracked symlink out of the repo

    for rel in ("../secret.txt", str(secret), "leak.txt"):
        result = svc.get_file_diff(str(repo), rel)
        assert "TOP SECRET" not in result["diff"], rel
    assert svc.get_file_diff(str(repo), "../secret.txt")["diff"] in (
        "", "Error: path traversal blocked",
    )


def test_file_diff_unknown_file_is_empty(svc, repo, backend):
    assert svc.get_file_diff(str(repo), "nope.txt")["diff"] == ""


def test_diff_text_combines_and_truncates(svc, repo, backend):
    write(repo / "mod.txt", "one\nstaged\n")
    git(repo, "add", "mod.txt")
    write(repo / "keep.txt", "keep\nunstaged\n")

    both = svc.get_diff_text(str(repo))
    staged_only = svc.get_diff_text(str(repo), include_unstaged=False)
    short = svc.get_diff_text(str(repo), max_chars=40)

    assert "+staged" in both and "+unstaged" in both
    assert "+staged" in staged_only and "+unstaged" not in staged_only
    assert short.endswith("[... diff truncated ...]") and len(short) < 80


# =============================================================================
# Branches, commit graph, branch log, default branch
# =============================================================================


def test_list_branches_upstreams_and_ordering(svc, tmp_path, repo, backend):
    make_origin(tmp_path, repo)
    git(repo, "checkout", "-q", "-b", "pushed")
    commit_file(repo, "p.txt", "p\n", "pushed work | with pipe")
    git(repo, "push", "-q", "-u", "origin", "pushed")
    commit_file(repo, "p.txt", "p2\n", "unpushed follow-up")
    # Tracks origin/main but has no remote counterpart of its own.
    git(repo, "checkout", "-q", "-b", "Tracking", "--track", "origin/main")
    commit_file(repo, "t.txt", "t\n", "tracking work")
    git(repo, "checkout", "-q", "main")

    branches = svc.list_branches(str(repo))
    by_name = {b["name"]: b for b in branches}

    assert branches[0]["name"] == "main" and branches[0]["is_current"] is True
    local = [b["name"] for b in branches if not b["is_remote"]]
    assert local == ["main", "pushed", "Tracking"]
    assert "origin/HEAD" not in by_name and "origin" not in by_name
    assert {"origin/main", "origin/pushed"} <= set(by_name)
    assert by_name["origin/main"]["is_remote"] is True
    assert by_name["main"]["upstream"] == "origin/main"
    assert (by_name["pushed"]["upstream"], by_name["pushed"]["ahead"]) == ("origin/pushed", 1)
    assert by_name["Tracking"]["upstream"] is None
    assert (by_name["Tracking"]["ahead"], by_name["Tracking"]["behind"]) == (0, 0)
    assert by_name["main"]["commit"] == git(repo, "rev-parse", "--short=7", "main")
    remote_msg = by_name["origin/pushed"]["commit_message"]
    assert remote_msg == "pushed work | with pipe"
    assert [b["name"] for b in svc.list_branches(str(repo), include_remote=False)] == local


def test_commit_graph_refs_filter_and_limit(svc, repo, backend):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1")
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_file(repo, "s.txt", "s\n", "side commit")
    git(repo, "checkout", "-q", "main")
    head = commit_file(repo, "m.txt", "m\n", "main commit\n\nbody text")

    graph = svc.get_commit_graph(str(repo))
    by_sha = {c["sha"]: c for c in graph}

    assert set(by_sha) == {first, side, head}
    assert "HEAD -> main" in by_sha[head]["refs"]
    assert "tag: v1" in by_sha[first]["refs"]
    assert by_sha[side]["refs"] == ["side"]
    assert by_sha[head]["message"] == "main commit"
    assert by_sha[head]["parents"] == [first]
    assert by_sha[head]["short_sha"] == head[:7]
    assert by_sha[head]["author_email"] == "author@example.invalid"
    assert by_sha[head]["timestamp"][:4].isdigit()

    only_side = svc.get_commit_graph(str(repo), branch="side")
    assert [c["sha"] for c in only_side] == [side, first]
    assert len(svc.get_commit_graph(str(repo), limit=1)) == 1
    assert svc.get_commit_graph(str(repo), branch="no-such-branch") == []


def test_branch_log_lists_commits_since_base(svc, repo, backend):
    git(repo, "checkout", "-q", "-b", "feat")
    c1 = commit_file(repo, "f.txt", "1\n", "feat one")
    c2 = commit_file(repo, "f.txt", "2\n", "feat two")

    log, diff = svc.get_branch_log(str(repo), "main")

    assert log.splitlines() == [f"{c2[:7]} feat two", f"{c1[:7]} feat one"]
    assert "f.txt" in diff
    assert svc.get_branch_log(str(repo), "does-not-exist")[0] == ""


def test_default_branch_resolution(svc, tmp_path, backend):
    with_origin = init_repo(tmp_path / "o")
    make_origin(tmp_path, with_origin)
    master_only = init_repo(tmp_path / "m", branch="master")
    neither = init_repo(tmp_path / "n", branch="trunk")

    assert svc.get_default_branch(str(with_origin)) == "main"
    assert svc.get_default_branch(str(master_only)) == "master"
    assert svc.get_default_branch(str(neither)) == "main"
    assert svc.get_remote_url(str(master_only)) is None
    assert svc.get_remote_url(str(with_origin)).endswith("origin.git")


# =============================================================================
# Native -> CLI fallback
# =============================================================================


@pytest.mark.skipif(not HAS_PYGIT2, reason="pygit2 unavailable")
def test_native_failure_falls_back_to_cli(svc, repo, backend, monkeypatch):
    if backend != "native":
        pytest.skip("fallback starts from the native reader")

    def boom(self, *a, **k):
        raise RuntimeError("libgit2 exploded")

    for name in ("get_status", "get_diff_stats", "get_per_file_diff_stats",
                 "get_file_diff", "list_branches", "get_commit_graph",
                 "get_current_branch", "is_git_repo"):
        monkeypatch.setattr(GitNativeReader, name, boom)
    write(repo / "mod.txt", "one\ntwo\n")

    status = _fresh(svc, repo)
    assert status["modified"] == ["mod.txt"]
    assert svc.get_diff_stats(str(repo))["additions"] == 1
    assert svc.get_per_file_diff_stats(str(repo))[0]["file"] == "mod.txt"
    assert "+two" in svc.get_file_diff(str(repo), "mod.txt")["diff"]
    assert svc.list_branches(str(repo))[0]["name"] == "main"
    assert len(svc.get_commit_graph(str(repo))) == 1
    assert svc.get_current_branch(str(repo)) == "main"
    assert svc.is_git_repo(str(repo)) is True
