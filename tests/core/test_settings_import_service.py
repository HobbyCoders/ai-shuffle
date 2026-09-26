from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from app.core import settings_import_service as service


def _symlinks_supported() -> bool:
    """Probe whether this process can create symlinks.

    On Windows, os.symlink raises OSError (WinError 1314) unless Developer Mode
    is on or the process is elevated. Detect the capability by actually trying to
    create one in a throwaway temp dir so the symlink-dependent tests skip with a
    clear reason on unprivileged runners instead of failing (issue #620).
    """
    try:
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, "probe_link")
            os.symlink(os.path.join(d, "probe_target"), link)
            os.remove(link)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


_SYMLINKS_SUPPORTED = _symlinks_supported()
_requires_symlinks = pytest.mark.skipif(
    not _SYMLINKS_SUPPORTED,
    reason="symlink creation requires Developer Mode or admin privileges on this platform",
)


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def import_homes(tmp_path, monkeypatch):
    source = tmp_path / "macOS source home with spaces"
    destination = tmp_path / "Linux destination home"
    workspace = tmp_path / "workspace"
    source.mkdir()
    destination.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    monkeypatch.setattr(service, "is_local_mode", lambda: True)
    monkeypatch.setattr(service.settings, "workspace_dir", workspace)
    monkeypatch.setattr(service.database, "get_all_projects", list)
    monkeypatch.setattr(service.database, "get_project", lambda _project_id: None)
    return source, destination


def test_different_provider_instructions_are_concatenated(import_homes) -> None:
    source, destination = import_homes
    codex = _write(source / ".codex" / "AGENTS.md", "codex rules")
    claude = _write(source / ".claude" / "CLAUDE.md", "claude rules")

    preview = service.preview_import(source_home=str(source))
    instructions = [item for item in preview["items"] if "global_instruction" in item["kind"]]

    assert [item["source"] for item in instructions] == [str(claude), str(codex)]
    assert [item["status"] for item in instructions] == ["ready", "duplicate"]
    combined = instructions[0]
    assert combined["destination"] == str(destination / ".fluid" / "AGENTS.md")
    assert preview["source_untouched"] is True
    assert preview["overwrite_existing"] is False
    assert {category["id"] for category in preview["categories"]} >= {"global_instructions"}
    assert not (destination / ".fluid").exists()

    result = service.apply_import(
        token=preview["token"],
        selected_item_ids=[combined["id"]],
        source_home=str(source),
    )

    agents = destination / ".fluid" / "AGENTS.md"
    assert agents.read_text(encoding="utf-8") == (
        f"# From: {claude}\nclaude rules\n\n# From: {codex}\ncodex rules\n"
    )
    assert codex.read_text() == "codex rules"
    assert claude.read_text() == "claude rules"
    assert len(result["imported"]) == 1


def test_different_provider_instructions_become_review_sidecars(import_homes) -> None:
    source, destination = import_homes
    codex = _write(source / ".codex" / "AGENTS.md", "codex rules")
    claude = _write(source / ".claude" / "CLAUDE.md", "claude rules")
    native = _write(destination / ".fluid" / "AGENTS.md", "native rules")

    preview = service.preview_import(source_home=str(source))
    instructions = [item for item in preview["items"] if "global_instruction" in item["kind"]]

    assert [item["source"] for item in instructions] == [str(claude), str(codex)]
    assert [item["status"] for item in instructions] == ["ready", "ready"]
    assert all("imports" in item["destination"] for item in instructions)
    assert native.read_text() == "native rules"
    assert preview["source_untouched"] is True
    assert preview["overwrite_existing"] is False

    result = service.apply_import(
        token=preview["token"],
        selected_item_ids=[instructions[0]["id"]],
        source_home=str(source),
    )

    assert native.read_text() == "native rules"
    assert Path(instructions[0]["destination"]).read_text() == "claude rules"
    assert codex.read_text() == "codex rules"
    assert claude.read_text() == "claude rules"
    # Sidecar writes are not activations; Fluid reports them skipped.
    assert result["imported"] == []
    assert len(result["skipped"]) == 1


def test_apply_rejects_stale_preview(import_homes) -> None:
    source, _ = import_homes
    instruction = _write(source / ".codex" / "AGENTS.md", "before")
    preview = service.preview_import(source_home=str(source), providers=["codex"])
    selected = next(item["id"] for item in preview["items"] if item["status"] == "ready")
    instruction.write_text("after", encoding="utf-8")

    with pytest.raises(service.StaleImportPlan):
        service.apply_import(
            token=preview["token"],
            selected_item_ids=[selected],
            source_home=str(source),
            providers=["codex"],
        )


