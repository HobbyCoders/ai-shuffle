"""#790: a project's first git_repositories row is created once, without error,
even when two Git status loads race to create it."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from app.api import git_helpers
from app.db import database as db

PROJECT = "race-project"


@pytest.fixture
def project(mock_db):
    db.create_project(PROJECT, "Race Project", "", "/path/to/race", {})
    git_helpers._repo_record_cache.pop(PROJECT, None)
    yield PROJECT
    git_helpers._repo_record_cache.pop(PROJECT, None)


def _row_count(mock_db):
    return mock_db.execute(
        "SELECT count(*) FROM git_repositories WHERE project_id = ?", (PROJECT,)
    ).fetchone()[0]


def test_second_create_returns_the_existing_row(mock_db, project):
    first = db.create_git_repository("repo-first", project, default_branch="main")
    second = db.create_git_repository("repo-second", project, default_branch="dev")

    assert first["id"] == "repo-first"
    assert second["id"] == "repo-first"
    assert second["default_branch"] == "main"
    assert _row_count(mock_db) == 1


def test_concurrent_first_status_loads_share_one_row(mock_db, project):
    """Both callers pass the existence check before either inserts."""
    git = MagicMock()
    git.is_git_repo.return_value = True
    git.get_remote_url.return_value = "https://github.com/owner/repo.git"
    git.get_default_branch.return_value = "main"
    both_checked = threading.Barrier(2, timeout=10)
    real_lookup = db.get_git_repository_by_project

    def lookup_then_wait(project_id):
        row = real_lookup(project_id)
        both_checked.wait()
        return row

    def first_load(_):
        git_helpers._repo_record_cache.pop(PROJECT, None)
        return git_helpers.ensure_git_repository_record(PROJECT, "/path/to/race")

    with (patch.object(git_helpers, "git_service", git),
          patch.object(git_helpers.database, "get_git_repository_by_project",
                       side_effect=lookup_then_wait),
          ThreadPoolExecutor(max_workers=2) as pool):
        results = list(pool.map(first_load, range(2)))

    assert all(results), results
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["github_repo_name"] == "owner/repo"
    assert _row_count(mock_db) == 1
