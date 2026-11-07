"""Never bind a branch checkout left half-populated by a failed Git add."""
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import checkout_manager
from app.core.git_worktree_ops import WorktreeAddError
from app.db import checkouts as checkouts_db
from tests.core.test_checkout_materialization import (
    RUNNER, _add_legacy_worktree, _git, _seed_git_repository, _seed_legacy_project,
    _seed_worktree, base_checkout, project, workspace_root,
)
from tests.core.test_git_worktree_lfs import POINTER, lfs_repo
from tests.shared_fixtures import foreign_keys_off  # noqa: F401 (autouse fixture)


def _resolve(project, branch, **kwargs):
    return checkout_manager.ensure_checkout(
        project, branch=branch, runner_id=RUNNER, provision_runtime=False, **kwargs,
    )


def _materializing_marker(base, path):
    common = checkout_manager.git_service._run_git(
        str(base), ["rev-parse", "--git-common-dir"], timeout=10,
    )
    assert common.returncode == 0
    key = hashlib.sha256(os.path.normcase(os.path.realpath(path)).encode()).hexdigest()
    return Path(base) / common.stdout.strip() / "ai-shuffle-materializing" / key


def _failure_marker(path):
    result = checkout_manager.git_service._run_git(
        str(path), ["rev-parse", "--git-path", "ai-shuffle-worktree-add-failed"], timeout=10,
    )
    assert result.returncode == 0
    return Path(path) / result.stdout.strip()


def _snapshot(path):
    """Include ignored/untracked files and Git's private worktree index."""
    index = checkout_manager.git_service._run_git(
        str(path), ["rev-parse", "--git-path", "index"], timeout=10,
    )
    index_path = Path(path) / index.stdout.strip()
    files = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return files, index_path.read_bytes() if index_path.is_file() else None


@pytest.mark.parametrize("previously_materialized", [False, True])
def test_failed_add_partial_checkout_is_not_adopted(
    project, base_checkout, workspace_root, caplog, previously_materialized,
):
    branch = "fix/failed-add"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    if previously_materialized:
        assert _resolve(project, branch) == str(path)
        # Leave a stale active row; a known failed add must override it.
        _git(base_checkout, "worktree", "remove", str(path))
    before_rows = checkouts_db.list_checkouts(project["id"])
    # Git really fails *after* registration. Removing a file in a failing hook
    # deterministically models the incomplete checkout a failed rollback leaves.
    hook = base_checkout / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\necho 'checkout interrupted' >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(WorktreeAddError, match="checkout interrupted"):
        _resolve(project, branch, raise_add_errors=True)
    assert path.is_dir() and not (path / "README.md").exists()
    assert _failure_marker(path).read_text() == "incomplete: failed add\n"
    assert str(path).replace("\\", "/") in checkout_manager.git_service._run_git(
        str(base_checkout), ["worktree", "list", "--porcelain"], timeout=10,
    ).stdout.replace("\\", "/")
    after_failure_rows = checkouts_db.list_checkouts(project["id"])
    if previously_materialized:
        previous = {r["id"]: r for r in before_rows}
        for row in after_failure_rows:
            old = previous[row["id"]]
            assert {k: v for k, v in row.items() if k not in ("status", "updated_at")} == {
                k: v for k, v in old.items() if k not in ("status", "updated_at")
            }
            assert row["status"] == ("incomplete" if row["branch"] == branch else old["status"])
    else:
        assert after_failure_rows == before_rows
    before = _snapshot(path)

    assert _resolve(project, branch) is None
    assert "incomplete" in caplog.text.lower()
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert _snapshot(path) == before
    assert checkouts_db.list_checkouts(project["id"]) == after_failure_rows


@pytest.mark.parametrize("route", ["deterministic", "row", "legacy", "sweep"])
@pytest.mark.parametrize("damage", ["missing-index", "staged-deletion", "missing-file"])
def test_incomplete_checkout_is_refused_without_changing_files_or_rows(
    project, base_checkout, workspace_root, tmp_path, caplog, route, damage,
):
    branch = "fix/incomplete"
    path = (tmp_path / "legacy" if route == "legacy" else
            checkout_manager._branch_checkout_path(str(base_checkout), branch))
    _add_legacy_worktree(base_checkout, path, branch)
    if route == "row":
        checkouts_db.upsert_checkout(
            project["id"], RUNNER, str(path), branch=branch, status="archived",
        )
    elif route == "legacy":
        _seed_legacy_project("lp1", "Repo", str(base_checkout), link_group_id="git:repo")
        _seed_git_repository("gr1", "lp1")
        _seed_worktree("wt1", "gr1", branch, str(path))
    if damage == "missing-index":
        index = checkout_manager.git_service._run_git(
            str(path), ["rev-parse", "--git-path", "index"], timeout=10,
        )
        (path / index.stdout.strip()).unlink()
    elif damage == "staged-deletion":
        _git(path, "rm", "--cached", "README.md")
    else:
        (path / "README.md").unlink()
    # An incomplete checkout can already contain user work. Refusal must not
    # restore deleted files, reset the index, remove, prune, or refresh its row.
    (path / "notes.txt").write_text("uncommitted user work\n")
    if damage != "missing-file":
        (path / "README.md").write_text("user edit\n")
    before_files = _snapshot(path)
    before_rows = checkouts_db.list_checkouts(project["id"])

    if route == "sweep":
        assert checkout_manager.register_unregistered_worktrees(
            project["id"], RUNNER, str(base_checkout),
        ) == []
    else:
        assert _resolve(project, branch) is None
        with pytest.raises(WorktreeAddError, match="incomplete"):
            _resolve(project, branch, raise_add_errors=True)
    assert "incomplete" in caplog.text.lower()
    assert _snapshot(path) == before_files
    assert checkouts_db.list_checkouts(project["id"]) == before_rows


