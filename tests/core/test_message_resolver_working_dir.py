"""
Tests for ``resolve_working_dir`` in app/core/message_resolver.py — the
auxiliary working-dir reader used by history/JSONL parsing, LiveSessionStore
seeding, and checkpoint flows.

Contract (mirrors ``_resolve_session_worktree_cwd``'s disk-based rule):
  - Worktree path is used whenever the resolved directory EXISTS on disk,
    regardless of the worktree row's ``status`` (a per-device sync sweep can
    flip a healthy row to 'deleted' without the directory moving).
  - When the row is missing, the session's backup ``worktree_path`` column is
    tried the same way.
  - Fall back to the project dir ONLY when no worktree directory exists on
    disk — never raise (this reader must never break history reads).
  - No status self-heal here (that lives in query_options only).

Real temp directories are used (absolute worktree/project paths resolve
directly through project_utils), so only the DB boundary is patched.
"""

from unittest.mock import MagicMock

import pytest

import app.core.message_resolver as mr
from app.core.message_resolver import resolve_working_dir


@pytest.fixture
def db(monkeypatch):
    """Patch the lazy-imported DB boundary (app.db.database.*)."""
    get_worktree = MagicMock(name="get_worktree", return_value=None)
    get_project = MagicMock(name="get_project", return_value=None)
    monkeypatch.setattr("app.db.database.get_worktree", get_worktree)
    monkeypatch.setattr("app.db.database.get_project", get_project)
    return type("DB", (), {"get_worktree": get_worktree, "get_project": get_project})


def _session(worktree_id="wt-1", project_id="proj-1", worktree_path=None):
    return {
        "id": "ses-1",
        "worktree_id": worktree_id,
        "project_id": project_id,
        "worktree_path": worktree_path,
    }


# ---------------------------------------------------------------------------

def test_stale_status_with_existing_dir_returns_worktree_path(db, tmp_path):
    """The false-flip regression: status='deleted' but the dir is right there."""
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": "deleted",
        "worktree_path": str(wt_dir),
    }

    assert resolve_working_dir(_session()) == str(wt_dir)
    # The project must not even be consulted when the worktree dir exists.
    db.get_project.assert_not_called()


def test_active_status_with_existing_dir_returns_worktree_path(db, tmp_path):
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": "active",
        "worktree_path": str(wt_dir),
    }

    assert resolve_working_dir(_session()) == str(wt_dir)


def test_worktree_dir_missing_falls_back_to_project(db, tmp_path):
    """Auxiliary reader: a genuinely gone worktree dir degrades to the project dir."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": "active",
        "worktree_path": str(tmp_path / "gone"),  # never created
    }
    db.get_project.return_value = {"id": "proj-1", "path": str(proj_dir)}

    assert resolve_working_dir(_session()) == str(proj_dir)


def test_missing_row_uses_session_backup_path(db, tmp_path):
    """Row hard-deleted but the session backup column still points at the dir."""
    wt_dir = tmp_path / "wt-backup"
    wt_dir.mkdir()
    db.get_worktree.return_value = None

    result = resolve_working_dir(_session(worktree_path=str(wt_dir)))

    assert result == str(wt_dir)
    db.get_project.assert_not_called()


def test_row_path_missing_but_backup_path_exists(db, tmp_path):
    """Row's path is gone; the session backup path still exists → use the backup."""
    backup_dir = tmp_path / "wt-backup"
    backup_dir.mkdir()
    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": "active",
        "worktree_path": str(tmp_path / "gone"),
    }

    result = resolve_working_dir(_session(worktree_path=str(backup_dir)))

    assert result == str(backup_dir)


def test_worktree_lookup_error_falls_back_to_project(db, tmp_path):
    """A DB/resolution failure must never break history reads — degrade to project."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db.get_worktree.side_effect = RuntimeError("db boom")
    db.get_project.return_value = {"id": "proj-1", "path": str(proj_dir)}

    assert resolve_working_dir(_session()) == str(proj_dir)


def test_worktree_lookup_error_still_tries_session_backup(db, tmp_path):
    """Regression: a get_worktree raise must NOT skip the session's backup
    worktree_path — the row lookup failing says nothing about the dir on disk."""
    backup_dir = tmp_path / "wt-backup"
    backup_dir.mkdir()
    db.get_worktree.side_effect = RuntimeError("db boom")

    result = resolve_working_dir(_session(worktree_path=str(backup_dir)))

    assert result == str(backup_dir)
    # The backup satisfied the read — the project fallback is never consulted.
    db.get_project.assert_not_called()


def test_non_worktree_session_uses_project(db, tmp_path):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db.get_project.return_value = {"id": "proj-1", "path": str(proj_dir)}

    result = resolve_working_dir(_session(worktree_id=None))

    assert result == str(proj_dir)
    db.get_worktree.assert_not_called()


def test_no_worktree_no_project_uses_default(db, monkeypatch):
    monkeypatch.setattr(mr, "_default_working_dir", lambda: "/default/workspace")

    result = resolve_working_dir(_session(worktree_id=None, project_id=None))

    assert result == "/default/workspace"
