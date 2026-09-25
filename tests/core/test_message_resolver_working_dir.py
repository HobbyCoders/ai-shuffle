"""
Tests for ``resolve_working_dir`` in app/core/message_resolver.py — the
auxiliary working-dir reader used by history/JSONL parsing, LiveSessionStore
seeding, and checkpoint flows.

Contract (PR-B, resolver-first): the origin resolver is the SINGLE cwd
authority, so this reader defers to ``project_resolver.resolve_existing_cwd``
unconditionally — no legacy worktree/project walk, no ``project_v2_id`` gate. It
is a PURE READ (never materializes) and degrades to the default workspace on a
miss OR any resolver error, because it must never break history reads.
"""

from unittest.mock import patch

import app.core.message_resolver as mr
from app.core.message_resolver import resolve_working_dir


def _session(**over):
    s = {"id": "ses-1", "project_v2_id": "pv2-1", "project_id": "proj-1"}
    s.update(over)
    return s


def test_resolver_path_is_returned(tmp_path):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    with patch(
        "app.core.project_resolver.resolve_existing_cwd", return_value=str(cwd)
    ) as rc:
        assert resolve_working_dir(_session()) == str(cwd)
    rc.assert_called_once()


def test_resolver_none_falls_back_to_default_workspace(monkeypatch):
    monkeypatch.setattr(mr, "_default_working_dir", lambda: "/default/workspace")
    with patch("app.core.project_resolver.resolve_existing_cwd", return_value=None):
        assert resolve_working_dir(_session()) == "/default/workspace"


def test_resolver_first_even_without_project_v2_id(tmp_path):
    """resolver-first: no ``project_v2_id`` gate — a pre-cutover session (only a
    legacy ``project_id``) still goes through the resolver, which self-heals its
    binding before returning the checkout path."""
    cwd = tmp_path / "legacy-checkout"
    cwd.mkdir()
    session = _session(project_v2_id=None)
    with patch(
        "app.core.project_resolver.resolve_existing_cwd", return_value=str(cwd)
    ) as rc:
        assert resolve_working_dir(session) == str(cwd)
    rc.assert_called_once_with(session)


def test_resolver_error_degrades_to_workspace(monkeypatch):
    """An auxiliary reader must never break history reads: a resolver error
    degrades to the default workspace rather than propagating."""
    monkeypatch.setattr(mr, "_default_working_dir", lambda: "/default/workspace")
    with patch(
        "app.core.project_resolver.resolve_existing_cwd",
        side_effect=RuntimeError("db boom"),
    ):
        assert resolve_working_dir(_session()) == "/default/workspace"


def test_default_working_dir_is_effective_workspace(tmp_path, monkeypatch):
    """The fallback is the workspace chosen during setup (runtime override),
    not the start-up ``settings.workspace_dir``."""
    from app.core import config as config_module

    startup, chosen = tmp_path / "startup-workspace", tmp_path / "chosen-workspace"
    monkeypatch.setattr(config_module.settings, "workspace_dir", startup)
    monkeypatch.setitem(config_module._runtime_overrides, "workspace_dir", chosen)
    with patch("app.core.project_resolver.resolve_existing_cwd", return_value=None):
        assert resolve_working_dir(_session()) == str(chosen)
