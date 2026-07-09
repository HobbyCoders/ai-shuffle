"""Tests for implicit plugins: exposing skills from a marketplace-less git repo.

When a user adds a git repo that has no `.claude-plugin/marketplace.json` but does
carry skills/commands/agents (e.g. remotion, whose skills live in `.agents/skills`
symlinked from `.claude/skills`), the whole repo is treated as a single implicit
plugin so it appears in the Available tab, installs, and its skills surface as
`/<repo>:<skill>` in chat.
"""

import os
from pathlib import Path

import pytest

from app.core.plugin_service import PluginService
from app.core.plugin_updates import check_for_updates


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """A PluginService whose CLAUDE dir is an isolated temp directory."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_claude_dir", lambda: claude_dir)
    return PluginService()


def _register_marketplace(service: PluginService, name: str) -> Path:
    """Create a fake cloned marketplace dir and register it. Returns the clone path."""
    clone = service.MARKETPLACES_DIR / name
    clone.mkdir(parents=True, exist_ok=True)
    data = service._read_json_file(service.MARKETPLACES_FILE, {})
    data[name] = {
        "source": {"source": "git", "url": f"https://github.com/test/{name}.git"},
        "installLocation": str(clone),
        "lastUpdated": "2026-01-01T00:00:00Z",
    }
    service._write_json_file(service.MARKETPLACES_FILE, data)
    return clone


def _write_skill(skills_dir: Path, name: str, description: str = "A test skill") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )
    return d


def test_available_synthesizes_implicit_plugin_from_dot_claude_skills(svc):
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")

    available = svc.get_available_plugins()
    entry = next((p for p in available if p.id == "remotion@remotion"), None)

    assert entry is not None
    assert entry.name == "remotion"
    assert entry.marketplace == "remotion"
    assert entry.marketplace_path == str(clone)
    assert entry.has_skills is True
    assert entry.has_commands is False
    assert entry.has_agents is False
    # Auto-executing components are never adopted from an implicit plugin.
    assert entry.has_hooks is False
    assert entry.has_mcp is False
    assert entry.installed is False


def test_available_detects_root_and_agents_skill_locations(svc):
    """skills/ at the repo root and under .agents/ are both recognized."""
    root_clone = _register_marketplace(svc, "rootrepo")
    _write_skill(root_clone / "skills", "one")

    agents_clone = _register_marketplace(svc, "agentsrepo")
    _write_skill(agents_clone / ".agents" / "skills", "two")

    available = {p.id: p for p in svc.get_available_plugins()}
    assert available["rootrepo@rootrepo"].has_skills is True
    assert available["agentsrepo@agentsrepo"].has_skills is True


def test_empty_repo_contributes_no_plugin(svc):
    """A repo with no skills/commands/agents (or an empty skills dir) yields nothing."""
    clone = _register_marketplace(svc, "emptyrepo")
    (clone / "README.md").write_text("# Empty\n", encoding="utf-8")
    # A skills dir that exists but holds no SKILL.md must not conjure a plugin.
    (clone / ".claude" / "skills").mkdir(parents=True)

    available = svc.get_available_plugins()
    assert all(p.id != "emptyrepo@emptyrepo" for p in available)


def test_install_materializes_symlinked_skills_as_real_files(svc):
    """remotion layout: real skill under .agents/skills symlinked from .claude/skills."""
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".agents" / "skills", "foo")
    claude_skills = clone / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    os.symlink("../../.agents/skills/foo", claude_skills / "foo")

    info = svc.install_plugin("remotion@remotion")

    assert info.id == "remotion@remotion"
    assert info.enabled is True
    assert info.has_skills is True

    # Install record marks it implicit and points at a real cache dir.
    installed = svc._read_json_file(svc.INSTALLED_FILE, {})
    record = installed["plugins"]["remotion@remotion"][0]
    assert record["implicit"] is True
    install_path = Path(record["installPath"])
    assert install_path.is_dir()

    # Symlinked skill is materialized as a real file, not a symlink.
    materialized = install_path / "skills" / "foo" / "SKILL.md"
    assert materialized.is_file()
    assert not materialized.is_symlink()
    assert not (install_path / "skills" / "foo").is_symlink()


def test_installed_implicit_plugin_drops_off_available(svc):
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")

    svc.install_plugin("remotion@remotion")
    entry = next(
        (p for p in svc.get_available_plugins() if p.id == "remotion@remotion"), None
    )
    assert entry is not None
    assert entry.installed is True
    assert entry.enabled is True


def test_get_plugin_details_lists_implicit_skills(svc):
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")
    _write_skill(clone / ".claude" / "skills", "bar")

    svc.install_plugin("remotion@remotion")
    details = svc.get_plugin_details("remotion@remotion")

    assert details is not None
    assert sorted(details.skills) == ["bar", "foo"]
    assert details.has_skills is True


def test_available_details_lists_skills_before_install(svc):
    """Pre-install details view finds skills under .claude/ too, not just root skills/."""
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")

    details = svc.get_available_plugin_details("remotion@remotion")
    assert details is not None
    assert details.skills == ["foo"]


def test_installed_implicit_plugin_skills_surface_in_discovery(svc, monkeypatch):
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")
    svc.install_plugin("remotion@remotion")

    monkeypatch.setattr(
        "app.core.plugin_service.get_plugin_service", lambda project_dir=None: svc
    )
    from app.core.slash_commands import discover_plugin_skills

    skills = discover_plugin_skills()
    assert any(s["name"] == "remotion:foo" for s in skills)


def test_check_for_updates_skips_implicit_plugin(svc):
    clone = _register_marketplace(svc, "remotion")
    _write_skill(clone / ".claude" / "skills", "foo")
    svc.install_plugin("remotion@remotion")

    updates = check_for_updates(svc)
    assert all(u.plugin_id != "remotion@remotion" for u in updates)


def test_install_ignores_dangling_symlink_skill(svc):
    clone = _register_marketplace(svc, "remotion")
    skills = clone / ".claude" / "skills"
    _write_skill(skills, "foo")
    os.symlink("does-not-exist", skills / "broken")  # dangling symlink

    info = svc.install_plugin("remotion@remotion")
    install_path = Path(info.install_path)

    assert (install_path / "skills" / "foo" / "SKILL.md").is_file()
    assert not (install_path / "skills" / "broken").exists()