@pytest.mark.parametrize("route", ["deterministic", "row", "legacy", "sweep"])
def test_complete_checkout_with_local_edits_is_still_adopted(
    project, base_checkout, workspace_root, tmp_path, route,
):
    branch = "fix/complete"
    path = (tmp_path / "legacy" if route == "legacy" else
            checkout_manager._branch_checkout_path(str(base_checkout), branch))
    _add_legacy_worktree(base_checkout, path, branch)
    if route == "row":
        checkouts_db.upsert_checkout(
            project["id"], RUNNER, str(path), branch=branch, status="archived",
        )
    elif route == "legacy":
        _seed_legacy_project("lp1", "Repo", str(base_checkout), link_group_id="git:repo")
        _seed_git_repository("gr1", "lp1")
        _seed_worktree("wt1", "gr1", branch, str(path))
    (path / "README.md").write_text("staged user edit\n")
    _git(path, "add", "README.md")
    (path / "README.md").write_text("unstaged user edit\n")
    (path / "notes.txt").write_text("untracked user work\n")
    before = _snapshot(path)

    if route == "sweep":
        assert checkout_manager.register_unregistered_worktrees(
            project["id"], RUNNER, str(base_checkout),
        ) == [str(path)]
    assert _resolve(project, branch) == str(path)
    assert _resolve(project, branch) == str(path)  # Recorded-row reuse too.
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "active"
    assert _snapshot(path) == before


def test_lfs_pointer_checkout_is_adopted_without_running_filters(
    project, lfs_repo, workspace_root,
):
    # Exercise add_worktree's real unavailable-LFS fallback with no checkout
    # row, by building through GitService directly before resolving the branch.
    repo = lfs_repo
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))
    branch = "fix/lfs"
    path = checkout_manager._branch_checkout_path(str(repo), branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    success, warning = checkout_manager.git_service.add_worktree(
        str(repo), str(path), branch, new_branch=True,
    )
    assert success, warning
    assert "placeholders" in warning
    assert (path / "asset.bin").read_text() == POINTER
    config = (repo / ".git" / "config").read_bytes()
    before = _snapshot(path)

    # Filter commands point at missing tooling. Any status/diff that invokes
    # clean/smudge would fail; the completeness check must remain filter-free.
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert (repo / ".git" / "config").read_bytes() == config
    assert _snapshot(path) == before


@pytest.mark.parametrize("materialization", ["created", "adopted"])
@pytest.mark.parametrize("edit", ["staged-rename", "unstaged-delete", "untracked-add"])
def test_established_checkout_continues_after_intentional_changes(
    project, base_checkout, workspace_root, monkeypatch, materialization, edit,
):
    from app.core.query_options import _resolve_checkout_for_options

    branch = "fix/continuation"
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    if materialization == "adopted":
        _add_legacy_worktree(base_checkout, path, branch)
    assert _resolve(project, branch, create_new_branch=materialization == "created") == str(path)
    if edit == "staged-rename":
        _git(path, "mv", "README.md", "RENAMED.md")
    elif edit == "unstaged-delete":
        (path / "README.md").unlink()
    else:
        (path / "user-work.txt").write_text("untracked user work\n")
    before = _snapshot(path)
    monkeypatch.setattr(
        type(checkout_manager.settings), "effective_instance_id", property(lambda _: RUNNER),
    )
    session = {"id": "continuing", "project_v2_id": project["id"], "branch": branch}

    assert _resolve_checkout_for_options(session) == str(path)
    assert _snapshot(path) == before
    # Successful materialization evidence must survive calls (not a one-call
    # bypass). Reuse should not even inspect index/HEAD completeness again.
    def unexpected_revalidation(path):
        raise AssertionError("established checkout was revalidated")

    monkeypatch.setattr(checkout_manager.git_service, "_checkout_is_complete", unexpected_revalidation)
    assert _resolve_checkout_for_options(session) == str(path)
    assert _snapshot(path) == before


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("git ls-files", 10), RuntimeError("inspection failed"),
])
def test_base_resolution_survives_unrelated_worktree_inspection_error(
    project, base_checkout, workspace_root, monkeypatch, caplog, failure,
):
    bad = _add_legacy_worktree(base_checkout, base_checkout / ".fluid/worktrees/a-bad", "fix/bad")
    good = _add_legacy_worktree(base_checkout, base_checkout / ".fluid/worktrees/z-good", "fix/good")
    original = checkout_manager.git_service._checkout_is_complete

    def inspect(path):
        if Path(path) == bad:
            raise failure
        return original(path)

    monkeypatch.setattr(checkout_manager.git_service, "_checkout_is_complete", inspect)
    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER, branch=None) == str(base_checkout)
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch="fix/bad") is None
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch="fix/good")["path"] == str(good)
    assert "inspection" in caplog.text.lower() or "timed out" in caplog.text.lower()


