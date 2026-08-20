"""Prompt-compiler unit tests (fluid.context.compiler / environment)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fluid.context.compiler import (
    PromptInputs,
    agents_index,
    compile_prompt,
    compile_system_prompt,
    skills_index,
)
from fluid.context.environment import environment_block, git_context


@pytest.fixture()
def plain_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    return tmp_path


# ─── environment ─────────────────────────────────────────────────────────────


def test_environment_block_non_repo(plain_dir: Path):
    block = environment_block(plain_dir)
    assert block.startswith("# Environment")
    assert f"Working directory: {plain_dir.resolve()}" in block
    assert "Today's date:" in block
    assert "Git repository: no" in block


def test_environment_block_git_repo(git_repo: Path):
    block = environment_block(git_repo)
    assert "Git repository: yes" in block
    assert "Current branch:" in block
    assert "first" in block  # recent commit subject
    assert "Working tree: clean" in block


def test_git_context_dirty(git_repo: Path):
    (git_repo / "b.txt").write_text("dirty")
    ctx = git_context(str(git_repo))
    assert ctx is not None and "1 changed path(s)" in ctx


def test_git_context_none_outside_repo(plain_dir: Path):
    assert git_context(str(plain_dir)) is None


# ─── compiler ────────────────────────────────────────────────────────────────


def test_base_first_env_after(git_repo: Path):
    out = compile_system_prompt(
        PromptInputs(base="I am the agent.", cwd=git_repo)
    )
    assert out is not None
    assert out.startswith("I am the agent.")
    assert out.index("I am the agent.") < out.index("# Environment")


def test_empty_base_still_compiles_env(plain_dir: Path):
    out = compile_system_prompt(PromptInputs(base=None, cwd=plain_dir))
    assert out is not None and "# Environment" in out


def test_extra_blocks_append_last(plain_dir: Path):
    out = compile_system_prompt(
        PromptInputs(
            base="BASE",
            extra_blocks=["SECURITY RULES", "", "MEMORY"],
            cwd=plain_dir,
        )
    )
    assert out is not None
    assert out.index("# Environment") < out.index("SECURITY RULES") < out.index("MEMORY")


def test_all_disabled_and_empty_returns_none(plain_dir: Path):
    out = compile_system_prompt(
        PromptInputs(
            base="  ",
            cwd=plain_dir,
            include_env=False,
            include_global_instructions=False,
            include_project_instructions=False,
            include_skills=False,
        )
    )
    assert out is None


def test_project_instruction_files_injected(git_repo: Path):
    (git_repo / "AGENTS.md").write_text("Always be terse.")
    out = compile_system_prompt(
        PromptInputs(base="B", cwd=git_repo, include_global_instructions=False)
    )
    assert out is not None
    assert "Always be terse." in out
    assert f"# From: {git_repo / 'AGENTS.md'}" in out


def _skill(path: Path, name: str, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nBODY", encoding="utf-8")


def test_skills_index_is_sorted_by_name(git_repo: Path, tmp_path: Path, monkeypatch):
    """CE7: the index feeds the cached prefix, so its order must not depend on
    the filesystem walk (or on which plugins happen to be installed)."""
    # Plugin discovery reads ~/.claude regardless of include_global, so point
    # HOME at an empty dir (USERPROFILE is what expanduser reads on Windows).
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty-home"))
    for name in ("zulu", "alpha", "mike"):
        _skill(git_repo / ".agents" / "skills" / name / "SKILL.md", name, f"the {name} skill")
    idx = skills_index(git_repo, include_global=False)
    assert idx is not None
    listed = [line[2:].split(":")[0] for line in idx.splitlines() if line.startswith("- ")]
    assert listed == ["alpha", "mike", "zulu"]


def test_plugin_skills_are_inert_in_the_index(git_repo: Path, tmp_path: Path, monkeypatch):
    """Legacy plugin content is not a native discovery source."""
    import json

    home = tmp_path / "home"
    plugin = home / ".claude" / "plugins" / "cache" / "mp" / "demoplug" / "1.0.0"
    _skill(plugin / "skills" / "packaged" / "SKILL.md", "packaged", "from a plugin")
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demoplug"}), encoding="utf-8"
    )
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"demoplug@mp": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _skill(git_repo / ".agents" / "skills" / "local" / "SKILL.md", "local", "the project's own")

    idx = skills_index(git_repo, include_global=False, include_plugins=True)
    assert idx is not None
    assert "demoplug" not in idx
    assert "- local: the project's own" in idx


def _agent(path: Path, name: str, description: str, body: str = "AGENT PROMPT") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8"
    )


def _empty_home(tmp_path: Path, monkeypatch) -> Path:
    """Point HOME (USERPROFILE on Windows) at a fresh dir so the developer's own
    ~/.claude never reaches an assertion."""
    home = tmp_path / "empty-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def test_agents_index_is_sorted_and_body_free(git_repo: Path, tmp_path: Path, monkeypatch):
    """CE6/CE7: name+description only, sorted — the bodies are the workers'
    system prompts, not the caller's business."""
    _empty_home(tmp_path, monkeypatch)
    for name in ("zulu", "alpha", "mike"):
        _agent(git_repo / ".agents" / "agents" / f"{name}.md", name, f"the {name} agent")

    idx = agents_index(git_repo)
    assert idx is not None
    listed = [line[2:].split(":")[0] for line in idx.splitlines() if line.startswith("- ")]
    assert listed == ["alpha", "mike", "zulu"]
    assert "Task tool" in idx  # the header explains what the list is for
    assert "AGENT PROMPT" not in idx


