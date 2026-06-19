"""
Unit tests for cloud-intake project resolution (``_ensure_project_for_repo``).

These cover the shared-database foreign-row bug: the cloud *primary* sends its
own ``project_id`` plus a ``repo_url`` to the *worker*, but both installs share
one Postgres database and ``database.get_project(id)`` does NOT filter by
``instance_id``. The fix resolves to a project whose working dir is actually a
git repo on THIS install (or creates+clones one) instead of blindly reusing the
foreign row.

Everything is mocked at the boundaries — these tests need no live database and
no real git.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.cloud import (
    _ensure_project_for_repo,
    _normalize_remote_url,
    _instance_suffixed_id,
)


REPO_URL = "https://github.com/HobbyCoders/ai-shuffle.git"
REPO_URL_NO_GIT = "https://github.com/HobbyCoders/ai-shuffle"

# The foreign (primary's) row that the shared-DB lookup returns: a Windows path
# that does not exist on the Linux worker, with a different instance_id.
FOREIGN_ROW = {
    "id": "ai-shuffle",
    "name": "ai-shuffle",
    "path": "D:\\Projects\\ai-shuffle",
    "instance_id": "primary-instance",
}

# This worker's own valid clone of the same repo (instance-filtered).
LOCAL_ROW = {
    "id": "ai-shuffle-43d217",
    "name": "ai-shuffle",
    "path": "ai-shuffle",  # relative -> /workspace/ai-shuffle
    "instance_id": "worker-instance",
}


@pytest.fixture
def boundaries():
    """Patch every external boundary ``_ensure_project_for_repo`` touches.

    Yields a namespace of the individual mocks so each test can program return
    values / assert calls.
    """
    with patch("app.api.cloud.database") as mock_db, \
         patch("app.core.git_service.git_service") as mock_git, \
         patch("app.api.git_helpers.ensure_git_repository_record") as mock_ensure, \
         patch("app.api.projects.validate_project_path") as mock_validate, \
         patch("app.core.project_utils.get_project_working_dir") as mock_working_dir, \
         patch("app.core.config.settings") as mock_settings:

        mock_settings.effective_instance_id = "worker-instance"
        # Resolve working dir from the project's path key (mirrors real behavior
        # closely enough for these tests).
        mock_working_dir.side_effect = lambda p: p["path"] if isinstance(p, dict) else p
        # validate_project_path returns a Path-like with mkdir().
        mock_validate.return_value = MagicMock()

        ns = MagicMock()
        ns.db = mock_db
        ns.git = mock_git
        ns.ensure = mock_ensure
        ns.validate = mock_validate
        ns.working_dir = mock_working_dir
        ns.settings = mock_settings
        yield ns


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_normalize_remote_url_strips_git_and_case():
    assert _normalize_remote_url(REPO_URL) == _normalize_remote_url(REPO_URL_NO_GIT)
    assert _normalize_remote_url("HTTPS://GitHub.com/Owner/Repo.git/") == (
        "https://github.com/owner/repo"
    )
    assert _normalize_remote_url(None) == ""
    assert _normalize_remote_url("") == ""


def test_instance_suffixed_id_is_deterministic():
    with patch("app.core.config.settings") as mock_settings:
        mock_settings.effective_instance_id = "worker-instance"
        first = _instance_suffixed_id("ai-shuffle")
        second = _instance_suffixed_id("ai-shuffle")
    assert first == second
    assert first.startswith("ai-shuffle-")
    assert len(first.split("-")[-1]) == 6


# ---------------------------------------------------------------------------
# (the bug) foreign verbatim row + instance-local clone with matching remote
# ---------------------------------------------------------------------------

def test_resolves_to_local_clone_when_verbatim_is_foreign(boundaries):
    b = boundaries
    # Verbatim id returns the primary's Windows-path row.
    b.db.get_project.return_value = FOREIGN_ROW
    # This install has its own clone discoverable via the instance-filtered scan.
    b.db.get_all_projects.return_value = [LOCAL_ROW]
    # The foreign Windows path is NOT a git repo here; the local clone IS.
    b.git.is_git_repo.side_effect = lambda wd: wd == LOCAL_ROW["path"]
    # The local clone's git record carries the matching remote.
    b.db.get_git_repository_by_project.return_value = {"remote_url": REPO_URL}

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert project["id"] == LOCAL_ROW["id"]
    assert created is False
    # The bug: it must NOT clone or create when a local clone already exists.
    b.git.clone_repo.assert_not_called()
    b.db.create_project.assert_not_called()
    # A git repo record is ensured for the resolved local project.
    b.ensure.assert_called_with(LOCAL_ROW["id"], LOCAL_ROW["path"])


def test_remote_match_is_robust_to_trailing_git(boundaries):
    """Local record stores the URL without .git; primary sends it with .git."""
    b = boundaries
    b.db.get_project.return_value = FOREIGN_ROW
    b.db.get_all_projects.return_value = [LOCAL_ROW]
    b.git.is_git_repo.side_effect = lambda wd: wd == LOCAL_ROW["path"]
    # No git_repositories record -> falls back to live get_remote_url, which
    # returns the URL WITHOUT the .git suffix.
    b.db.get_git_repository_by_project.return_value = None
    b.git.get_remote_url.return_value = REPO_URL_NO_GIT

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert project["id"] == LOCAL_ROW["id"]
    assert created is False
    b.git.clone_repo.assert_not_called()


# ---------------------------------------------------------------------------
# single-install / same-machine: verbatim id IS a git repo here
# ---------------------------------------------------------------------------

def test_verbatim_id_used_directly_when_git_repo_here(boundaries):
    b = boundaries
    local_verbatim = {
        "id": "ai-shuffle",
        "name": "ai-shuffle",
        "path": "/workspace/ai-shuffle",
        "instance_id": "worker-instance",
    }
    b.db.get_project.return_value = local_verbatim
    b.git.is_git_repo.return_value = True  # verbatim dir is a real repo here

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert project["id"] == "ai-shuffle"
    assert created is False
    b.git.clone_repo.assert_not_called()
    b.db.create_project.assert_not_called()
    # Should short-circuit before scanning all projects.
    b.db.get_all_projects.assert_not_called()
    b.ensure.assert_called_with("ai-shuffle", "/workspace/ai-shuffle")


# ---------------------------------------------------------------------------
# nothing usable: create + clone
# ---------------------------------------------------------------------------

def test_creates_and_clones_when_nothing_exists(boundaries):
    b = boundaries
    # No verbatim row, no local projects.
    b.db.get_project.return_value = None
    b.db.get_all_projects.return_value = []
    b.git.is_git_repo.return_value = False
    created_row = {"id": "ai-shuffle", "name": "ai-shuffle", "path": "ai-shuffle"}
    b.db.create_project.return_value = created_row
    b.git.clone_repo.return_value = (True, None)

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert created is True
    assert project["id"] == "ai-shuffle"
    # Fresh id (no collision) is used verbatim.
    b.db.create_project.assert_called_once()
    assert b.db.create_project.call_args.kwargs["project_id"] == "ai-shuffle"
    b.git.clone_repo.assert_called_once()


def test_creates_suffixed_id_when_verbatim_held_by_foreign_row(boundaries):
    b = boundaries
    expected_id = _instance_suffixed_id("ai-shuffle")

    def get_project(pid):
        # Verbatim id is the foreign row; the suffixed id is free.
        if pid == "ai-shuffle":
            return FOREIGN_ROW
        return None

    b.db.get_project.side_effect = get_project
    b.db.get_all_projects.return_value = []  # no local clone to reuse
    b.git.is_git_repo.return_value = False  # foreign path isn't a repo here
    created_row = {"id": expected_id, "name": "ai-shuffle", "path": expected_id}
    b.db.create_project.return_value = created_row
    b.git.clone_repo.return_value = (True, None)

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert created is True
    assert project["id"] == expected_id
    # Created with the suffixed id, NOT the foreign verbatim id.
    assert b.db.create_project.call_args.kwargs["project_id"] == expected_id
    b.git.clone_repo.assert_called_once()


def test_clone_failure_rolls_back_created_project(boundaries):
    b = boundaries
    b.db.get_project.return_value = None
    b.db.get_all_projects.return_value = []
    b.git.is_git_repo.return_value = False
    created_row = {"id": "ai-shuffle", "name": "ai-shuffle", "path": "ai-shuffle"}
    b.db.create_project.return_value = created_row
    b.git.clone_repo.return_value = (False, "fatal: repository not found")

    with pytest.raises(HTTPException) as exc:
        _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert exc.value.status_code == 400
    assert "Failed to clone repository" in exc.value.detail
    # The freshly-created row is rolled back.
    b.db.delete_project.assert_called_once_with("ai-shuffle")


def test_existing_target_row_cloned_in_place_not_recreated(boundaries):
    """If a row already sits at the target id but isn't a git repo, clone into
    its existing dir instead of calling create_project again (avoids PK clash)."""
    b = boundaries
    # Same-instance verbatim row exists but isn't a git repo (e.g. empty dir
    # from a half-finished prior attempt).
    half_done = {
        "id": "ai-shuffle",
        "name": "ai-shuffle",
        "path": "ai-shuffle",
        "instance_id": "worker-instance",  # ours, so no suffixing
    }
    b.db.get_project.return_value = half_done
    b.db.get_all_projects.return_value = []
    b.git.is_git_repo.return_value = False
    b.git.clone_repo.return_value = (True, None)

    project, created = _ensure_project_for_repo("ai-shuffle", REPO_URL, "ai-shuffle", "main")

    assert project["id"] == "ai-shuffle"
    assert created is False  # row was not freshly created
    b.db.create_project.assert_not_called()
    b.git.clone_repo.assert_called_once()
    # No rollback for an in-place clone on success.
    b.db.delete_project.assert_not_called()