@pytest.mark.parametrize("edit", ["staged-rename", "unstaged-delete", "untracked-add"])
def test_pre_upgrade_established_checkout_continues_on_first_resolution(
    project, base_checkout, workspace_root, monkeypatch, edit,
):
    from app.core.query_options import _resolve_checkout_for_options

    branch = "fix/pre-upgrade"
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    # Pre-PR setup used real Git then recorded successful materialization. Do
    # not resolve via new code: there must be no validation marker beforehand.
    _add_legacy_worktree(base_checkout, path, branch)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(path), branch=branch)
    if edit == "staged-rename":
        _git(path, "mv", "README.md", "RENAMED.md")
    elif edit == "unstaged-delete":
        (path / "README.md").unlink()
    else:
        (path / "user-work.txt").write_text("work predating upgrade\n")
    before = _snapshot(path)
    monkeypatch.setattr(
        type(checkout_manager.settings), "effective_instance_id", property(lambda _: RUNNER),
    )
    session = {"id": "pre-upgrade", "project_v2_id": project["id"], "branch": branch}

    assert _resolve_checkout_for_options(session) == str(path)
    assert _snapshot(path) == before
    # Trusted active rows without a failed-add marker never revalidate the
    # user's intentional work against HEAD, even on their first upgraded send.
    monkeypatch.setattr(checkout_manager.git_service, "_checkout_is_complete",
                        lambda _: pytest.fail("established checkout was revalidated"))
    assert _resolve_checkout_for_options(session) == str(path)


@pytest.mark.parametrize("edit", ["staged-rename", "unstaged-delete"])
def test_pre_upgrade_successful_recreation_continues_after_intentional_changes(
    project, base_checkout, workspace_root, monkeypatch, edit,
):
    from app.core.query_options import _resolve_checkout_for_options

    branch = "fix/recreated-pre-upgrade"
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    _add_legacy_worktree(base_checkout, path, branch)
    first = checkouts_db.upsert_checkout(project["id"], RUNNER, str(path), branch=branch)
    _git(base_checkout, "worktree", "remove", str(path))
    _git(base_checkout, "worktree", "add", str(path), branch)
    # This is the old successful setup writer; its upsert retains created_at.
    second = checkouts_db.upsert_checkout(project["id"], RUNNER, str(path), branch=branch)
    assert second["created_at"] == first["created_at"]
    if edit == "staged-rename":
        _git(path, "mv", "README.md", "RENAMED.md")
    else:
        (path / "README.md").unlink()
    before = _snapshot(path)
    monkeypatch.setattr(
        type(checkout_manager.settings), "effective_instance_id", property(lambda _: RUNNER),
    )
    session = {"id": "pre-upgrade-recreated", "project_v2_id": project["id"], "branch": branch}

    assert _resolve_checkout_for_options(session) == str(path)
    assert _snapshot(path) == before


def test_successful_add_clears_failure_marker(project, base_checkout, workspace_root):
    branch = "fix/success-clears-failure"
    _git(base_checkout, "branch", branch)
    # A successful real add leaves a previously-created flag in its admin dir.
    # The public add_worktree success path must clear it before accepting it.
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text('#!/bin/sh\ntouch "$(git rev-parse --git-path ai-shuffle-worktree-add-failed)"\n')
    hook.chmod(0o755)
    path = Path(_resolve(project, branch))
    assert not _failure_marker(path).exists()
    (path / "README.md").unlink()
    assert _resolve(project, branch, raise_add_errors=True) == str(path)


def test_repaired_failed_checkout_clears_failure_marker(
    project, base_checkout, workspace_root,
):
    branch = "fix/repaired"
    _git(base_checkout, "branch", branch)
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 1\n")
    hook.chmod(0o755)
    assert _resolve(project, branch) is None
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    assert _failure_marker(path).is_file()
    # The user repairs the failed materialization; the app never resets it.
    _git(path, "-c", "core.hooksPath=unused-hooks", "restore", "--source=HEAD",
         "--staged", "--worktree", "README.md")

    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert not _failure_marker(path).exists()
    (path / "README.md").unlink()
    assert _resolve(project, branch, raise_add_errors=True) == str(path)


def test_failed_add_registration_without_git_file_is_marked(
    project, base_checkout, workspace_root,
):
    branch = "fix/registration-only"
    _git(base_checkout, "branch", branch)
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ncp .git saved-git-pointer\nrm -- .git README.md\nexit 1\n")
    hook.chmod(0o755)
    assert _resolve(project, branch) is None
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    pointer = (path / "saved-git-pointer").read_text()
    admin = Path(pointer.removeprefix("gitdir: ").strip())
    assert (admin / "ai-shuffle-worktree-add-failed").is_file()
    # Simulate the user restoring only Git's pointer; tracked files remain absent.
    (path / ".git").write_text(pointer)
    checkouts_db.upsert_checkout(project["id"], "local", str(path), branch)
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)


def test_refused_add_over_existing_checkout_does_not_flag_user_changes(
    project, base_checkout, workspace_root,
):
    branch = "fix/existing-refusal"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    (path / "README.md").unlink()
    before = _snapshot(path)
    success, error = checkout_manager.git_service.add_worktree(
        str(base_checkout), str(path), branch,
    )
    assert not success and error
    assert not _failure_marker(path).exists()
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert _snapshot(path) == before


def test_failed_add_uses_negative_row_status_when_metadata_is_unwritable(
    project, base_checkout, workspace_root, monkeypatch, caplog,
):
    branch = "fix/locked-failure-metadata"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    _git(base_checkout, "worktree", "remove", str(path))
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 1\n")
    hook.chmod(0o755)
    write_text = Path.write_text

    def locked_marker(self, *args, **kwargs):
        if self.name == "ai-shuffle-worktree-add-failed":
            raise PermissionError("failure metadata is locked")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", locked_marker)
    assert _resolve(project, branch) is None
    assert "Could not record worktree add outcome" in caplog.text
    assert not _failure_marker(path).exists()
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "incomplete"
    before = _snapshot(path)
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert _snapshot(path) == before