def test_agents_index_none_without_definitions(git_repo: Path, tmp_path: Path, monkeypatch):
    _empty_home(tmp_path, monkeypatch)
    assert agents_index(git_repo) is None


def test_agents_section_sits_after_skills_before_environment(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    _empty_home(tmp_path, monkeypatch)
    _skill(git_repo / ".agents" / "skills" / "deploy" / "SKILL.md", "deploy", "Ship it")
    _agent(git_repo / ".agents" / "agents" / "reviewer.md", "reviewer", "Reviews code")

    compiled = compile_prompt(PromptInputs(base="B", cwd=git_repo))
    kinds = [s.kind for s in compiled.sections]
    assert kinds.index("skills") < kinds.index("agents") < kinds.index("environment")
    assert "- reviewer: Reviews code" in (compiled.text or "")


def test_include_agents_false_omits_the_index(git_repo: Path, tmp_path: Path, monkeypatch):
    _empty_home(tmp_path, monkeypatch)
    _agent(git_repo / ".agents" / "agents" / "reviewer.md", "reviewer", "Reviews code")

    compiled = compile_prompt(PromptInputs(base="B", cwd=git_repo, include_agents=False))
    assert compiled.of_kind("agents") == ()
    assert "reviewer" not in (compiled.text or "")


def test_legacy_plugin_flag_cannot_enable_provider_discovery(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import json

    home = _empty_home(tmp_path, monkeypatch)
    plugin = home / ".claude" / "plugins" / "cache" / "mp" / "demoplug" / "1.0.0"
    _skill(plugin / "skills" / "packaged" / "SKILL.md", "packaged", "from a plugin")
    _agent(plugin / "agents" / "packaged-agent.md", "packaged-agent", "from a plugin")
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demoplug"}), encoding="utf-8"
    )
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"demoplug@mp": True}}), encoding="utf-8"
    )
    _skill(git_repo / ".agents" / "skills" / "local" / "SKILL.md", "local", "the project's own")

    native = compile_system_prompt(PromptInputs(base="B", cwd=git_repo)) or ""
    assert "demoplug" not in native
    assert "- local: the project's own" in native

    explicit_compat = compile_system_prompt(
        PromptInputs(base="B", cwd=git_repo, include_plugins=True)
    ) or ""
    assert "demoplug" not in explicit_compat
    assert "- local: the project's own" in explicit_compat


def test_skills_index_progressive_disclosure(git_repo: Path):
    skill = git_repo / ".agents" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: does demo things\n---\nBODY NEVER LOADED")
    idx = skills_index(git_repo, include_global=False)
    assert idx is not None
    assert "demo: does demo things" in idx
    assert "BODY NEVER LOADED" not in idx
    out = compile_system_prompt(
        PromptInputs(base=None, cwd=git_repo, include_global_instructions=False)
    )
    assert out is not None and "demo: does demo things" in out
