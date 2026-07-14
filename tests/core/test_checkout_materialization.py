"""Tests for app/core/checkout_manager.py — per-runner lazy materialization of
project_v2 checkouts (v2 core, Phase 3).

Uses REAL tmp git repos (no git mocking) so the deterministic-path + real
``git worktree add`` behavior is exercised end to end, plus the pg_shim SQLite DB
for the ``checkouts`` rows (matching tests/db conventions). The workspace root is
redirected to ``tmp_path`` via the config runtime override so the deterministic
materialization lands under the test's temp dir.
"""

import re
import subprocess
from pathlib import Path

import pytest

from app.core import checkout_manager
from app.db import checkouts as checkouts_db
from app.db import projects_v2

RUNNER = "runnerA"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Redirect the runner's workspace root (hence checkout root) to tmp_path."""
    from app.core import config
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root)
    return root


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def _make_base_repo(base_dir) -> Path:
    """A real git repo with one commit on the default branch — the base checkout."""
    repo = Path(base_dir) / "base_repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def project(mock_db):
    return projects_v2.upsert_project("git:repo", name="My Cool Repo")


@pytest.fixture
def base_checkout(project, tmp_path):
    """A materialized base checkout: a real repo + its checkouts row (branch NULL)."""
    repo = _make_base_repo(tmp_path)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))
    return repo


# ---------------------------------------------------------------------------
# Base checkout resolution
# ---------------------------------------------------------------------------

def test_base_returns_path_when_materialized(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(project, runner_id=RUNNER)
    assert path == str(base_checkout)


def test_base_not_materialized_returns_none(project, workspace_root):
    # No checkouts row at all → not on this device → None (never clones).
    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) is None


def test_base_missing_dir_returns_none_no_recreation(project, mock_db, tmp_path, workspace_root):
    # Row exists but the on-disk dir is gone → honest None, and NOTHING recreated.
    gone = tmp_path / "never_existed"
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(gone))
    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) is None
    assert not gone.exists()


# ---------------------------------------------------------------------------
# Branch checkout materialization
# ---------------------------------------------------------------------------

def test_branch_lazy_create_deterministic_path(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(project, branch="feature-x", runner_id=RUNNER)
    assert path is not None
    p = Path(path)
    assert p.is_dir()
    assert (p / ".git").exists()

    # Deterministic path: {workspace}/checkouts/{slug}/feature-x — NOT an md5-hash dir.
    assert "checkouts" in p.parts
    assert p.name == "feature-x"
    # The old scheme suffixed a 4-hex md5 (e.g. "feature-x-a7b3"); assert absent.
    assert not re.search(r"-[0-9a-f]{4}$", p.name)
    # Parent slug is name-derived + id-suffixed (collision guard), not a path hash.
    assert p.parent.name == checkout_manager._project_slug(project)
    assert p.parent.name.startswith("my-cool-repo-")


def test_branch_idempotent_same_path_no_duplicate_rows(project, base_checkout, workspace_root):
    first = checkout_manager.ensure_checkout(project, branch="feat/login", runner_id=RUNNER)
    second = checkout_manager.ensure_checkout(project, branch="feat/login", runner_id=RUNNER)
    assert first is not None and first == second
    # base + one branch checkout = exactly two rows; the re-call adds none.
    rows = checkouts_db.list_checkouts(project["id"])
    assert len(rows) == 2
    branch_rows = [r for r in rows if r.get("branch") == "feat/login"]
    assert len(branch_rows) == 1
    assert branch_rows[0]["path"] == first


def test_branch_requires_base_checkout(project, workspace_root):
    # No base checkout materialized → a branch cannot be materialized either.
    assert checkout_manager.ensure_checkout(project, branch="feature-x", runner_id=RUNNER) is None


def test_branch_sanitizes_slashes_in_dir_name(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(project, branch="feat/deep/name", runner_id=RUNNER)
    assert path is not None
    # Slashes are sanitized to dashes in the final path component.
    assert Path(path).name == "feat-deep-name"


def test_existing_dir_not_a_worktree_returns_none(project, base_checkout, workspace_root):
    # Pre-create the deterministic dest as a plain dir (no .git) → refuse, don't destroy.
    dest = checkout_manager._branch_checkout_path(project, "feature-y")
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("keep me\n", encoding="utf-8")

    assert checkout_manager.ensure_checkout(project, branch="feature-y", runner_id=RUNNER) is None
    # The sentinel survives — nothing was torn down.
    assert (dest / "sentinel.txt").exists()


def test_per_runner_divergence_same_branch(project, base_checkout, tmp_path, workspace_root, monkeypatch):
    """A second install resolves the SAME branch to its OWN path — divergence comes
    from each install's own configured workspace root (home dirs differ per machine),
    with an independent per-runner base checkout + checkouts row."""
    path_a = checkout_manager.ensure_checkout(project, branch="shared", runner_id=RUNNER)
    assert path_a is not None

    # Install B: a different workspace root + its own base checkout clone.
    from app.core import config
    root_b = tmp_path / "workspace_b"
    root_b.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root_b)
    repo_b = _make_base_repo(tmp_path / "b")
    checkouts_db.upsert_checkout(project["id"], "runnerB", str(repo_b))

    path_b = checkout_manager.ensure_checkout(project, branch="shared", runner_id="runnerB")
    assert path_b is not None
    assert path_b != path_a
    assert "workspace_b" in path_b
