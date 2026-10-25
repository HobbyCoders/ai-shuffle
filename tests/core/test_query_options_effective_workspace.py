"""build_options_from_profile's default working directory is the EFFECTIVE workspace.

In Docker the workspace chosen during setup is a runtime override; the start-up
``settings.workspace_dir`` must not be used as the default cwd.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import config as config_module
from app.core.session_modes import CALLER_KEY, CALLER_PREVIEW
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


@pytest.mark.parametrize("mode", [None, "code", "chat"])
@pytest.mark.parametrize(("caller", "folder"), [
    (None, "no-project"),  # the owner's own chat (#641)
    (CALLER_PREVIEW, "chat-uploads"),  # a preview send keeps the ceiling
])
def test_orphan_owner_session_uses_chat_cwd_under_effective_workspace(
    effective: Path, mode: str | None, caller: str | None, folder: str,
) -> None:
    session = {"id": "ses-1", "project_v2_id": None, "branch": None, "mode": mode}
    overrides = {CALLER_KEY: caller} if caller else None
    with patch.object(qo, "get_security_instructions", return_value=""), patch.object(
        qo.database, "get_session", return_value=session
    ), patch("app.core.project_resolver.resolve_cwd", return_value=None), patch(
        "app.core.project_resolver.resolve_project_v2", return_value=None
    ):
        opts, _ = qo.build_options_from_profile(
            profile=query_profile(), session_id="ses-1", overrides=overrides)
    # #553: a legacy spelling cannot bypass the no-project cwd/ceiling; both
    # folders sit under the effective workspace, never the start-up one.
    assert str(opts.cwd) == str(effective / folder)
