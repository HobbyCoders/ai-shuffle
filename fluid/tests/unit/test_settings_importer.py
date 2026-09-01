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

    codex_md = write(source / ".codex/AGENTS.md", "codex")
    claude_md = write(source / ".claude/CLAUDE.md", "claude")
    collision = preview_settings_import(sources=("claude", "codex"), source_home=source, destination_home=destination)
    instructions = [item for item in collision.items if item.kind == "global_instruction"]
    assert {item.status for item in instructions} == {"ready", "duplicate"}
    combined = ready(collision, "global_instruction")
    # No native file yet: one destination concatenates both, Claude first.
    assert combined.source_system == "combined"
    assert combined.source_paths == (str(claude_md), str(codex_md))
    assert combined.destination == str(destination / ".fluid/AGENTS.md")
    assert "concatenates" in combined.message
    result = apply_settings_import(collision)
    assert next(item for item in result.items if item.id == combined.id).status == "imported"
    text = (destination / ".fluid/AGENTS.md").read_text(encoding="utf-8")
    assert text == f"# From: {claude_md}\nclaude\n\n# From: {codex_md}\ncodex\n"


def test_existing_destination_apply_reports_skipped_with_the_sidecar(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude/CLAUDE.md", "incoming")
    write(destination / ".fluid/AGENTS.md", "native")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    result = apply_settings_import(preview)
    outcome = next(item for item in result.items if item.kind == "global_instruction")
    assert outcome.status == "skipped"
    assert "destination exists" in outcome.message and "review sidecar written" in outcome.message
    assert "/.fluid/imports/claude/" in posix(outcome.destination)
    assert Path(outcome.destination).read_text(encoding="utf-8") == "incoming"
    assert (destination / ".fluid/AGENTS.md").read_text(encoding="utf-8") == "native"


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
    assert "also-secret" not in json.dumps(preview.to_dict())
    assert "${SAFE_TOKEN}" in item.message and "api_key" in item.message
    result = apply_settings_import(preview)
    payload = json.loads((destination / ".fluid/mcp.json").read_text())
    assert payload == {"mcpServers": {"safe": {
        "command": "server", "args": ["--ok"], "env": {"TOKEN": "${SAFE_TOKEN}"},
    }}}
    assert next(candidate for candidate in result.items if candidate.id == item.id).status == "imported"

    credential_source = tmp_path / "credential-source"
    write(credential_source / ".claude.json", json.dumps({"mcpServers": {
        "query-secret": {"url": "https://example.test/mcp?api_key=hidden&v=2"},
        "argument-secret": {"command": "server", "args": ["--token", "hidden", "--key=hidden", "password=hidden"]},
        "userinfo-secret": {"url": "https://user:hidden@example.test/mcp"},
    }}))
    credential_preview = preview_settings_import(
        sources=("claude",),
        source_home=credential_source,
        destination_home=tmp_path / "credential-target",
    )
    serialized = json.dumps(credential_preview.to_dict())
    assert "hidden" not in serialized
    redacted = json.loads(ready(credential_preview, "global_mcp")._content)["mcpServers"]
    assert redacted["query-secret"]["url"] == "https://example.test/mcp?api_key=${QUERY_SECRET_API_KEY}&v=2"
    assert redacted["argument-secret"]["args"] == [
        "--token", "${ARGUMENT_SECRET_TOKEN}", "--key=${ARGUMENT_SECRET_KEY}", "password=${ARGUMENT_SECRET_PASSWORD}",
    ]
    assert "userinfo-secret" not in redacted
    assert any(
        item.status == "diagnostic" and "userinfo-secret" in item.message
        for item in credential_preview.items
    )

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
        and item.status == "diagnostic"  # informational: nothing to write
        and item.provenance["project_path"] == str(project)
        for item in preview.items
    )
    assert any(item.destination == str(project / "AGENTS.md") and item.status == "ready" for item in preview.items)
    assert any(item.destination == str(project / ".fluid/skills/p") for item in preview.items)
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
    assert registration.status == "diagnostic"
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


