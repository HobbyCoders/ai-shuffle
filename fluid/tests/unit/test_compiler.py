"""Prompt-compiler unit tests (omni.context.compiler / environment)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omni.context.compiler import PromptInputs, compile_system_prompt, skills_index
from omni.context.environment import environment_block, git_context


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


def test_skills_index_progressive_disclosure(git_repo: Path):
    skill = git_repo / ".claude" / "skills" / "demo" / "SKILL.md"
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
