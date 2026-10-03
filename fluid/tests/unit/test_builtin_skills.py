"""Built-in skills shipped in ``fluid/builtin_skills`` and the build-plugin skill."""

import re
from pathlib import Path

import pytest

import fluid
from fluid.context import instructions
from fluid.context.plugin_authoring import save_authored_plugin
from fluid.context.plugin_check import check_plugin_folder
from fluid.tools.builtins import skill as skill_tool
from fluid.tools.registry import ToolCtx

PACKAGE_SKILLS = Path(fluid.__file__).resolve().parent / "builtin_skills"
BUILD_PLUGIN = PACKAGE_SKILLS / "build-plugin" / "SKILL.md"


@pytest.fixture
def builtins_on(monkeypatch):
    """conftest hides the built-in skills from every other test."""
    monkeypatch.setattr(instructions, "builtin_skills_dir", lambda: PACKAGE_SKILLS)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _by_name(proj: Path, **kw) -> dict:
    return {s.name: s for s in instructions.discover_skills(proj, **kw)}


def test_default_builtin_dir_is_inside_the_package() -> None:
    # The real function, not conftest's stand-in.
    assert Path(instructions.__file__).resolve().parent.parent / "builtin_skills" == PACKAGE_SKILLS
    assert BUILD_PLUGIN.is_file()


def test_build_plugin_is_discovered_as_a_builtin(builtins_on, project) -> None:
    skill = _by_name(project)["build-plugin"]
    assert skill.scope == "builtin"
    assert Path(skill.path) == BUILD_PLUGIN
    assert "plugin" in skill.description.lower()
    assert "build-plugin" not in _by_name(project, include_builtin=False)


@pytest.mark.parametrize("where", ["global", "project"])
def test_a_skill_of_the_same_name_replaces_the_builtin(builtins_on, project, where) -> None:
    base = Path.home() / ".fluid" / "skills" if where == "global" else project / ".fluid" / "skills"
    _write(base / "build-plugin" / "SKILL.md", "---\nname: build-plugin\ndescription: mine\n---\nmine\n")
    skill = _by_name(project)["build-plugin"]
    assert (skill.scope, skill.description) == (where, "mine")


async def test_the_skill_tool_loads_the_builtin_body(builtins_on, project) -> None:
    outcome = await skill_tool.handler({"name": "build-plugin"}, ToolCtx(cwd=str(project)))
    assert not outcome.is_error, outcome.text
    assert "# Build a plugin" in outcome.text
    assert "python -m fluid.context.plugin_check" in outcome.text


def _example_files() -> dict[str, str]:
    """The files of the skill's worked example, keyed by path in the plugin."""
    text = BUILD_PLUGIN.read_text(encoding="utf-8")
    example = text.split("\n## Example\n", 1)[1]
    found = re.findall(r"\*\*zone-report/(\S+)\*\*\n\n```\w*\n(.*?)\n```", example, re.DOTALL)
    return {path: body + "\n" for path, body in found}


def test_the_example_in_the_skill_passes_the_validator(tmp_path) -> None:
    """The skill cannot drift from the format the validator enforces."""
    files = _example_files()
    assert set(files) == {
        "plugin.json",
        "skills/zone-report/SKILL.md",
        "commands/dns.md",
        "agents/record-auditor.md",
    }
    root = tmp_path / "zone-report"
    for rel, body in files.items():
        _write(root / rel, body)
    result = check_plugin_folder(root)
    assert result.ok and result.spec is not None, result.render()
    assert result.spec["services"][0]["name"] == "cloudflare"
    assert set(result.components) >= {
        "skill zone-report", "command dns", "agent record-auditor",
    }, result.components

    # The step after the check: save_plugin takes the same content.
    saved = save_authored_plugin(result.spec, home=tmp_path / "plugins")
    assert saved.id == "zone-report@authored" and saved.version == "0.1.0"