def test_killed_add_with_stale_active_row_is_not_adopted(
    project, base_checkout, workspace_root,
):
    branch = "fix/killed-materialization"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    _git(base_checkout, "worktree", "remove", str(path))
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 0\n")
    hook.chmod(0o755)
    # Kill the materializer immediately after real Git registers the replacement,
    # before any add-result handling, validation, or DB status update can run.
    script = f"""
import os, signal
from app.core.git_service import GitService
service = GitService()
run = service._run_git
def interrupted(cwd, args, **kwargs):
    result = run(cwd, args, **kwargs)
    if args[:2] == ["worktree", "add"]:
        if hasattr(signal, "SIGKILL"):
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(137)
    return result
service._run_git = interrupted
service.add_worktree({str(base_checkout)!r}, {str(path)!r}, {branch!r}, fetch_base=False)
"""
    killed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15,
        cwd=Path(__file__).resolve().parents[2],
    )
    expected = -signal.SIGKILL if hasattr(signal, "SIGKILL") else 137
    assert killed.returncode == expected, killed.stderr
    assert not (path / "README.md").exists()
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "active"
    before = _snapshot(path)
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert _snapshot(path) == before
    assert _materializing_marker(base_checkout, path).is_file()


@pytest.mark.parametrize("previously_materialized", [False, True])
def test_zero_exit_hook_partial_checkout_stays_refused(
    project, base_checkout, workspace_root, previously_materialized,
):
    branch = "fix/zero-exit-hook"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    if previously_materialized:
        assert _resolve(project, branch) == str(path)
        _git(base_checkout, "worktree", "remove", str(path))
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 0\n")
    hook.chmod(0o755)
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    before = _snapshot(path)
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert _snapshot(path) == before
    assert _materializing_marker(base_checkout, path).is_file()


@pytest.mark.parametrize("previously_materialized", [False, True])
def test_successful_add_has_write_ahead_flag_then_allows_intentional_edits(
    project, base_checkout, workspace_root, monkeypatch, previously_materialized,
):
    branch = "fix/write-ahead-success"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    if previously_materialized:
        assert _resolve(project, branch) == str(path)
        _git(base_checkout, "worktree", "remove", str(path))
    run = checkout_manager.git_service._run_git
    starts = []

    def observed(cwd, args, **kwargs):
        if args[:2] == ["worktree", "add"]:
            common = run(str(base_checkout), ["rev-parse", "--git-common-dir"], timeout=10)
            key = hashlib.sha256(os.path.normcase(os.path.realpath(path)).encode()).hexdigest()
            marker = base_checkout / common.stdout.strip() / "ai-shuffle-materializing" / key
            assert marker.is_file(), "add must not start without a durable flag"
            if previously_materialized:
                assert checkouts_db.get_checkout(
                    project["id"], RUNNER, branch=branch,
                )["status"] == "incomplete", "DB fallback must also precede Git"
            starts.append(True)
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(checkout_manager.git_service, "_run_git", observed)
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert starts == [True]
    assert not _materializing_marker(base_checkout, path).exists()
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "active"
    (path / "README.md").unlink()
    before = _snapshot(path)
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert _snapshot(path) == before


@pytest.mark.parametrize("route", ["active", "adoption"])
def test_legacy_recreate_cannot_bypass_pending_materialization(
    project, base_checkout, workspace_root, monkeypatch, route,
):
    import app.core.worktree_manager as wm
    from app.db.base import WorktreeStatus

    branch = "fix/legacy-pending"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 0\n")
    hook.chmod(0o755)
    success, error = checkout_manager.git_service.add_worktree(
        str(base_checkout), str(path), branch, fetch_base=False,
    )
    assert not success and "incomplete" in error
    manager = wm.WorktreeManager()
    row = {"id": "legacy-pending", "worktree_path": str(path),
           "branch_name": branch, "status": WorktreeStatus.ACTIVE}
    monkeypatch.setattr(wm.database, "get_worktree", lambda _: row)
    monkeypatch.setattr(wm.database, "update_worktree",
                        lambda *args, **kwargs: pytest.fail("partial checkout activated"))
    before = _snapshot(path)
    with pytest.raises(wm.WorktreeError, match="incomplete"):
        if route == "active":
            manager.recreate_worktree(row["id"])
        else:
            manager._adopt_existing_worktree(
                row["id"], branch, {"path": str(path)}, {"path": str(base_checkout)},
                prior_status=WorktreeStatus.ORPHANED,
            )
    assert _snapshot(path) == before
    assert _materializing_marker(base_checkout, path).is_file()


def test_unwritable_write_ahead_metadata_prevents_git_add(
    project, base_checkout, workspace_root, monkeypatch,
):
    branch = "fix/blocked-write-ahead"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    _git(base_checkout, "worktree", "remove", str(path))
    write_text = Path.write_text

    def locked(self, *args, **kwargs):
        if self.parent.name == "ai-shuffle-materializing":
            raise PermissionError("write-ahead metadata locked")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", locked)
    monkeypatch.setattr(checkout_manager.git_service, "_add_worktree_unlocked",
                        lambda *args, **kwargs: pytest.fail("Git started without write-ahead state"))
    with pytest.raises(WorktreeAddError, match="Cannot record checkout materialization"):
        _resolve(project, branch, raise_add_errors=True)
    assert not path.exists()
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "incomplete"


