"""#791: a checkout the app creates under ``<repo>/.fluid/worktrees/`` never
makes the main checkout look dirty, whichever path created it.

The repositories here deliberately have no ``.gitignore`` covering ``.fluid``
(our own repo ignores ``/.fluid``, which hid the bug).
"""

from pathlib import Path

import pytest

from app.core import checkout_manager, checkout_transfer
from app.db import checkouts as checkouts_db
from app.db import projects_v2
from tests.git_repo_helpers import clone, git, hermetic_git_env, init_repo, make_origin
from tests.shared_fixtures import foreign_keys_off  # noqa: F401  (autouse fixture)

RUNNER = "runnerA"
ENTRY = ".fluid/worktrees/"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.core import config
    hermetic_git_env(monkeypatch, tmp_path)
    (tmp_path / "workspace").mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    return init_repo(tmp_path / "proj", {"README.md": "hello\n"})


def _excludes(repo):
    path = repo / ".git" / "info" / "exclude"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_branch_sessions_leave_the_main_checkout_clean(mock_db, repo):
    project = projects_v2.upsert_project("git:repo", name="Repo")
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))

    paths = [checkout_manager.ensure_checkout(project, branch=branch, runner_id=RUNNER,
                                              create_new_branch=True, provision_runtime=False)
             for branch in ("feature/one", "feature/two")]

    assert all(paths), paths
    assert all(repo / ".fluid" / "worktrees" in Path(path).parents for path in paths)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _excludes(repo).count(ENTRY) == 1


def test_handoff_import_leaves_the_main_checkout_clean(repo, tmp_path):
    bare = make_origin(tmp_path, repo)
    source = clone(tmp_path, bare, "source")
    git(source, "switch", "-q", "-c", "feature/moved")
    (source / "work.txt").write_text("work\n", encoding="utf-8")
    state = checkout_transfer.export_checkout(source, "feature/moved")
    target = clone(tmp_path, bare, "target")
    dest = checkout_manager._branch_checkout_path(str(target), "feature/moved")

    imported = checkout_transfer.import_checkout(target, dest, state)

    assert imported == str(dest)
    assert (dest / "work.txt").is_file()
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""
    assert _excludes(target).count(ENTRY) == 1


def test_only_paths_under_app_worktrees_add_the_entry(repo, tmp_path):
    from app.core.git_worktree_ops import exclude_app_worktrees
    exclude_app_worktrees(str(repo), str(tmp_path / "elsewhere"))
    exclude_app_worktrees(str(repo), str(repo / ".fluid"))
    assert ENTRY not in _excludes(repo)
    exclude_app_worktrees(str(repo), str(repo / ".fluid" / "worktrees" / "x"))
    assert _excludes(repo).count(ENTRY) == 1