@_requires_symlinks
def test_symlinked_skill_is_visible_but_blocked(import_homes, tmp_path) -> None:
    source, _ = import_homes
    outside = _write(tmp_path / "outside" / "secret.txt", "do not copy")
    skill = source / ".agents" / "skills" / "linked"
    skill.mkdir(parents=True)
    _write(skill / "SKILL.md", "---\nname: linked\n---")
    (skill / "secret.txt").symlink_to(outside)

    preview = service.preview_import(source_home=str(source), providers=["codex"])
    item = next(
        item
        for item in preview["items"]
        if item["kind"] == "global_skill" and item["source_system"] == "codex"
    )

    assert item["status"] == "skipped"
    public = next(
        entry
        for category in preview["categories"]
        for entry in category["items"]
        if entry["id"] == item["id"]
    )
    assert public["selectable"] is False


def test_codex_and_claude_project_registries_deduplicate(import_homes, monkeypatch) -> None:
    source, _ = import_homes
    project = source.parent / "existing project"
    project.mkdir()
    _write(
        source / ".codex" / "config.toml",
        f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n',
    )
    _write(source / ".claude.json", json.dumps({"projects": {str(project): {}}}))
    created = []

    def create_project(**kwargs):
        created.append(kwargs)
        return {"id": kwargs["project_id"]}

    monkeypatch.setattr(service.database, "create_project", create_project)
    preview = service.preview_import(source_home=str(source))
    projects = [item for item in preview["items"] if item["kind"] == "project_registration"]

    assert [item["status"] for item in projects] == ["ready", "duplicate"]
    result = service.apply_import(
        token=preview["token"],
        selected_item_ids=[projects[0]["id"]],
        source_home=str(source),
    )

    assert len(created) == 1
    assert created[0]["path"] == str(project.resolve())
    assert result["imported_projects"][0]["status"] == "imported"


def test_settings_category_describes_what_apply_copies(import_homes) -> None:
    """The dialog shows this description verbatim; hooks (and enabled plugins)
    in the category are copied on apply, so it must not claim inventory only."""
    source, destination = import_homes
    hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}
    _write(source / ".claude" / "settings.json", json.dumps({"hooks": hooks, "theme": "dark"}))

    preview = service.preview_import(source_home=str(source), providers=["claude"])
    category = next(c for c in preview["categories"] if c["id"] == "settings_plugins_inventory")
    assert "inventory only" not in category["description"].lower()
    assert "hooks" in category["description"].lower()
    assert "plugins" in category["description"].lower()
    hooks_item = next(entry for entry in category["items"] if entry["selectable"])

    result = service.apply_import(
        token=preview["token"],
        selected_item_ids=[hooks_item["id"]],
        source_home=str(source),
        providers=["claude"],
    )

    assert [entry["item_id"] for entry in result["imported"]] == [hooks_item["id"]]
    written = json.loads((destination / ".fluid" / "hooks.json").read_text(encoding="utf-8"))
    assert written.get("hooks", written) == hooks


@pytest.fixture
def docker_workspaces(tmp_path, monkeypatch):
    """Docker mode with a setup-chosen workspace that differs from start-up."""
    from app.core import config as config_module

    startup, chosen = tmp_path / "startup-workspace", tmp_path / "chosen-workspace"
    startup.mkdir()
    chosen.mkdir()
    monkeypatch.setattr(service, "is_local_mode", lambda: False)
    monkeypatch.setattr(service.settings, "workspace_dir", startup)
    monkeypatch.setitem(config_module._runtime_overrides, "workspace_dir", chosen)
    monkeypatch.setattr(service.database, "get_all_projects", list)
    monkeypatch.setattr(service.database, "get_project", lambda _project_id: None)
    return startup, chosen


def test_relative_project_row_resolves_against_effective_workspace(docker_workspaces) -> None:
    _, chosen = docker_workspaces
    assert service._project_path_for_row({"path": "proj"}) == (chosen / "proj").resolve()


def test_docker_project_inside_effective_workspace_is_ready(docker_workspaces) -> None:
    startup, chosen = docker_workspaces
    inside = chosen / "proj"
    inside.mkdir()
    assert service._project_status(inside, set(), set())[0] == "ready"
    elsewhere = startup / "proj"
    elsewhere.mkdir()
    assert service._project_status(elsewhere, set(), set())[0] == "diagnostic"


def test_docker_registration_stores_path_relative_to_effective_workspace(
    docker_workspaces, monkeypatch
) -> None:
    _, chosen = docker_workspaces
    project = chosen / "nested" / "proj"
    project.mkdir(parents=True)
    created = []
    monkeypatch.setattr(
        service.database,
        "create_project",
        lambda **kwargs: created.append(kwargs) or {"id": kwargs["project_id"]},
    )
    result = service._register_project(project)
    assert result.get("status") != "skipped"
    assert created and created[0]["path"] == "nested/proj"