def test_overlapping_sweep_cannot_publish_inflight_partial_checkout(
    project, base_checkout, workspace_root, tmp_path, monkeypatch,
):
    import shlex

    branch = "fix/overlapping-sweep"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    ready, release = tmp_path / "hook-ready", tmp_path / "hook-release"
    script = tmp_path / "paused-hook.py"
    script.write_text(f"""
import time
from pathlib import Path
Path({str(ready)!r}).write_text("ready")
deadline = time.monotonic() + 10
while not Path({str(release)!r}).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("test did not release hook")
    time.sleep(.01)
Path("README.md").unlink()
""")
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\n{shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    hook.chmod(0o755)
    materialized, swept, errors = [], [], []
    entered_sweep, sweep_done = threading.Event(), threading.Event()
    base_resolver = checkout_manager._ensure_base_checkout

    def observed_base(*args, **kwargs):
        result = base_resolver(*args, **kwargs)
        if threading.current_thread().name == "overlapping-sweep":
            entered_sweep.set()
        return result

    monkeypatch.setattr(checkout_manager, "_ensure_base_checkout", observed_base)

    def materialize():
        try:
            materialized.append(_resolve(project, branch))
        except BaseException as exc:
            errors.append(exc)

    def sweep():
        try:
            swept.append(_resolve(project, None))
        except BaseException as exc:
            errors.append(exc)
        finally:
            sweep_done.set()

    producer = threading.Thread(target=materialize)
    adopter = threading.Thread(target=sweep, name="overlapping-sweep")
    producer.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), errors
        assert (path / "README.md").is_file()  # Temporarily complete, hook still running.
        adopter.start()
        assert entered_sweep.wait(5), errors
        swept_before_hook_finished = sweep_done.wait(.5)
        pending_before_hook_finished = _materializing_marker(base_checkout, path).is_file()
    finally:
        release.write_text("release")
        producer.join(10)
        if adopter.ident is not None:
            adopter.join(10)
    assert not producer.is_alive() and not adopter.is_alive()
    assert not errors
    assert materialized == [None] and swept == [str(base_checkout)]
    assert not (path / "README.md").exists()
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert not swept_before_hook_finished, "sweep must wait for the whole add, including its hook"
    assert pending_before_hook_finished
    assert _materializing_marker(base_checkout, path).is_file()
    row = checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)
    assert not row or row["status"] != "active"


def test_repository_lock_unifies_linked_paths_and_processes(base_checkout, tmp_path):
    from app.core.git_worktree_ops import repo_worktree_lock

    path = tmp_path / "linked-lock"
    _add_legacy_worktree(base_checkout, path, "fix/lock-alias")
    lock = repo_worktree_lock(str(base_checkout))
    assert repo_worktree_lock(str(path)) is lock
    assert repo_worktree_lock(str(base_checkout / ".git")) is lock
    with lock:
        script = (
            "from app.core.git_worktree_ops import repo_worktree_lock; "
            f"lock = repo_worktree_lock({str(path)!r}); "
            "acquired = lock.acquire(blocking=False); print(acquired); "
            "lock.release() if acquired else None"
        )
        child = subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True, timeout=10, cwd=Path(__file__).resolve().parents[2])
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "False", "another process must share the same lock"
    assert lock.acquire(blocking=False)
    lock.release()


def test_metadata_refresh_cannot_reactivate_failed_materialization(
    project, base_checkout, workspace_root, monkeypatch,
):
    from app.core import git_ops_runner, project_resolver, turn_finalize
    from app.db import database

    branch = "fix/failed-refresh"
    _git(base_checkout, "branch", branch)
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 0\n")
    hook.chmod(0o755)
    assert _resolve(project, branch) is None
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    monkeypatch.setattr(checkouts_db, "upsert_checkout",
                        lambda *args, **kwargs: pytest.fail("metadata activated a partial checkout"))
    git_ops_runner._record_checkout_state(project["id"], str(path), branch)
    checkout = {"path": str(path), "project_id": project["id"], "runner_id": RUNNER,
                "branch": "fix/old", "status": "active"}
    monkeypatch.setattr(project_resolver, "resolve_local_checkout", lambda _: checkout)
    monkeypatch.setattr(database, "get_session",
                        lambda _: {"id": "refresh", "branch": "fix/old"})
    monkeypatch.setattr(turn_finalize, "_checkout_shared_with_another_session", lambda *args: False)
    monkeypatch.setattr(database, "resync_session_branch",
                        lambda *args: pytest.fail("session rebound to a partial checkout"))
    assert turn_finalize.resync_session_branch_from_head("refresh") is None
    assert _materializing_marker(base_checkout, path).is_file()


def test_validation_failure_restores_evidence_and_invalidates_active_row(
    project, base_checkout, workspace_root,
):
    branch = "fix/restore-evidence"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    # Simulate an old reader clearing state, then a failed full validation.
    (path / "README.md").unlink()
    with pytest.raises(WorktreeAddError, match="incomplete"):
        checkout_manager.git_service.require_complete_worktree(str(path), force=True)
    assert _materializing_marker(base_checkout, path).is_file()
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)["status"] == "incomplete"


