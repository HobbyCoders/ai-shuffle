from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core import settings_import_service as service


def _claude_home(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "macOS Home With Spaces"
    destination = tmp_path / "container home"
    claude = source / ".claude"
    (claude / "skills" / "review").mkdir(parents=True)
    (claude / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\nDo the review.\n"
    )
    (claude / "agents").mkdir()
    (claude / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\nReview.\n")
    (claude / "CLAUDE.md").write_text("Global instructions\n")
    (claude / "settings.json").write_text('{"secret": "must-not-leak"}')
    destination.mkdir()
    return source, destination


def _patch_runtime(monkeypatch, tmp_path: Path, *, local: bool = True):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    fake_settings = MagicMock()
    fake_settings.workspace_dir = workspace
    monkeypatch.setattr(service, "settings", fake_settings)
    monkeypatch.setattr(service, "is_local_mode", lambda: local)
    monkeypatch.setattr(service.database, "get_all_projects", list)
    monkeypatch.setattr(service.database, "get_project", lambda _project_id: None)
    return workspace


def test_preview_is_read_only_and_redacts_settings(monkeypatch, tmp_path):
    source, destination = _claude_home(tmp_path)
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))

    preview = service.preview_import(source_home=str(source))

    assert preview["source_untouched"] is True
    assert preview["overwrite_existing"] is False
    assert len(preview["token"]) == 64
    assert not (destination / ".fluid").exists()
    assert "must-not-leak" not in str(preview)
    kinds = {item["kind"] for item in preview["items"]}
    assert {"global_instruction", "global_skill", "global_agent", "settings"} <= kinds
    assert all(item["id"] for item in preview["items"])


def test_apply_copies_selected_items_without_overwrite(monkeypatch, tmp_path):
    source, destination = _claude_home(tmp_path)
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    existing = destination / ".fluid" / "agents" / "reviewer.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep me\n")

    preview = service.preview_import(source_home=str(source))
    selected = [item["id"] for item in preview["items"] if item["status"] == "ready"]
    result = service.apply_import(
        token=preview["token"], selected_item_ids=selected, source_home=str(source)
    )

    assert (destination / ".fluid" / "AGENTS.md").read_text() == "Global instructions\n"
    assert (destination / ".fluid" / "skills" / "review" / "SKILL.md").is_file()
    assert existing.read_text() == "keep me\n"
    assert (source / ".claude" / "CLAUDE.md").read_text() == "Global instructions\n"
    assert result["source_untouched"] is True


def test_apply_rejects_stale_preview(monkeypatch, tmp_path):
    source, destination = _claude_home(tmp_path)
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    preview = service.preview_import(source_home=str(source))
    (source / ".claude" / "CLAUDE.md").write_text("Changed after preview\n")

    with pytest.raises(service.StaleImportPlan):
        service.apply_import(
            token=preview["token"], selected_item_ids=[], source_home=str(source)
        )


def test_configured_project_is_registered_without_replacing_records(monkeypatch, tmp_path):
    source, destination = _claude_home(tmp_path)
    workspace = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    project = workspace / "Existing Project"
    project.mkdir()
    (source / ".claude.json").write_text(
        '{"projects": {"' + str(project).replace('\\', '\\\\') + '": {"allowedTools": []}}}'
    )
    created = []

    def create_project(**kwargs):
        created.append(kwargs)
        return {"id": kwargs["project_id"]}

    monkeypatch.setattr(service.database, "create_project", create_project)
    preview = service.preview_import(source_home=str(source))
    project_item = next(item for item in preview["items"] if item["kind"] == "project_registration")

    result = service.apply_import(
        token=preview["token"],
        selected_item_ids=[project_item["id"]],
        source_home=str(source),
    )

    assert len(created) == 1
    assert created[0]["path"] == str(project.resolve())
    assert result["imported_projects"][0]["status"] == "imported"


def test_docker_rejects_unconfigured_source_home(monkeypatch, tmp_path):
    source, destination = _claude_home(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, local=False)
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    monkeypatch.setenv("CLAUDE_IMPORT_SOURCE_HOME", str(tmp_path / "mounted legacy home"))
    (tmp_path / "mounted legacy home").mkdir()

    with pytest.raises(service.InvalidImportSource):
        service.preview_import(source_home=str(source))
