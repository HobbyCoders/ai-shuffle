"""
Unit tests for the non-worktree cloud path's branch handling — the
"Cloud = Local on another device" rework.

Covers:
  * ``_switch_or_create_branch_sync`` — putting the remote's MAIN checkout on the
    requested branch, mirroring the local checkout endpoint's dirty-tree 409 +
    stash behavior (``app/api/git.py``).
  * ``_find_local_project_for_repo`` — the read-only project resolver shared with
    the cross-install git-state query (never clones).

Everything is mocked at the git/database boundaries — no live git or DB.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.cloud import (
    _switch_or_create_branch_sync,
    _find_local_project_for_repo,
)


WD = "/workspace/ai-shuffle"
DIRTY_MSG = "Cannot checkout: you have uncommitted changes. Commit or stash them first."


@pytest.fixture
def git():
    """Patch the git_service used by the branch-switch helper."""
    with patch("app.core.git_service.git_service") as mock_git:
        # Sensible defaults: clean tree, ops succeed.
        mock_git.get_current_branch.return_value = "main"
        mock_git.get_status.return_value = {"staged": [], "modified": [], "conflicts": []}
        mock_git.checkout.return_value = True
        mock_git.create_branch.return_value = True
        mock_git.stash_push.return_value = True
        yield mock_git


# ---------------------------------------------------------------------------
# No-op cases (no checkout → no dirty-tree concern)
# ---------------------------------------------------------------------------

def test_noop_when_branch_equals_current(git):
    git.get_current_branch.return_value = "main"
    result = _switch_or_create_branch_sync(
        WD, branch_name="main", base_branch=None, create_new_branch=False, auto_stash=False
    )
    assert result == "main"
    git.checkout.assert_not_called()
    git.get_status.assert_not_called()  # never even checks the tree


def test_noop_when_branch_empty(git):
    """Empty branch_name = 'run on whatever is checked out' (the default the
    primary preselects from the remote's current branch)."""
    git.get_current_branch.return_value = "feature-x"
    result = _switch_or_create_branch_sync(
        WD, branch_name="", base_branch=None, create_new_branch=False, auto_stash=False
    )
    assert result == "feature-x"
    git.checkout.assert_not_called()


# ---------------------------------------------------------------------------
# Switching to an existing branch
# ---------------------------------------------------------------------------

def test_checkout_existing_branch_when_clean(git):
    git.get_current_branch.return_value = "main"
    result = _switch_or_create_branch_sync(
        WD, branch_name="develop", base_branch=None, create_new_branch=False, auto_stash=False
    )
    assert result == "develop"
    git.checkout.assert_called_once_with(WD, "develop")
    git.create_branch.assert_not_called()


def test_dirty_tree_blocks_with_409(git):
    git.get_current_branch.return_value = "main"
    git.get_status.return_value = {"staged": [], "modified": ["app/foo.py"], "conflicts": []}
    with pytest.raises(HTTPException) as exc:
        _switch_or_create_branch_sync(
            WD, branch_name="develop", base_branch=None, create_new_branch=False, auto_stash=False
        )
    assert exc.value.status_code == 409
    # Exact message the frontend's 409 detection / stash-retry relies on.
    assert exc.value.detail == DIRTY_MSG
    git.checkout.assert_not_called()
    git.stash_push.assert_not_called()


def test_dirty_tree_auto_stash_then_checkout(git):
    git.get_current_branch.return_value = "main"
    git.get_status.return_value = {"staged": ["x"], "modified": [], "conflicts": []}
    result = _switch_or_create_branch_sync(
        WD, branch_name="develop", base_branch=None, create_new_branch=False, auto_stash=True
    )
    assert result == "develop"
    git.stash_push.assert_called_once()
    git.checkout.assert_called_once_with(WD, "develop")


def test_auto_stash_failure_is_409(git):
    git.get_current_branch.return_value = "main"
    git.get_status.return_value = {"staged": [], "modified": ["x"], "conflicts": []}
    git.stash_push.return_value = False
    with pytest.raises(HTTPException) as exc:
        _switch_or_create_branch_sync(
            WD, branch_name="develop", base_branch=None, create_new_branch=True, auto_stash=True
        )
    assert exc.value.status_code == 409
    git.checkout.assert_not_called()


def test_untracked_files_do_not_block(git):
    """Untracked files alone (no staged/modified/conflicts) must not 409 —
    matches the local checkout endpoint guard."""
    git.get_current_branch.return_value = "main"
    git.get_status.return_value = {
        "staged": [], "modified": [], "conflicts": [], "untracked": ["new.txt"],
    }
    result = _switch_or_create_branch_sync(
        WD, branch_name="develop", base_branch=None, create_new_branch=False, auto_stash=False
    )
    assert result == "develop"
    git.checkout.assert_called_once_with(WD, "develop")


# ---------------------------------------------------------------------------
# Creating a new branch
# ---------------------------------------------------------------------------

def test_create_new_branch_from_base_then_checkout(git):
    git.get_current_branch.return_value = "main"
    result = _switch_or_create_branch_sync(
        WD, branch_name="feat/new", base_branch="develop", create_new_branch=True, auto_stash=False
    )
    assert result == "feat/new"
    git.create_branch.assert_called_once_with(WD, "feat/new", start_point="develop")
    git.checkout.assert_called_once_with(WD, "feat/new")


def test_create_new_branch_blank_base_uses_head(git):
    """No base pinned → start_point None → fork from current HEAD."""
    git.get_current_branch.return_value = "main"
    _switch_or_create_branch_sync(
        WD, branch_name="feat/new", base_branch="  ", create_new_branch=True, auto_stash=False
    )
    git.create_branch.assert_called_once_with(WD, "feat/new", start_point=None)


def test_create_new_branch_failure_is_400(git):
    git.get_current_branch.return_value = "main"
    git.create_branch.return_value = False
    with pytest.raises(HTTPException) as exc:
        _switch_or_create_branch_sync(
            WD, branch_name="feat/dup", base_branch=None, create_new_branch=True, auto_stash=False
        )
    assert exc.value.status_code == 400
    git.checkout.assert_not_called()


def test_checkout_failure_is_400(git):
    git.get_current_branch.return_value = "main"
    git.checkout.return_value = False
    with pytest.raises(HTTPException) as exc:
        _switch_or_create_branch_sync(
            WD, branch_name="ghost", base_branch=None, create_new_branch=False, auto_stash=False
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# _find_local_project_for_repo — read-only, never clones
# ---------------------------------------------------------------------------

def test_find_local_returns_none_without_cloning_when_absent():
    """When neither the verbatim id nor any local clone matches, the resolver
    returns None and performs no clone/create (the git-state query relies on
    this to report present:false safely)."""
    with patch("app.api.cloud.database") as mock_db, \
         patch("app.core.git_service.git_service") as mock_git, \
         patch("app.core.project_utils.get_project_working_dir") as mock_wd:
        mock_wd.side_effect = lambda p: p["path"] if isinstance(p, dict) else p
        mock_db.get_project.return_value = None      # no verbatim row
        mock_db.get_all_projects.return_value = []    # no local clones
        mock_git.is_git_repo.return_value = False

        result = _find_local_project_for_repo("ai-shuffle", "https://github.com/o/r.git")

        assert result is None
        mock_git.clone_repo.assert_not_called()
        mock_db.create_project.assert_not_called()


def test_find_local_matches_clone_by_remote():
    """A local clone whose remote matches is returned (no clone)."""
    local = {"id": "ai-shuffle-abc123", "name": "ai-shuffle", "path": "/workspace/ai-shuffle"}
    with patch("app.api.cloud.database") as mock_db, \
         patch("app.core.git_service.git_service") as mock_git, \
         patch("app.api.git_helpers.ensure_git_repository_record"), \
         patch("app.core.project_utils.get_project_working_dir") as mock_wd:
        mock_wd.side_effect = lambda p: p["path"] if isinstance(p, dict) else p
        mock_db.get_project.return_value = None
        mock_db.get_all_projects.return_value = [local]
        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = {
            "remote_url": "https://github.com/o/r"
        }

        result = _find_local_project_for_repo("ai-shuffle", "https://github.com/o/r.git")

        assert result is not None
        assert result["id"] == "ai-shuffle-abc123"
        mock_git.clone_repo.assert_not_called()
