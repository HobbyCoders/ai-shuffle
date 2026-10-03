"""The plugin folder validator (fluid.context.plugin_check)."""

import json
from pathlib import Path

import pytest

from fluid.context.plugin_authoring import save_authored_plugin
from fluid.context.plugin_check import check_plugin_folder, main
from fluid.context.plugin_formats import read_plugin


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _valid(root: Path) -> Path:
    _write(root / "plugin.json", json.dumps({
        "name": "cf-dns",
        "version": "0.1.0",
        "description": "Read DNS records",
        "skills": "./skills/",
        "mcpServers": "./mcp.json",
        "extensions": {"ai.shuffle": {
            "commands": "./commands/",
            "agents": "./agents/",
            "services": [{"name": "cloudflare", "provider": "cloudflare", "access": "read",
                          "purpose": "Read DNS records"}],
        }},
    }))
    _write(root / "skills" / "dns" / "SKILL.md",
           "---\nname: dns\ndescription: List DNS records\n---\n1. Call ChatResources.\n")
    _write(root / "commands" / "zones.md", "---\ndescription: list zones\n---\nList zones $ARGUMENTS\n")
    _write(root / "agents" / "auditor.md",
           "---\nname: auditor\ndescription: audits records\ntools: Read, Grep\n---\nAudit.\n")
    _write(root / "mcp.json", json.dumps({"mcpServers": {"dns-tools": {
        "command": "npx", "args": ["-y", "dns-tools"], "env": {"ZONE": "${ZONE}"}}}}))
    _write(root / "README.md", "# cf-dns\n")
    return root


def test_a_valid_folder_passes_and_installs_as_checked(tmp_path) -> None:
    result = check_plugin_folder(_valid(tmp_path / "cf-dns"))
    assert result.ok, result.render()
    assert result.render().startswith("OK: cf-dns 0.1.0 is a valid plugin")
    assert any("README.md" in w for w in result.warnings)

    saved = save_authored_plugin(result.spec, home=tmp_path / "plugins")
    record = read_plugin(saved.path, name="cf-dns", source="authored")
    assert {(c.kind, c.name) for c in record.components} >= {
        ("skill", "dns"), ("command", "zones"), ("agent", "auditor"), ("mcp_server", "dns-tools"),
    }
    assert "tools: Read, Grep" in (saved.path / "agents" / "auditor.md").read_text(encoding="utf-8")
    manifest = json.loads((saved.path / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["extensions"]["ai.shuffle"]["services"][0]["name"] == "cloudflare"


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda r: _write(r / "agents" / "auditor.md",
                      "---\nname: auditor\ndescription: a\nmodel: opus\n---\nx\n"),
     "frontmatter field 'model' is not allowed; remove it; the model is chosen per chat"),
    (lambda r: _write(r / "agents" / "auditor.md",
                      "---\nname: auditor\ndescription: a\ntools: Read, MultiEdit\n---\nx\n"),
     "'MultiEdit' is not a tool here; use Edit"),
    (lambda r: _write(r / "hooks" / "hooks.json", "{}"), "cannot carry hooks"),
    (lambda r: _write(r / "scripts" / "run.sh", "echo hi"), "scripts/run.sh: not part of the plugin format"),
    (lambda r: _write(r / ".claude-plugin" / "plugin.json", "{}"), "another tool's manifest folder"),
    (lambda r: _write(r / "skills" / "dns" / "SKILL.md",
                      "---\nname: other\ndescription: d\n---\nbody\n"),
     "frontmatter 'name' must be 'dns'"),
    (lambda r: _write(r / "skills" / "dns" / "SKILL.md",
                      "---\nname: dns\ndescription: d\n---\nuse sk-abcdefghijklmnopqrstuvwxyz123456\n"),
     "looks like it contains an API key"),
    (lambda r: _write(r / "mcp.json", json.dumps({"mcpServers": {"dns-tools": {
        "command": "npx", "env": {"ZONE": "literal-value"}}}})),
     "must be a ${VAR} reference"),
], ids=["model-pin", "foreign-tool", "hooks", "script", "foreign-manifest", "skill-name",
        "secret", "literal-env"])
def test_problems_are_reported_in_plain_words(tmp_path, mutate, expected) -> None:
    root = _valid(tmp_path / "cf-dns")
    mutate(root)
    result = check_plugin_folder(root)
    assert not result.ok
    assert result.spec is None
    assert expected in result.render(), result.render()


def test_service_entries_cannot_carry_a_credential(tmp_path) -> None:
    root = _valid(tmp_path / "cf-dns")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    manifest["extensions"]["ai.shuffle"]["services"][0]["token"] = "x"
    _write(root / "plugin.json", json.dumps(manifest))
    result = check_plugin_folder(root)
    assert not result.ok
    assert "remove ['token']" in result.render()


def test_every_problem_is_listed_not_just_the_first(tmp_path) -> None:
    root = tmp_path / "bad"
    _write(root / "plugin.json", json.dumps({"name": "Bad Name", "version": "1", "hooks": {}}))
    _write(root / "notes.txt", "x")
    result = check_plugin_folder(root)
    text = result.render()
    for part in ("'name' must be lowercase", "'version' must look like 1.2.3",
                 "'description' is required", "field 'hooks'", "notes.txt"):
        assert part in text, part


def test_missing_folder_and_command_line(tmp_path, capsys) -> None:
    assert not check_plugin_folder(tmp_path / "nope").ok
    assert main([str(_valid(tmp_path / "cf-dns"))]) == 0
    assert "OK: cf-dns" in capsys.readouterr().out
    assert main([str(tmp_path / "nope")]) == 1