@pytest.mark.parametrize("consumer", ["import", "fingerprint", "publication"])
def test_overlapping_handoff_cannot_accept_inflight_checkout(
    project, base_checkout, workspace_root, tmp_path, consumer,
):
    import shlex
    from app.core import checkout_transfer

    branch = "fix/overlapping-handoff"
    _git(base_checkout, "branch", branch)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(base_checkout, "remote", "add", "origin", str(origin))
    _git(base_checkout, "push", "origin", branch)
    head = checkout_transfer.git(base_checkout, "rev-parse", "HEAD").decode().strip()
    state = {"protocol": "git-origin-v1", "branch": branch, "head": head,
             "remote": checkout_transfer.remote_identity(base_checkout)}
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    ready, release = tmp_path / "handoff-ready", tmp_path / "handoff-release"
    script = tmp_path / "handoff-hook.py"
    script.write_text(f"""
import time
from pathlib import Path
Path({str(ready)!r}).write_text("ready")
deadline = time.monotonic() + 10
while not Path({str(release)!r}).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("test did not release hook")
    time.sleep(.01)
Path("README.md").unlink()
""")
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\n{shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    hook.chmod(0o755)
    added, accepted, refused, errors = [], [], [], []
    entered, done = threading.Event(), threading.Event()

    def add():
        try:
            added.append(_resolve(project, branch))
        except BaseException as exc:
            errors.append(exc)

    def receive():
        entered.set()
        try:
            if consumer == "import":
                accepted.append(checkout_transfer.import_checkout(base_checkout, path, state))
            elif consumer == "publication":
                from app.network import handoff
                accepted.append(handoff._publish_receiving_checkout(project["id"], base_checkout, str(path), state))
            else:
                accepted.append(checkout_transfer.fingerprint(path))
        except (ValueError, WorktreeAddError) as exc:
            refused.append(str(exc))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    producer, receiver = threading.Thread(target=add), threading.Thread(target=receive)
    producer.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), errors
        assert (path / "README.md").is_file()
        receiver.start()
        assert entered.wait(5)
        returned_during_add = done.wait(.5)
        marker_during_add = _materializing_marker(base_checkout, path).is_file()
    finally:
        release.write_text("release")
        producer.join(10)
        if receiver.ident is not None:
            receiver.join(10)
    assert not producer.is_alive() and not receiver.is_alive()
    assert not errors
    assert not returned_during_add, "handoff accepted an in-flight materialization"
    assert marker_during_add and _materializing_marker(base_checkout, path).is_file()
    assert added == [None] and not accepted and refused
    assert not checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)


@pytest.mark.parametrize("blocking", [True, False])
def test_windows_repository_lock_waits_past_ten_seconds(base_checkout, monkeypatch, blocking):
    import errno
    from types import SimpleNamespace
    from app.core import git_worktree_ops as ops

    lock = ops._RepositoryLock(base_checkout / ".git")
    elapsed, calls = [], []

    def locking(fd, mode, count):
        calls.append(mode)
        if len(calls) <= 110:
            raise OSError(errno.EACCES, "byte range is locked")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, locking=locking))
    monkeypatch.setattr(ops, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ops, "time", SimpleNamespace(
        monotonic=lambda: sum(elapsed), sleep=lambda seconds: elapsed.append(seconds),
    ))
    assert lock.acquire(blocking=blocking) is blocking
    if blocking:
        assert sum(elapsed) > 10
        assert len(calls) == 111 and all(mode == 2 for mode in calls)
        lock.release()
    else:
        assert len(calls) == 1 and not elapsed
    assert lock.depth == 0 and lock.file is None


@pytest.mark.parametrize("base_role", [True, False])
def test_handoff_publication_keeps_validation_and_active_upsert_locked(
    project, base_checkout, tmp_path, monkeypatch, base_role,
):
    from app.core import checkout_transfer
    from app.core.git_worktree_ops import repo_worktree_lock
    from app.network import handoff

    branch = checkout_transfer.git(base_checkout, "branch", "--show-current").decode().strip()
    path = base_checkout
    if not base_role:
        branch, path = "fix/publish-ready", tmp_path / "publish-ready"
        _add_legacy_worktree(base_checkout, path, branch)
    head = checkout_transfer.git(path, "rev-parse", "HEAD").decode().strip()
    original = checkouts_db.upsert_checkout
    probes = []

    def probe():
        lock = repo_worktree_lock(str(path))
        acquired = lock.acquire(blocking=False)
        probes.append(acquired)
        if acquired:
            lock.release()

    def guarded_upsert(*args, **kwargs):
        contender = threading.Thread(target=probe)
        contender.start()
        contender.join(5)
        assert not contender.is_alive() and probes == [False]
        return original(*args, **kwargs)

    monkeypatch.setattr(checkouts_db, "upsert_checkout", guarded_upsert)
    result = handoff._publish_receiving_checkout(
        project["id"], base_checkout, str(path), {"branch": branch, "head": head},
    )
    assert result == (None if base_role else branch)
    row = checkouts_db.get_checkout(project["id"], handoff.settings.effective_instance_id, branch=result)
    assert row["status"] == "active" and row["path"] == str(path) and row["head_sha"] == head


@pytest.mark.parametrize("blocking", [True, False])
def test_windows_repository_lock_does_not_retry_noncontention(base_checkout, monkeypatch, blocking):
    import errno
    from types import SimpleNamespace
    from app.core import git_worktree_ops as ops

    lock = ops._RepositoryLock(base_checkout / ".git")

    def broken(*args):
        raise OSError(errno.EBADF, "not a contention error")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=broken))
    monkeypatch.setattr(ops, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ops, "time", SimpleNamespace(
        monotonic=lambda: 0, sleep=lambda _: pytest.fail("retried a permanent lock error"),
    ))
    with pytest.raises(OSError, match="not a contention error"):
        lock.acquire(blocking=blocking)
    assert lock.depth == 0 and lock.file is None
    assert lock.thread_lock.acquire(blocking=False)
    lock.thread_lock.release()


