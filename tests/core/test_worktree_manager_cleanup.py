"""cleanup_worktree must not mark a worktree COMPLETED when git removal failed."""

from unittest.mock import AsyncMock

import pytest

import app.core.worktree_manager as wm_module
from app.core.worktree_manager import worktree_manager


@pytest.fixture
def cleanup_targets(mock_db, tmp_path, monkeypatch):
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    main = init_repo(tmp_path / "main")
    wt_dir = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat-x", str(wt_dir))

    wt = {
        "id": "wt-1",
        "repository_id": "repo-1",
        "branch_name": "feat-x",
        "worktree_path": str(wt_dir),
    }
    repo = {"id": "repo-1", "project_id": "p1", "default_branch": "main"}
    project = {"id": "p1", "path": str(main)}

    monkeypatch.setattr(wm_module.database, "get_worktree", lambda _id: wt)
    monkeypatch.setattr(wm_module.database, "get_git_repository", lambda _id: repo)
    monkeypatch.setattr(wm_module.database, "get_project", lambda _id: project)
    monkeypatch.setattr(wm_module.database, "update_worktree", lambda *a, **k: None)
    monkeypatch.setattr(wm_module, "get_project_working_dir", lambda _p: str(main))
    monkeypatch.setattr(wm_module, "get_worktree_working_dir", lambda _w: str(wt_dir))
    monkeypatch.setattr(
        worktree_manager,
        "terminate_worktree_sessions",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        wm_module.checkouts_db, "archive_checkout_by_path", lambda *a, **k: None
    )
    return wt_dir, main


async def test_cleanup_returns_false_when_git_remove_fails(cleanup_targets, monkeypatch):
    monkeypatch.setattr(
        worktree_manager.git_service, "remove_worktree", lambda *a, **k: False
    )
    updated = []
    monkeypatch.setattr(
        wm_module.database, "update_worktree", lambda *a, **k: updated.append(k)
    )

    ok = await worktree_manager.cleanup_worktree("wt-1", keep_branch=False)

    assert ok is False
    assert updated == []
    assert cleanup_targets[0].exists()


async def test_cleanup_prunes_when_directory_already_gone(cleanup_targets, monkeypatch):
    wt_dir, main = cleanup_targets
    import shutil

    shutil.rmtree(wt_dir)

    pruned = []
    monkeypatch.setattr(
        worktree_manager.git_service,
        "prune_worktrees",
        lambda d: pruned.append(d) or True,
    )
    monkeypatch.setattr(
        worktree_manager.git_service, "delete_branch", lambda *a, **k: True
    )
    updated = []
    monkeypatch.setattr(
        wm_module.database,
        "update_worktree",
        lambda *a, **k: updated.append(k) or None,
    )

    ok = await worktree_manager.cleanup_worktree("wt-1", keep_branch=False)

    assert ok is True
    assert pruned == [str(main)]
    assert updated