def test_enabled_claude_plugins_are_importable(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "target"
    root = source / ".claude/plugins/cache/mp/myplug/1.2.0"
    write(root / ".claude-plugin/plugin.json", '{"name": "myplug"}')
    write(root / "skills/hello/SKILL.md", "---\nname: hello\n---\nbody")
    write(source / ".claude/settings.json", json.dumps(
        {"enabledPlugins": {"myplug@mp": True, "ghost@mp": True, "off@mp": False}}
    ))

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    plugin = next(item for item in preview.items if item.kind == "plugin" and item.status == "ready")
    assert plugin.destination == str(destination / ".fluid/plugins/cache/mp/myplug/1.2.0")
    assert plugin.provenance["plugin_ref"] == "myplug@mp"
    enablement = next(item for item in preview.items if item.kind == "plugin_enablement")
    assert enablement.status == "ready"
    assert enablement.destination == str(destination / ".fluid/settings.json")
    # An enabled-but-uninstalled plugin is inventoried, never invented.
    assert any(item.status == "diagnostic" and "'ghost@mp'" in item.message for item in preview.items)

    result = apply_settings_import(preview)
    assert (destination / ".fluid/plugins/cache/mp/myplug/1.2.0/skills/hello/SKILL.md").exists()
    data = json.loads((destination / ".fluid/settings.json").read_text(encoding="utf-8"))
    assert data == {"enabledPlugins": {"myplug@mp": True}}
    assert all(item.status != "ready" for item in result.items)


def test_plugins_without_enablement_stay_inventory_only(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "target"
    write(source / ".claude/plugins/cache/mp/myplug/1.0.0/plugin.json", '{"name": "myplug"}')
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    plugin_items = [item for item in preview.items if item.kind in ("plugin", "plugins")]
    assert plugin_items and all(item.status == "diagnostic" for item in plugin_items)


# ─── instruction layering, hooks, commands, MCP fields, codex config ─────────


def _register_project(source: Path, project: Path) -> None:
    write(source / ".claude.json", json.dumps({"projects": {str(project): {}}}))


def test_claude_local_imports_as_a_layering_sibling(tmp_path: Path) -> None:
    from fluid.context.instructions import build_instruction_stack

    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    write(project / "CLAUDE.md", "shared rules")
    write(project / "CLAUDE.local.md", "personal rules")
    write(project / ".claude/CLAUDE.local.md", "dot personal rules")
    _register_project(source, project)

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    destinations = {posix(item.destination) for item in preview.items if item.kind == "project_instruction"}
    assert destinations == {
        posix(str(project / "AGENTS.md")),
        posix(str(project / "AGENTS.local.md")),
        posix(str(project / ".fluid/AGENTS.local.md")),
    }
    apply_settings_import(preview)
    stack = build_instruction_stack(project, include_global=False)
    assert stack.index("shared rules") < stack.index("personal rules") < stack.index("dot personal rules")


def test_hooks_blocks_are_copied_per_scope(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project"
    project.mkdir()
    user_hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}]}
    write(source / ".claude/settings.json", json.dumps({"hooks": user_hooks, "model": "opus"}))
    shared = {"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt.sh"}]}
    write(project / ".claude/settings.json", json.dumps({"hooks": {"PostToolUse": [shared]}}))
    write(project / ".claude/settings.local.json", json.dumps({"hooks": {
        "PostToolUse": [shared, {"hooks": [{"type": "command", "command": "local.sh"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "hello.sh"}]}],
    }}))
    _register_project(source, project)

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    user_item = ready(preview, "hooks")
    assert user_item.destination == str(destination / ".fluid/hooks.json")
    project_item = ready(preview, "project_hooks")
    assert project_item.destination == str(project / ".fluid/hooks.json")
    assert len(project_item.source_paths) == 2
    assert "hooks.local.json" in project_item.message

    apply_settings_import(preview)
    assert json.loads((destination / ".fluid/hooks.json").read_text()) == {"hooks": user_hooks}
    merged = json.loads((project / ".fluid/hooks.json").read_text())["hooks"]
    # The shared group appears once; every event is copied, the loader decides what runs.
    assert merged["PostToolUse"] == [shared, {"hooks": [{"type": "command", "command": "local.sh"}]}]
    assert merged["SessionStart"] == [{"hooks": [{"type": "command", "command": "hello.sh"}]}]


def test_existing_hooks_json_is_merged_per_event(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    kept = {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
    incoming = {"hooks": [{"type": "command", "command": "audit.sh"}]}
    write(source / ".claude/settings.json", json.dumps({"hooks": {
        "PreToolUse": [kept, incoming],
        "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
    }}))
    existing = destination / ".fluid/hooks.json"
    write(existing, json.dumps({"hooks": {"PreToolUse": [kept]}}))

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = ready(preview, "hooks")
    assert item.destination == str(existing)
    assert item.provenance["merge_into_existing"] is True
    assert "merged per event" in item.message
    result = apply_settings_import(preview)
    outcome = next(candidate for candidate in result.items if candidate.id == item.id)
    assert outcome.status == "imported" and "merged" in outcome.message
    merged = json.loads(existing.read_text())["hooks"]
    assert merged["PreToolUse"] == [kept, incoming]
    assert merged["Stop"] == [{"hooks": [{"type": "command", "command": "notify.sh"}]}]
    assert not list(existing.parent.glob("*.fluid-import"))

    # Nothing new: duplicate. A destination edited after the preview: conflict.
    again = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert next(candidate for candidate in again.items if candidate.kind == "hooks").status == "duplicate"
    write(source / ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}}))
    stale = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    existing.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    raced = apply_settings_import(stale)
    assert next(candidate for candidate in raced.items if candidate.kind == "hooks").status == "conflict"


