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
    cfg = {"model": "claude-opus-4-8", "tools_preset": "claude_code"}
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
