"""Project/command path helpers resolve against the EFFECTIVE workspace.

In Docker the workspace chosen during setup is a runtime override
(``settings.effective_workspace_dir``); the start-up ``settings.workspace_dir``
is only the fallback. Every place that turns a relative project path or a
missing project into a directory must use the effective one, the same value
``project_utils`` resolves stored relative paths against.
"""
from pathlib import Path

import pytest

from app.api import command_context as context
from app.api import common
from app.api import projects as projects_module
from app.core import config as config_module
from app.core.models import ProjectCreate


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    startup = tmp_path / "startup-workspace"
    effective = tmp_path / "chosen-workspace"
    startup.mkdir()
    effective.mkdir()
    monkeypatch.setattr(config_module.settings, "workspace_dir", startup)
    monkeypatch.setitem(config_module._runtime_overrides, "workspace_dir", effective)
    assert config_module.settings.effective_workspace_dir == effective
    return startup, effective


async def _inline(fn, *a, **k):
    return fn(*a, **k)


async def test_create_project_with_relative_path_lands_in_effective_workspace(workspaces, monkeypatch):
    startup, effective = workspaces
    created = {}
    monkeypatch.setattr(projects_module, "run_sync", _inline)
    monkeypatch.setattr(projects_module.database, "get_project", lambda pid: None)
    monkeypatch.setattr(projects_module.database, "create_project",
                        lambda **kw: created.update(kw) or kw)
    await projects_module.create_project(ProjectCreate(id="demo", name="Demo"), token="t")
    assert created["path"] == "demo"
    assert (effective / "demo").is_dir()
    assert not (startup / "demo").exists()


def test_validate_project_path_confines_to_effective_workspace(workspaces):
    startup, effective = workspaces
    assert projects_module.validate_project_path("sub", allow_create=True) == (effective / "sub").resolve()
    # A relative path that climbs into the start-up workspace escapes the effective one.
    with pytest.raises(projects_module.HTTPException) as exc:
        projects_module.validate_project_path("../startup-workspace/x", allow_create=True)
    assert exc.value.status_code == 400


def test_docker_absolute_path_inside_effective_workspace_is_made_relative(workspaces, monkeypatch):
    startup, effective = workspaces
    monkeypatch.setattr(projects_module, "is_local_mode", lambda: False)
    assert projects_module.normalize_docker_path(str(effective / "proj")) == "proj"
    # The start-up workspace is not the boundary any more: left absolute, then rejected.
    assert projects_module.normalize_docker_path(str(startup / "proj")) == str(startup / "proj")


def test_served_roots_resolve_relative_projects_in_effective_workspace(workspaces, monkeypatch):
    startup, effective = workspaces
    (effective / "proj").mkdir()
    monkeypatch.setattr(common.database, "get_all_projects", lambda: [{"id": "p", "path": "proj"}])
    monkeypatch.setattr(common.database, "get_all_git_repositories", lambda: [])
    common.reset_served_roots_cache()
    try:
        served = [Path(r).resolve() for r in common._registered_serve_roots()]
        assert (effective / "proj").resolve() in served
        assert common.is_path_served(effective / "proj" / "a.png", effective)
        assert common.is_path_served(effective / "b.png", effective)
    finally:
        common.reset_served_roots_cache()


def test_resolve_project_path_falls_back_to_effective_workspace(workspaces, monkeypatch):
    _, effective = workspaces
    monkeypatch.setattr(common.database, "get_project", lambda pid: None)
    assert common.resolve_project_path(None, fallback_to_workspace=True) == effective
    assert common.resolve_project_path("gone", fallback_to_workspace=True) == effective


def test_command_context_default_cwd_is_effective_workspace(workspaces, monkeypatch):
    _, effective = workspaces
    monkeypatch.setattr(context.database, "get_profile", lambda pid: None)
    assert context.command_context(None, None, None, None) == (str(effective), None)