@pytest.mark.parametrize("consumer", ["export", "worker_restore"])
def test_handoff_export_and_worker_restore_refuse_failed_existing_directory(
    project, base_checkout, workspace_root, monkeypatch, consumer,
):
    from app.core import checkout_transfer, project_resolver, worker_checkout
    from app.db import database

    worker = "resume-race"
    branch = worker_checkout.worker_branch(worker)
    _git(base_checkout, "branch", branch)
    hook = base_checkout / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\nexit 0\n")
    hook.chmod(0o755)
    assert _resolve(project, branch) is None
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    before = _snapshot(path)
    if consumer == "export":
        monkeypatch.setattr(checkout_transfer, "_export_checkout_locked",
                            lambda *args: pytest.fail("export started before readiness validation"))
        with pytest.raises(ValueError, match="incomplete"):
            checkout_transfer.export_checkout(path, branch)
    else:
        monkeypatch.setattr(database, "get_session", lambda _: {
            "id": worker, "project_v2_id": project["id"], "branch": branch, "worktree_path": str(path),
        })
        monkeypatch.setattr(project_resolver, "resolve_project_v2", lambda _: project)
        monkeypatch.setattr(worker_checkout, "_base_checkout_path", lambda _: str(base_checkout))
        ready, _ = worker_checkout.restore_worker_checkout(worker, follow_live_switch=False)
        assert not ready, "existing-directory shortcut accepted an unfinished checkout"
    assert _snapshot(path) == before
    assert _materializing_marker(base_checkout, path).is_file()


@pytest.mark.parametrize("previously_materialized", [False, True])
@pytest.mark.parametrize("edit", ["delete", "rename"])
def test_overlapping_resolvers_recheck_publication_before_add(
    project, base_checkout, workspace_root, monkeypatch, previously_materialized, edit,
):
    branch = "fix/overlapping-resolvers"
    _git(base_checkout, "branch", branch)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    if previously_materialized:
        assert _resolve(project, branch) == str(path)
        _git(base_checkout, "worktree", "remove", str(path))
    ready, release = threading.Event(), threading.Event()
    original_plan = checkout_manager._branch_plan
    results, errors = [], []

    def paused_plan(*args, **kwargs):
        plan = original_plan(*args, **kwargs)
        if threading.current_thread().name == "slow-resolver":
            ready.set()
            assert release.wait(5), "fast resolver did not release the slow resolver"
        return plan

    def resolve_slowly():
        try:
            results.append(_resolve(project, branch, raise_add_errors=True))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(checkout_manager, "_branch_plan", paused_plan)
    slow = threading.Thread(target=resolve_slowly, name="slow-resolver")
    slow.start()
    try:
        assert ready.wait(5), errors
        assert _resolve(project, branch, raise_add_errors=True) == str(path)
        if edit == "delete":
            (path / "README.md").unlink()
        else:
            _git(path, "mv", "README.md", "RENAMED.md")
        before = _snapshot(path)
        published = checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)
        assert published["status"] == "active"
    finally:
        release.set()
        slow.join(5)
    assert not slow.is_alive() and not errors, errors
    assert results == [str(path)]
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch) == published
    assert _resolve(project, branch, raise_add_errors=True) == str(path)
    assert _snapshot(path) == before
    assert not _materializing_marker(base_checkout, path).exists()
    assert not _failure_marker(path).exists()


@pytest.mark.parametrize("stage", ["thread", "windows", "posix"])
@pytest.mark.parametrize("tracking", ["none", "same-loop", "foreign-loop", "parent-loop"])
def test_repository_lock_wait_cancelled_without_releasing_owner(
    base_checkout, monkeypatch, stage, tracking,
):
    import asyncio
    import errno
    from types import SimpleNamespace
    from app.core import git_worktree_ops as ops
    from app.db.async_database import TurnWork, bind_turn_work, run_sync, unbind_turn_work

    if stage == "posix" and os.name == "nt":
        pytest.skip("native POSIX byte-lock contention; Windows is covered separately")
    lock = ops._RepositoryLock(base_checkout / ".git")
    held, release, entered, finished = (threading.Event() for _ in range(4))
    accepted, cancelled = [], []
    owner = None

    if stage == "windows":
        def contended(*args):
            entered.set()
            if not release.is_set():
                raise OSError(errno.EACCES, "another process holds the byte lock")

        monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=contended))
        monkeypatch.setattr(ops, "os", SimpleNamespace(name="nt"))
    else:
        owner_lock = lock if stage == "thread" else ops._RepositoryLock(base_checkout / ".git")

        def hold():
            with owner_lock:
                held.set()
                assert release.wait(5), "test failed to release the owner"

        owner = threading.Thread(target=hold)
        owner.start()
        assert held.wait(2)
        if stage == "posix":
            import fcntl
            original_flock = fcntl.flock

            def observed_flock(*args):
                try:
                    return original_flock(*args)
                except OSError:
                    entered.set()
                    raise

            monkeypatch.setattr(fcntl, "flock", observed_flock)
        else:
            original_wait = ops._wait_for_lock

            def observed_wait(*args, **kwargs):
                entered.set()
                return original_wait(*args, **kwargs)

            monkeypatch.setattr(ops, "_wait_for_lock", observed_wait)

    def wait():
        try:
            with lock:
                accepted.append(True)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        finally:
            finished.set()

    async def cancel_wait():
        try:
            # Parent cancellation must reach a nested run_sync on a foreign
            # loop too, not only the directly-awaited executor function.
            dispatch = (lambda: asyncio.run(run_sync(wait))) if tracking == "parent-loop" else wait
            task = asyncio.create_task(run_sync(dispatch))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Cancellation must end the executor work itself, not merely its
            # awaiter. Keep the live/hung owner locked throughout this assertion.
            assert finished.wait(.5), "executor waiter survived cancellation"
            assert cancelled == [True] and not accepted
            assert not release.is_set()
        finally:
            release.set()  # Baseline failures must not hang executor shutdown.

    async def exercise():
        work = TurnWork() if tracking != "none" else None
        if work:
            bind_turn_work(work)
        try:
            if tracking == "foreign-loop":
                await run_sync(lambda: asyncio.run(cancel_wait()))
            else:
                await cancel_wait()
            if work:
                await asyncio.wait_for(work.drain(), 1)
                assert not work.pending
        finally:
            if work:
                unbind_turn_work(work)

    try:
        asyncio.run(exercise())  # Also proves executor shutdown returns.
    finally:
        release.set()
        if owner:
            owner.join(2)
            assert not owner.is_alive()
    assert lock.depth == 0 and lock.file is None
    assert lock.thread_lock.acquire(blocking=False)
    lock.thread_lock.release()


