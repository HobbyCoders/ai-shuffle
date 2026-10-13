from pathlib import Path

from fluid.context.agents import discover_agents
from fluid.context.instructions import (
    build_instruction_stack,
    discover_commands,
    discover_instruction_files,
    discover_skills,
    nested_instructions,
)


def _home(monkeypatch, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def _project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _agent(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\ntools: [Read, Grep]\n---\nPrompt"


def test_global_and_project_instructions_are_fluid_native(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    _write(home / ".fluid" / "AGENTS.md", "fluid global")
    _write(home / ".claude" / "CLAUDE.md", "legacy Claude")
    _write(home / ".codex" / "AGENTS.md", "legacy Codex")
    project = _project(tmp_path / "project")
    _write(project / "AGENTS.md", "project rules")
    _write(project / "CLAUDE.md", "project Claude")

    stack = build_instruction_stack(project)
    assert stack.index("fluid global") < stack.index("project rules")
    assert "legacy Claude" not in stack
    assert "legacy Codex" not in stack
    assert "project Claude" not in stack


def test_open_agents_walk_override_and_nested_behavior(tmp_path) -> None:
    project = _project(tmp_path / "project")
    child = project / "pkg" / "nested"
    child.mkdir(parents=True)
    _write(project / "AGENTS.md", "replaced")
    _write(project / "AGENTS.override.md", "root override")
    _write(project / "AGENT.md", "singular")
    _write(project / ".agents" / "AGENTS.md", "open convention")
    _write(child / "AGENTS.md", "nearest")
    _write(child / "CLAUDE.md", "legacy nested")
    target = _write(child / "handler.py", "# code")

    paths = {Path(item.path) for item in discover_instruction_files(child, include_global=False)}
    assert project / "AGENTS.md" not in paths
    assert project / "AGENTS.override.md" in paths
    stack = build_instruction_stack(child, include_global=False)
    assert "replaced" not in stack
    assert stack.index("root override") < stack.index("nearest")
    assert [item.content for item in nested_instructions(target)] == ["nearest"]


def test_native_skill_and_command_roots_ignore_provider_directories(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "container home")
    _write(home / ".fluid" / "skills" / "global" / "SKILL.md", "---\nname: global\n---\nbody")
    _write(home / ".fluid" / "skills" / "helper.py", "def helper(): pass")
    _write(home / ".claude" / "skills" / "legacy" / "SKILL.md", "legacy")
    project = _project(tmp_path / "project")
    _write(project / ".agents" / "skills" / "local" / "SKILL.md", "---\nname: local\n---\nbody")
    _write(project / ".agents" / "commands" / "review.md", "review")
    _write(project / ".claude" / "commands" / "legacy.md", "legacy")

    skills = {skill.name for skill in discover_skills(project)}
    assert skills == {"global", "local"}
    assert [command.name for command in discover_commands(project, include_global=False)] == ["review"]


def test_native_agent_precedence_and_legacy_inertness(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "mac home")
    _write(home / ".fluid" / "agents" / "reviewer.md", _agent("reviewer", "global"))
    _write(home / ".fluid" / "agents" / "global-only.md", _agent("global-only", "home"))
    project = _project(tmp_path / "project")
    _write(project / ".agents" / "agents" / "reviewer.md", _agent("reviewer", "project"))
    child = project / "child"
    child.mkdir()
    _write(child / ".agents" / "agents" / "reviewer.md", _agent("reviewer", "nearest"))
    _write(project / ".claude" / "agents" / "legacy.md", _agent("legacy", "inert"))

    agents = {agent.name: agent for agent in discover_agents(child)}
    assert set(agents) == {"reviewer", "global-only"}
    assert agents["reviewer"].description == "nearest"
    assert agents["reviewer"].scope == "project"
    assert agents["reviewer"].tools == ("Read", "Grep")
