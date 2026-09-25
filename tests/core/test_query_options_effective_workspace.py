"""build_options_from_profile's default working directory is the EFFECTIVE workspace.

In Docker the workspace chosen during setup is a runtime override; the start-up
``settings.workspace_dir`` must not be used as the default cwd.
"""
from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import config as config_module
from tests.core.helpers import query_profile


@pytest.fixture
def effective(tmp_path, monkeypatch):
    startup = tmp_path / "startup-workspace"
    chosen = tmp_path / "chosen-workspace"
    startup.mkdir()
    chosen.mkdir()
    monkeypatch.setattr(qo.settings, "workspace_dir", startup)
    monkeypatch.setitem(config_module._runtime_overrides, "workspace_dir", chosen)
    return chosen


def test_no_session_no_project_defaults_to_effective_workspace(effective):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=query_profile())
    assert str(opts.cwd) == str(effective)


def test_orphan_session_defaults_to_effective_workspace(effective):
    session = {"id": "ses-1", "project_v2_id": None, "branch": None}
    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch("app.core.project_resolver.resolve_cwd", return_value=None), patch(
        "app.core.project_resolver.resolve_project_v2", return_value=None
    ):
        opts, _ = qo.build_options_from_profile(profile=query_profile(), session_id="ses-1")
    assert str(opts.cwd) == str(effective)
