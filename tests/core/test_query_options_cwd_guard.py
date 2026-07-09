"""
Regression tests: build_options_from_profile fails fast on a nonexistent cwd.

An invalid working directory otherwise survives until the SDK spawns the CLI
subprocess, where it dies as an opaque OS-level error (WinError 267 "The
directory name is invalid" on Windows). Shared-DB installs can carry project
rows whose paths only exist on another machine, so the resolved path must be
validated up front with an error that names the path and its source.

The default workspace directory is the app's own fallback and is created on
demand instead of erroring.

build_options_from_profile makes one unavoidable DB call
(get_security_instructions); we patch that single boundary.
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo


def _profile(config=None):
    cfg = {"model": "opus", "tools_preset": "claude_code"}
    if config:
        cfg.update(config)
    return {"id": "t", "config": cfg}


def test_nonexistent_project_path_raises_with_path_and_name(tmp_path):
    missing = tmp_path / "gone-project"
    project = {"id": "p1", "name": "other-install", "path": str(missing)}
    with patch.object(qo, "get_security_instructions", return_value=""):
        with pytest.raises(ValueError) as exc_info:
            qo.build_options_from_profile(profile=_profile(), project=project)
    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "other-install" in msg


def test_nonexistent_project_rebinds_to_local_sibling(tmp_path):
    """Cross-device rebind: a plain (non-worktree) session whose project path is
    foreign runs transparently in this install's clone of the same repo, and the
    rebound (local) project id — not the stale foreign one — flows into the
    builtin tools server (guarding the `project = local_project` reassignment)."""
    missing = tmp_path / "foreign-gone"
    local_clone = tmp_path / "local-clone"
    local_clone.mkdir()
    project = {"id": "p-foreign", "name": "MyRepo", "path": str(missing)}
    local_project = {"id": "p-local", "name": "MyRepo", "path": str(local_clone)}

    captured = {}

    def _fake_builtins(**kwargs):
        captured.update(kwargs)
        return None

    with patch.object(qo, "get_security_instructions", return_value=""), patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=(project, local_project),
    ), patch(
        "app.mcp.builtin_tools.create_builtin_tools_server",
        side_effect=_fake_builtins,
    ):
        opts, _ = qo.build_options_from_profile(profile=_profile(), project=project)

    # cwd rebound onto the local clone; the foreign path never reaches the SDK.
    assert str(opts.cwd) == str(local_clone)
    # The rebound project id (not the foreign one) is what downstream tools see.
    assert captured.get("project_id") == "p-local"


def test_worktree_rebind_refreshes_in_memory_project(tmp_path):
    """follow-up #3: a cross-device WORKTREE rebind moved the session onto this
    install's local project row (persisted in the DB). build_options must adopt
    that row in-memory too, so the builtin tools server sees the LOCAL project id
    on THIS send — not the foreign one, which previously leaked for one send and
    only self-healed on the next reload."""
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    foreign = {"id": "p-foreign", "name": "MyRepo", "path": str(tmp_path / "foreign")}
    local_project = {"id": "p-local", "name": "MyRepo", "path": str(tmp_path / "local")}

    captured = {}

    def _fake_builtins(**kwargs):
        captured.update(kwargs)
        return None

    def _fake_resolve(session_id, project):
        # Simulate _rebind_worktree_cwd_cross_device surfacing the local project as
        # the explicit 4th return value (no longer smuggled through worktree_info).
        return str(wt_dir), "worktree", {
            "branch": "feat/x", "base_branch": "main",
        }, local_project

    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo, "_resolve_session_worktree_cwd", side_effect=_fake_resolve
    ), patch(
        "app.mcp.builtin_tools.create_builtin_tools_server",
        side_effect=_fake_builtins,
    ):
        opts, _ = qo.build_options_from_profile(
            profile=_profile(), project=foreign, session_id="ses-1"
        )

    assert str(opts.cwd) == str(wt_dir)
    # The rebound (local) project id — not the foreign one — reaches downstream.
    assert captured.get("project_id") == "p-local"


def test_nonexistent_profile_config_cwd_raises(tmp_path):
    missing = tmp_path / "gone-cwd"
    with patch.object(qo, "get_security_instructions", return_value=""):
        with pytest.raises(ValueError) as exc_info:
            qo.build_options_from_profile(profile=_profile({"cwd": str(missing)}))
    assert str(missing) in str(exc_info.value)


def test_valid_profile_config_cwd_accepted(tmp_path):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=_profile({"cwd": str(tmp_path)}))
    assert str(opts.cwd) == str(tmp_path)


def test_missing_default_workspace_is_created(tmp_path, monkeypatch):
    ws = tmp_path / "fresh-workspace"
    assert not ws.exists()
    monkeypatch.setattr(qo.settings, "workspace_dir", ws)
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=_profile())
    assert ws.is_dir()
    assert str(opts.cwd) == str(ws)
