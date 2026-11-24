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


def test_stale_default_branch_is_refreshed(mock_db, tmp_path):
    """A record holding the old "main" guess follows the repository's real default."""
    from app.core.git_cache import _git_cache
    def run(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    origin, seed, repo = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "repo"
    run("init", "--bare", "-b", "trunk", str(origin))
    run("init", "-b", "trunk", str(seed))
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-qm", "first", cwd=seed)
    run("push", str(origin), "trunk", cwd=seed)
    run("clone", "-q", str(origin), str(repo))  # records origin/HEAD -> origin/trunk
    project_id = "trunk-default"
    database.create_project(project_id, "Example", "", str(repo), {})
    git_helpers._repo_record_cache.pop(project_id, None)
    first = git_helpers.ensure_git_repository_record(project_id, str(repo))
    database.update_git_repository(first["id"], default_branch="main")

    git_helpers._repo_record_cache.pop(project_id, None)
    _git_cache.invalidate_prefix("default_branch:")
    refreshed = git_helpers.ensure_git_repository_record(project_id, str(repo))
    assert refreshed["id"] == first["id"]
    assert refreshed["default_branch"] == "trunk"


def test_a_guess_never_replaces_a_confirmed_default(mock_db, tmp_path):
    """S4: same URL, no origin/HEAD, a local master: the stored trunk stays."""
    from app.core.git_cache import _git_cache
    def run(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    run("init", "--bare", "-b", "trunk", str(origin))
    run("init", "-b", "master", str(repo))
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-qm", "first", cwd=repo)
    run("remote", "add", "origin", str(origin), cwd=repo)
    project_id = "confirmed-trunk"
    database.create_project(project_id, "Example", "", str(repo), {})
    git_helpers._repo_record_cache.pop(project_id, None)
    _git_cache.invalidate_prefix("default_branch:")
    first = git_helpers.ensure_git_repository_record(project_id, str(repo))
    database.update_git_repository(first["id"], default_branch="trunk")

    git_helpers._repo_record_cache.pop(project_id, None)
    refreshed = git_helpers.ensure_git_repository_record(project_id, str(repo))
    assert refreshed["default_branch"] == "trunk"


def test_unknown_default_is_reported_as_unknown(mock_db, tmp_path):
    """No origin/HEAD and no main/master: status says nothing, not a made-up "main"."""
    def run(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    repo = tmp_path / "repo"
    run("init", "-b", "feature/wip", str(repo))
    run("remote", "add", "origin", "https://github.com/example/project.git", cwd=repo)
    project_id = "unknown-default"
    database.create_project(project_id, "Example", "", str(repo), {})
    git_helpers._repo_record_cache.pop(project_id, None)
    record = git_helpers.ensure_git_repository_record(project_id, str(repo))
    assert record["default_branch"] is None


def test_a_new_url_does_not_inherit_the_old_default(mock_db, tmp_path):
    """S4 r3: switching origin to a repository whose default is unknown clears the old trunk."""
    def run(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    repo = tmp_path / "repo"
    run("init", "-b", "feature/wip", str(repo))
    # Local paths that don't exist: the URL change asks the new origin, offline here.
    old_url, new_url = str(tmp_path / "old.git"), str(tmp_path / "new.git")
    run("remote", "add", "origin", old_url, cwd=repo)
    project_id = "switched-url"
    database.create_project(project_id, "Example", "", str(repo), {})
    git_helpers._repo_record_cache.pop(project_id, None)
    first = git_helpers.ensure_git_repository_record(project_id, str(repo))
    database.update_git_repository(first["id"], default_branch="trunk")

    run("remote", "set-url", "origin", new_url, cwd=repo)
    git_helpers._repo_record_cache.pop(project_id, None)
    refreshed = git_helpers.ensure_git_repository_record(project_id, str(repo))
    assert refreshed["remote_url"] == new_url
    assert refreshed["default_branch"] is None