@pytest.mark.parametrize("stage", ["thread", "windows", "posix"])
def test_repository_lock_deadline_is_shared_and_typed(base_checkout, monkeypatch, stage):
    import errno
    from types import SimpleNamespace
    from app.core import git_worktree_ops as ops
    from app.core.worktree_manager import WorktreeError

    if stage == "posix" and os.name == "nt":
        pytest.skip("POSIX lock implementation")
    elapsed, released = [], []
    lock = ops._RepositoryLock(base_checkout / ".git")
    # Spend half the budget in local contention, then the rest at the OS lock.
    class LocalContention:
        def acquire(self, *, blocking):
            assert not blocking
            return sum(elapsed) >= .2 if stage != "thread" else False

        def release(self):
            released.append(True)

    lock.thread_lock = LocalContention()
    monkeypatch.setattr(ops, "WORKTREE_LOCK_TIMEOUT", .4)
    monkeypatch.setattr(ops, "time", SimpleNamespace(
        monotonic=lambda: sum(elapsed), sleep=lambda seconds: elapsed.append(seconds),
    ))

    def contended(*args):
        raise OSError(errno.EACCES, "lock held by a live process")

    if stage == "windows":
        monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=contended))
        monkeypatch.setattr(ops, "os", SimpleNamespace(name="nt"))
    elif stage == "posix":
        import fcntl
        monkeypatch.setattr(fcntl, "flock", contended)
    with pytest.raises(ops.WorktreeLockError, match="Timed out waiting for the checkout lock") as error:
        lock.acquire()
    assert isinstance(error.value, WorktreeAddError)
    assert isinstance(error.value, WorktreeError) and isinstance(error.value, ValueError)
    assert sum(elapsed) == pytest.approx(.4), "OS wait reset the local wait's deadline"
    assert released == ([] if stage == "thread" else [True])
    assert lock.depth == 0 and lock.file is None


def test_checkout_lock_timeout_does_not_invalidate_published_row(
    project, base_checkout, workspace_root, monkeypatch,
):
    from types import SimpleNamespace
    from app.core import git_worktree_ops as ops

    branch = "fix/lock-timeout"
    _git(base_checkout, "branch", branch)
    path = Path(_resolve(project, branch))
    (path / "README.md").unlink()
    before, row = _snapshot(path), checkouts_db.get_checkout(project["id"], RUNNER, branch=branch)
    lock = ops.repo_worktree_lock(str(base_checkout))
    held, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            held.set()
            assert release.wait(5)

    owner = threading.Thread(target=hold)
    owner.start()
    try:
        assert held.wait(2)
        elapsed = []
        monkeypatch.setattr(ops, "WORKTREE_LOCK_TIMEOUT", .2)
        monkeypatch.setattr(ops, "time", SimpleNamespace(
            monotonic=lambda: sum(elapsed), sleep=lambda seconds: elapsed.append(seconds),
        ))
        assert _resolve(project, branch) is None
        with pytest.raises(ops.WorktreeLockError, match="checkout lock"):
            _resolve(project, branch, raise_add_errors=True)
    finally:
        release.set()
        owner.join(2)
    assert not owner.is_alive()
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch) == row
    assert _snapshot(path) == before


def test_cancellation_does_not_interrupt_nested_checkout_critical_section(base_checkout):
    import asyncio
    from app.core import git_worktree_ops as ops
    from app.db.async_database import run_sync

    lock = ops._RepositoryLock(base_checkout / ".git")
    held, release, finished = threading.Event(), threading.Event(), threading.Event()
    completed = []

    def write():
        try:
            with lock:
                held.set()
                assert release.wait(2)
                with lock:
                    completed.append(True)
        finally:
            finished.set()

    async def exercise():
        task = asyncio.create_task(run_sync(write))
        try:
            assert await asyncio.to_thread(held.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert finished.wait(1)

    asyncio.run(exercise())
    assert completed == [True]
    assert lock.depth == 0 and lock.file is None
