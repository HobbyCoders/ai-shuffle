"""Never bind a branch checkout left half-populated by a failed Git add."""
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


def _snapshot(path):
    """Include ignored/untracked files and Git's private worktree index."""
    index = checkout_manager.git_service._run_git(
        str(path), ["rev-parse", "--git-path", "index"], timeout=10,
    )
    index_path = Path(path) / index.stdout.strip()
    files = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return files, index_path.read_bytes() if index_path.is_file() else None


def test_failed_add_partial_checkout_is_not_adopted(
    project, base_checkout, workspace_root, caplog,
):
    branch = "fix/failed-add"
    _git(base_checkout, "branch", branch)
    # Git really fails *after* registration. Removing a file in a failing hook
    # deterministically models the incomplete checkout a failed rollback leaves.
    hook = base_checkout / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nrm -- README.md\necho 'checkout interrupted' >&2\nexit 1\n")
    hook.chmod(0o755)
    path = checkout_manager._branch_checkout_path(str(base_checkout), branch)
    with pytest.raises(WorktreeAddError, match="checkout interrupted"):
        _resolve(project, branch, raise_add_errors=True)
    assert path.is_dir() and not (path / "README.md").exists()
    assert str(path).replace("\\", "/") in checkout_manager.git_service._run_git(
        str(base_checkout), ["worktree", "list", "--porcelain"], timeout=10,
    ).stdout.replace("\\", "/")
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch) is None
    before = _snapshot(path)

    assert _resolve(project, branch) is None
    assert "incomplete" in caplog.text.lower()
    with pytest.raises(WorktreeAddError, match="incomplete"):
        _resolve(project, branch, raise_add_errors=True)
    assert _snapshot(path) == before
    assert checkouts_db.get_checkout(project["id"], RUNNER, branch=branch) is None


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
