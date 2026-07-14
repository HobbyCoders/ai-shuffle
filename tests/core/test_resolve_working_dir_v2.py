"""Tests for the V2-projects cutover seam in ``resolve_working_dir``
(app/core/message_resolver.py).

When ``settings.v2_projects`` is ON and the session is bound to a ``project_v2_id``,
the origin-keyed ``project_resolver.resolve_existing_cwd`` is the single cwd
authority. This is an auxiliary READ path (history/JSONL), so it uses the
PURE-READ ``resolve_existing_cwd`` (never materializes — no git worktree add /
clone), NOT the spawn-path ``resolve_cwd``:
  - resolver returns a path  → that path wins (legacy walk never runs).
  - resolver returns ``None`` → the DEFAULT workspace, NEVER the legacy project
    path (an unmaterialized checkout must surface honestly).
Sessions without ``project_v2_id``, or with the flag OFF, take the unchanged
legacy worktree→project→default chain.
"""

from unittest.mock import MagicMock

import pytest

import app.core.message_resolver as mr
from app.core.config import settings
from app.core.message_resolver import resolve_working_dir


@pytest.fixture
def db(monkeypatch):
    get_worktree = MagicMock(name="get_worktree", return_value=None)
    get_project = MagicMock(name="get_project", return_value=None)
    monkeypatch.setattr("app.db.database.get_worktree", get_worktree)
    monkeypatch.setattr("app.db.database.get_project", get_project)
    return type("DB", (), {"get_worktree": get_worktree, "get_project": get_project})


@pytest.fixture
def resolver(monkeypatch):
    """Stub the PURE-READ project_resolver.resolve_existing_cwd (the read path
    NEVER materializes), patched where it is imported from."""
    import app.core.project_resolver as pr
    resolve_existing_cwd = MagicMock(name="resolve_existing_cwd", return_value=None)
    monkeypatch.setattr(pr, "resolve_existing_cwd", resolve_existing_cwd)
    return resolve_existing_cwd


@pytest.fixture(autouse=True)
def _default_dir(monkeypatch):
    monkeypatch.setattr(mr, "_default_working_dir", lambda: "/default/workspace")


def _session(project_v2_id="pv2-abc", project_id="proj-1", worktree_id=None):
    return {
        "id": "ses-1",
        "worktree_id": worktree_id,
        "project_id": project_id,
        "project_v2_id": project_v2_id,
        "worktree_path": None,
        "branch": None,
    }


def test_flag_on_and_v2_bound_existing_uses_resolver(db, resolver, monkeypatch):
    monkeypatch.setattr(settings, "v2_projects", True)
    resolver.return_value = "/runner/checkouts/proj/base"

    result = resolve_working_dir(_session())

    assert result == "/runner/checkouts/proj/base"
    # PURE READ: the read path consults resolve_existing_cwd, never resolve_cwd
    # (which would materialize a worktree on a history read).
    resolver.assert_called_once()
    # The legacy chain is never consulted for a resolvable v2 session.
    db.get_worktree.assert_not_called()
    db.get_project.assert_not_called()


def test_flag_on_resolver_none_uses_default_not_legacy_project(db, resolver, monkeypatch):
    monkeypatch.setattr(settings, "v2_projects", True)
    resolver.return_value = None
    # A legacy project row exists, but it must NOT be used — the honest fallback
    # is the default workspace so an unmaterialized checkout surfaces.
    db.get_project.return_value = {"id": "proj-1", "path": "/legacy/proj/path"}

    result = resolve_working_dir(_session())

    assert result == "/default/workspace"
    db.get_project.assert_not_called()


def test_flag_off_takes_legacy_chain(db, resolver, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "v2_projects", False)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db.get_project.return_value = {"id": "proj-1", "path": str(proj_dir)}

    result = resolve_working_dir(_session())

    assert result == str(proj_dir)
    resolver.assert_not_called()


def test_no_project_v2_id_takes_legacy_chain(db, resolver, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "v2_projects", True)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db.get_project.return_value = {"id": "proj-1", "path": str(proj_dir)}

    result = resolve_working_dir(_session(project_v2_id=None))

    assert result == str(proj_dir)
    resolver.assert_not_called()
