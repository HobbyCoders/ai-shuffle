"""Contract tests for the explicit generic external settings importer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid.context.settings_import import apply_settings_import, preview_settings_import


def posix(path: str | None) -> str:
    """Compare destinations with forward slashes so the asserts hold on Windows."""
    return (path or "").replace("\\", "/")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def ready(preview, kind: str):
    return next(item for item in preview.items if item.kind == kind and item.status == "ready")


def test_claude_only_preview_apply_and_source_unchanged(tmp_path: Path) -> None:
    source, destination = tmp_path / "mac home with spaces", tmp_path / "linux target"
    write(source / ".claude/CLAUDE.md", "Claude instructions\n")
    write(source / ".claude/skills/review/SKILL.md", "review\n")
    write(source / ".claude/agents/nested/helper.md", "agent\n")
    write(source / ".claude/settings.json", '{"permissions": "never execute"}')
    write(source / ".claude/plugins/example/plugin.json", '{"name": "example"}')

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert preview.sources == ("claude",)
    assert ready(preview, "global_instruction").destination == str(destination / ".fluid/AGENTS.md")
    assert ready(preview, "global_skill").is_directory
    assert any(item.status == "diagnostic" and item.kind == "settings" for item in preview.items)
    plugin_item = next(item for item in preview.items if item.kind == "plugins")
    assert plugin_item.status == "diagnostic"
    assert plugin_item.source_digest is None
    assert json.loads(json.dumps(preview.to_dict()))["source_home"] == str(source)

    result = apply_settings_import(preview)
    assert {item.status for item in result.items if item.status != "diagnostic"} == {"imported"}
    assert (destination / ".fluid/AGENTS.md").read_text() == "Claude instructions\n"
    assert (destination / ".fluid/skills/review/SKILL.md").read_text() == "review\n"
    assert (source / ".claude/CLAUDE.md").read_text() == "Claude instructions\n"
    assert list((destination / ".fluid/provenance").glob("*.json"))


def test_codex_only_includes_legacy_and_configured_agents(tmp_path: Path) -> None:
    source, destination = tmp_path / "container-home", tmp_path / "target"
    write(source / ".codex/AGENTS.md", "codex\n")
    write(source / ".agents/skills/native/SKILL.md", "native skill\n")
    write(source / ".codex/skills/legacy/SKILL.md", "legacy skill\n")
    write(source / ".codex/agents/main.toml", "name = 'main'\n")
    write(source / ".codex/agents/referenced.toml", "name = 'referenced'\n")
    write(source / ".codex/config.toml", '[agent]\nfile = "agents/referenced.toml"\n')

    preview = preview_settings_import(sources=("codex",), source_home=source, destination_home=destination)
    assert all(item.source_system == "codex" for item in preview.items)
    assert len([item for item in preview.items if item.kind == "global_agent" and item.status == "ready"]) == 2
    result = apply_settings_import(preview)
    assert (destination / ".fluid/AGENTS.md").read_text() == "codex\n"
    assert (destination / ".fluid/skills/native/SKILL.md").exists()
    assert (destination / ".fluid/skills/legacy/SKILL.md").exists()
    imported_agent = destination / ".fluid/agents/referenced.md"
    assert imported_agent.exists()
    assert "name: referenced" in imported_agent.read_text()
    assert all(item.status != "ready" for item in result.items)


def test_codex_global_override_replaces_base_instruction(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".codex/AGENTS.md", "base")
    override = write(source / ".codex/AGENTS.override.md", "override")

    preview = preview_settings_import(
        sources=("codex",), source_home=source, destination_home=destination
    )
    instruction = ready(preview, "global_instruction")

    assert instruction.source == str(override)
    apply_settings_import(preview)
    assert (destination / ".fluid/AGENTS.md").read_text() == "override"


def test_combined_exact_duplicate_collapses_and_different_collision_does_not_win(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude/skills/shared/SKILL.md", "same")
    write(source / ".agents/skills/shared/SKILL.md", "same")
    preview = preview_settings_import(sources=("claude", "codex"), source_home=source, destination_home=destination)
    skills = [item for item in preview.items if item.destination == str(destination / ".fluid/skills/shared")]
    assert {item.status for item in skills} == {"ready", "duplicate"}

    write(source / ".codex/AGENTS.md", "codex")
    write(source / ".claude/CLAUDE.md", "claude")
    collision = preview_settings_import(sources=("claude", "codex"), source_home=source, destination_home=destination)
    instructions = [item for item in collision.items if item.kind == "global_instruction"]
    assert instructions and all(item.status == "ready" for item in instructions)
    assert all("/.fluid/imports/" in posix(item.destination) for item in instructions)
    assert all(item.provenance["activation"] == "review_required" for item in instructions)


def test_instruction_duplicates_ignore_line_endings_and_trailing_whitespace(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude/CLAUDE.md", "same guidance  \r\n")
    write(source / ".codex/AGENTS.md", "same guidance\n")

    preview = preview_settings_import(
        sources=("claude", "codex"), source_home=source, destination_home=destination
    )
    instructions = [item for item in preview.items if item.kind == "global_instruction"]
    assert {item.status for item in instructions} == {"ready", "duplicate"}
    assert all("/imports/" not in (item.destination or "") for item in instructions)


def test_existing_native_wins_or_is_reported_as_exact_duplicate(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude/CLAUDE.md", "same")
    write(destination / ".fluid/AGENTS.md", "same")
    exact = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert ready_or(exact, "global_instruction").status == "duplicate"
    write(source / ".claude/CLAUDE.md", "different")
    conflict = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    sidecar = next(item for item in conflict.items if item.kind == "global_instruction")
    assert sidecar.status == "ready"
    assert "/.fluid/imports/claude/" in posix(sidecar.destination)
    apply_settings_import(conflict)
    assert (destination / ".fluid/AGENTS.md").read_text() == "same"
    assert Path(sidecar.destination).read_text() == "different"


def ready_or(preview, kind: str):
    return next(item for item in preview.items if item.kind == kind)


def test_symlink_and_source_change_after_preview_are_rejected(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    real = write(source / "real.md", "safe")
    link = source / ".claude/CLAUDE.md"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert next(item for item in preview.items if item.kind == "global_instruction").status == "skipped"

    link.unlink()
    write(link, "before")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    write(link, "after")
    result = apply_settings_import(preview)
    assert next(item for item in result.items if item.kind == "global_instruction").status == "skipped"
    assert not (destination / ".fluid/AGENTS.md").exists()


def test_apply_selected_ids_and_no_overwrite_race(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude/CLAUDE.md", "source")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = ready(preview, "global_instruction")
    write(destination / ".fluid/AGENTS.md", "native raced")
    result = apply_settings_import(preview, selected_ids=(item.id,))
    outcome = next(candidate for candidate in result.items if candidate.id == item.id)
    assert outcome.status == "conflict"
    assert (destination / ".fluid/AGENTS.md").read_text() == "native raced"


def test_mcp_redacts_secrets_and_reports_native_or_source_collisions(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude.json", json.dumps({"mcpServers": {
        "safe": {"command": "server", "args": ["--ok"], "env": {"TOKEN": "do-not-return"}, "api_key": "also-secret"}
    }}))
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = ready(preview, "global_mcp")
    assert "do-not-return" not in json.dumps(preview.to_dict())
    result = apply_settings_import(preview)
    payload = json.loads((destination / ".fluid/mcp.json").read_text())
    assert payload == {"mcpServers": {"safe": {"command": "server", "args": ["--ok"]}}}
    assert next(candidate for candidate in result.items if candidate.id == item.id).status == "imported"

    credential_source = tmp_path / "credential-source"
    write(credential_source / ".claude.json", json.dumps({"mcpServers": {
        "query-secret": {"url": "https://example.test/mcp?api_key=hidden"},
        "argument-secret": {"command": "server", "args": ["--token", "hidden"]},
    }}))
    credential_preview = preview_settings_import(
        sources=("claude",),
        source_home=credential_source,
        destination_home=tmp_path / "credential-target",
    )
    serialized = json.dumps(credential_preview.to_dict())
    assert "hidden" not in serialized
    assert not any(item.status == "ready" and item.kind == "global_mcp" for item in credential_preview.items)

    write(source / ".codex/config.toml", '[mcp_servers.safe]\ncommand = "other"\n')
    collision = preview_settings_import(sources=("claude", "codex"), source_home=source, destination_home=tmp_path / "fresh")
    assert next(item for item in collision.items if item.kind == "global_mcp").status == "conflict"


def test_project_inventory_and_native_codex_project_items(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project path"
    write(project / "CLAUDE.md", "project instructions")
    write(project / ".claude/skills/p/SKILL.md", "project skill")
    write(project / ".mcp.json", '{"mcpServers": {"p": {"command": "pserver"}}}')
    write(source / ".claude.json", json.dumps({"projects": {str(project): {"mcpServers": {"from-config": {"command": "c"}}}}}))
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert any(
        item.kind == "project_registration"
        and item.status == "ready"
        and item.provenance["project_path"] == str(project)
        for item in preview.items
    )
    assert any(item.destination == str(project / "AGENTS.md") and item.status == "ready" for item in preview.items)
    assert any(item.destination == str(project / ".agents/skills/p") for item in preview.items)
    assert any(item.destination == str(project / ".fluid/mcp.json") for item in preview.items)

    write(project / "AGENTS.override.md", "native")
    write(project / ".agents/skills/already/SKILL.md", "native skill")
    # A TOML literal (single-quoted) key: Windows paths hold backslashes that a
    # basic string would treat as escapes.
    write(source / ".codex/config.toml", f"[projects.'{project}']\ntrust_level = \"trusted\"\n")
    codex = preview_settings_import(sources=("codex",), source_home=source, destination_home=destination)
    assert any(item.kind == "project_native_instruction" and item.status == "duplicate" for item in codex.items)
    assert any(item.kind == "project_native_skill" and item.status == "duplicate" for item in codex.items)


def test_managed_container_claude_registry_symlink_is_allowed(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project"
    project.mkdir()
    persisted = write(
        source / ".claude" / ".claude.json",
        json.dumps({"projects": {str(project): {}}}),
    )
    link = source / ".claude.json"
    try:
        link.symlink_to(persisted)
    except OSError:
        pytest.skip("symlinks unavailable")

    preview = preview_settings_import(
        sources=("claude",), source_home=source, destination_home=destination
    )

    registration = next(item for item in preview.items if item.kind == "project_registration")
    assert registration.status == "ready"
    assert registration.provenance["project_path"] == str(project)
    assert not any(
        item.kind == "configuration" and "symlink" in item.message
        for item in preview.items
    )


def test_persisted_unraid_claude_registry_is_imported_without_root_link(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project"
    project.mkdir()
    persisted = write(
        source / ".claude" / ".claude.json",
        json.dumps({"projects": {str(project): {}}}),
    )

    preview = preview_settings_import(
        sources=("claude",), source_home=source, destination_home=destination
    )

    registration = next(item for item in preview.items if item.kind == "project_registration")
    assert registration.source_paths == (str(persisted),)
    assert registration.provenance["project_path"] == str(project)


def test_each_configured_project_has_a_distinct_item_id(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    projects = [tmp_path / "project one", tmp_path / "project two"]
    for project in projects:
        project.mkdir()
    write(
        source / ".claude.json",
        json.dumps({"projects": {str(project): {} for project in projects}}),
    )

    preview = preview_settings_import(
        sources=("claude",), source_home=source, destination_home=destination
    )
    registrations = [item for item in preview.items if item.kind == "project_registration"]

    assert len(registrations) == 2
    assert len({item.id for item in registrations}) == 2
    assert {item.provenance["project_path"] for item in registrations} == {
        str(project) for project in projects
    }