def test_commands_are_copied_without_recursing(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "source", tmp_path / "destination", tmp_path / "project"
    project.mkdir()
    write(source / ".claude/commands/review.md", "review $ARGUMENTS")
    write(source / ".claude/commands/nested/deep.md", "not a command")
    write(project / ".claude/commands/ship.md", "ship it")
    _register_project(source, project)

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert [posix(item.destination) for item in preview.items if item.kind == "global_command"] == [
        posix(str(destination / ".fluid/commands/review.md")),
    ]
    assert ready(preview, "project_command").destination == str(project / ".fluid/commands/ship.md")
    apply_settings_import(preview)
    assert (destination / ".fluid/commands/review.md").read_text() == "review $ARGUMENTS"
    assert (project / ".fluid/commands/ship.md").read_text() == "ship it"

    # An existing command is never overwritten.
    write(destination / ".fluid/commands/review.md", "mine")
    second = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    assert next(item for item in second.items if item.kind == "global_command").status == "conflict"


def test_mcp_headers_env_cwd_type_and_flags_survive_with_placeholders(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    write(source / ".claude.json", json.dumps({"mcpServers": {
        "remote": {
            "type": "sse",
            "url": "https://example.test/sse",
            "headers": {"Authorization": "Bearer live-secret", "X-Trace": "on"},
            "disabled": True,
            "unknownField": 1,
        },
        "local": {
            "command": "srv",
            "cwd": "/srv/root",
            "env": {"API_TOKEN": "live-secret", "LOG_LEVEL": "debug"},
            "enabled": False,
        },
    }}))
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = ready(preview, "global_mcp")
    assert "live-secret" not in json.dumps(preview.to_dict())
    assert "${REMOTE_AUTHORIZATION}" in item.message and "${LOCAL_API_TOKEN}" in item.message
    assert "sse" in item.message and "unknownField" in item.message
    servers = json.loads(item._content)["mcpServers"]
    assert servers["remote"] == {
        "type": "sse",
        "url": "https://example.test/sse",
        "headers": {"Authorization": "${REMOTE_AUTHORIZATION}", "X-Trace": "on"},
        "disabled": True,
    }
    assert servers["local"] == {
        "command": "srv",
        "cwd": "/srv/root",
        "env": {"API_TOKEN": "${LOCAL_API_TOKEN}", "LOG_LEVEL": "debug"},
        "enabled": False,
    }


def test_codex_config_becomes_a_config_toml_fragment(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    config = write(source / ".codex/config.toml", 'model = "gpt-5-codex"\napproval_policy = "untrusted"\n')
    preview = preview_settings_import(sources=("codex",), source_home=source, destination_home=destination)
    fragment = ready(preview, "configuration_fragment")
    assert fragment.destination == str(destination / ".fluid/config.toml")
    assert "codex_auth.json" in fragment.message
    apply_settings_import(preview)
    text = (destination / ".fluid/config.toml").read_text(encoding="utf-8")
    assert text == (
        f"# From: {config}\n\n[providers.openai]\nmodel = \"gpt-5-codex\"\n\n"
        "[permission]\napproval_policy = \"always_ask\"\n"
    )

    # An existing config.toml sends the fragment to the imports tree instead.
    other = tmp_path / "other"
    write(other / ".fluid/config.toml", "default_provider = \"anthropic\"\n")
    write(source / ".codex/config.toml", 'approval_policy = "never"\n')
    second = preview_settings_import(sources=("codex",), source_home=source, destination_home=other)
    sidecar = ready(second, "configuration_fragment")
    assert posix(sidecar.destination) == posix(str(other / ".fluid/imports/codex/config.toml"))
    assert "merge by hand" in sidecar.message
    apply_settings_import(second)
    assert 'approval_policy = "never_ask"' in Path(sidecar.destination).read_text(encoding="utf-8")
    assert (other / ".fluid/config.toml").read_text(encoding="utf-8") == "default_provider = \"anthropic\"\n"

    # Nothing translatable: no fragment item.
    write(source / ".codex/config.toml", 'sandbox_mode = "workspace-write"\n')
    none = preview_settings_import(sources=("codex",), source_home=source, destination_home=tmp_path / "empty")
    assert not any(item.kind == "configuration_fragment" for item in none.items)
