"""Per-checkout skill discovery works without private maintainer files.

The public tree intentionally ships no .fluid/skills. Exercise the same
checkout and nested-worktree layouts with disposable prose skills instead.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid.context.instructions import discover_skills, parse_frontmatter

SKILL_TEXT = """---
name: example-skill
description: A disposable project skill for discovery and loading tests.
---
# Example skill

Use this synthetic body to test project skill loading.
"""


def _write_skill(checkout, text=SKILL_TEXT):
    skill = checkout / ".fluid" / "skills" / "example-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(text, encoding="utf-8")
    return skill


@pytest.fixture(autouse=True)
def _isolated_discovery_tree(tmp_path, monkeypatch):
    # Discovery stops at the closest .git marker, not at Path.home(). Windows
    # temp paths can be below a real home containing .fluid/.agents skills.
    (tmp_path / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def _skills(cwd):
    return discover_skills(
        cwd, include_global=False, include_plugins=False, include_builtin=False,
    )


def test_checkout_boundary_excludes_ancestor_home_skills(tmp_path, monkeypatch):
    from fluid.context import instructions

    home = tmp_path / "home"
    _write_skill(home, SKILL_TEXT.replace("example-skill", "ancestor-skill"))
    checkout = home / "AppData" / "Local" / "Temp" / "checkout"
    (checkout / ".git").mkdir(parents=True)
    read_skills = instructions._skills_in

    def only_checkout_roots(root, scope):
        # Fail before any ancestor skill is read, not just after filtering it.
        assert root.is_relative_to(checkout)
        return read_skills(root, scope)

    monkeypatch.setattr(instructions, "_skills_in", only_checkout_roots)
    assert _skills(checkout) == []


def test_checkout_without_local_skills_is_supported(tmp_path):
    assert _skills(tmp_path) == []


def test_project_skill_frontmatter_is_discovered_without_loading_body(tmp_path):
    path = _write_skill(tmp_path)
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    found, = _skills(tmp_path)
    assert found.name == fields["name"] == "example-skill"
    assert found.description == fields["description"]
    assert found.scope == "project" and not found.body_loaded
    assert Path(found.path) == path
    assert "synthetic body" in body


@pytest.mark.parametrize("nested", [".fluid/worktrees/feat-x", ".claude/worktrees/feat-y"])
def test_skills_are_discovered_from_checkout_and_nested_worktree(tmp_path, nested):
    checkout = tmp_path / "checkout"
    path = _write_skill(checkout)
    worktree = checkout / nested
    worktree.mkdir(parents=True)
    for cwd in (checkout, worktree):
        found, = _skills(cwd)
        assert found.name == "example-skill"
        assert Path(found.path) == path


def test_nested_worktree_skill_takes_precedence(tmp_path):
    _write_skill(tmp_path)
    worktree = tmp_path / ".fluid" / "worktrees" / "feat-x"
    local = _write_skill(worktree, SKILL_TEXT.replace("A disposable project skill", "A worktree-local skill"))
    found, = _skills(worktree)
    assert Path(found.path) == local
    assert found.description.startswith("A worktree-local skill")


@pytest.mark.asyncio
async def test_project_skill_tool_loads_body_with_scope_gates(tmp_path, monkeypatch):
    from fluid.tools.builtins import skill
    from fluid.tools.registry import ToolCtx

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    checkout = tmp_path / "checkout"
    path = _write_skill(checkout)
    outcome = await skill.handler(
        {"name": "example-skill"},
        ToolCtx(cwd=str(checkout), extra={"setting_sources": ["project"]}),
    )
    assert not outcome.is_error
    assert "synthetic body" in outcome.text
    usage = json.loads((home / ".fluid/skills/.usage.json").read_text())
    assert usage[str(path)]["loads"] == 1
    blocked = await skill.handler(
        {"name": "example-skill"},
        ToolCtx(cwd=str(checkout), extra={"setting_sources": []}),
    )
    assert blocked.is_error and "disabled by setting_sources" in blocked.text
