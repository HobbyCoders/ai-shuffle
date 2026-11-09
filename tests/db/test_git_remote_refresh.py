"""A known local repository can acquire its shared identity on a later refresh."""
import subprocess

from app.api import git_helpers
from app.core.project_links import auto_link_group_id
from app.db import database


def test_remote_added_after_first_status_links_project(mock_db, tmp_path):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    project_id = "remote-added-later"
    database.create_project(project_id, "Example", "", str(tmp_path), {})
    git_helpers._repo_record_cache.pop(project_id, None)
    first = git_helpers.ensure_git_repository_record(project_id, str(tmp_path))
    assert not first["remote_url"]
    assert not database.get_project(project_id)["link_group_id"]

    remote = "https://github.com/example/project.git"
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=tmp_path, check=True)
    # Simulate the next cache-expired status refresh, not a database edit.
    git_helpers._repo_record_cache.pop(project_id, None)
    refreshed = git_helpers.ensure_git_repository_record(project_id, str(tmp_path))
    assert refreshed["id"] == first["id"]
    assert refreshed["remote_url"] == remote
    assert database.get_project(project_id)["link_group_id"] == auto_link_group_id(remote)
