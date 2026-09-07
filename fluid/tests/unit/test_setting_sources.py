"""setting_sources — scope control over what loads into context.

Covers the config knob (toml/env/overrides, validation), the loader's gating of
the mcp.json/hooks.json context layers, compiler-level gating of instructions/
skills/agents, and the ``.fluid`` project trees that mirror the global layout.

HOME (and USERPROFILE on Windows) is redirected to a tmp dir so the developer's
real ~/.fluid never leaks into an assertion, same convention as
test_settings_stack.py.
"""

import json
from pathlib import Path

import pytest

from fluid.config.loader import load_config
from fluid.config.model import HarnessConfig, _setting_sources_from
from fluid.context.agents import discover_agents
from fluid.context.compiler import PromptInputs, compile_prompt
from fluid.context.instructions import discover_commands, discover_skills

SKILL_MD = "---\nname: {name}\ndescription: d\n---\nBODY"
AGENT_MD = "---\nname: {name}\ndescription: d\n---\nPROMPT"


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _layout(tmp_path, monkeypatch):
    """A home with one global skill/agent and a project with one of each."""
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _write(home / ".fluid" / "AGENTS.md", "global instructions")
    _write(home / ".fluid" / "skills" / "gskill" / "SKILL.md", SKILL_MD.format(name="gskill"))
    _write(home / ".fluid" / "agents" / "gagent.md", AGENT_MD.format(name="gagent"))

    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    _write(proj / "AGENTS.md", "project instructions")
    _write(proj / ".fluid" / "skills" / "pskill" / "SKILL.md", SKILL_MD.format(name="pskill"))
    _write(proj / ".fluid" / "agents" / "pagent.md", AGENT_MD.format(name="pagent"))
    return proj


# ─── config parsing ──────────────────────────────────────────────────────────


def test_default_is_all_three_sources() -> None:
    assert HarnessConfig().setting_sources == ("user", "project", "local")
    assert _setting_sources_from(None) == ("user", "project", "local")


def test_accepts_list_and_comma_string_and_empty() -> None:
    assert _setting_sources_from(["user"]) == ("user",)
    assert _setting_sources_from("project, local") == ("project", "local")
    assert _setting_sources_from([]) == ()
    assert _setting_sources_from("") == ()


def test_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown setting_sources"):
        _setting_sources_from(["user", "workspace"])


def test_env_override_and_toml_layer(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    _write(proj / ".fluid" / "config.toml", 'setting_sources = ["user"]\n')
    config = load_config(cwd=proj, env={})
    assert config.setting_sources == ("user",)
    # Env outranks the project toml; an empty value means "none".
    config = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": "project"})
    assert config.setting_sources == ("project",)
    config = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": ""})
    assert config.setting_sources == ()
    assert "setting_sources" in HarnessConfig().to_dict()


# ─── loader gating of mcp.json / hooks.json layers ───────────────────────────


def test_loader_gates_mcp_layers_by_source(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    home = tmp_path / "home"
    _write(home / ".fluid" / "mcp.json", json.dumps({"mcpServers": {"userserver": {"command": "u"}}}))
    _write(proj / ".fluid" / "mcp.json", json.dumps({"mcpServers": {"projserver": {"command": "p"}}}))

    both = load_config(cwd=proj, env={})
    assert set(both.mcp) == {"userserver", "projserver"}
    user_only = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": "user"})
    assert set(user_only.mcp) == {"userserver"}
    project_only = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": "project"})
    assert set(project_only.mcp) == {"projserver"}
    none = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": ""})
    assert none.mcp == {}


def test_config_toml_layers_are_never_gated(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    home = tmp_path / "home"
    _write(home / ".fluid" / "config.toml", 'default_model = "from-user-toml"\n')
    config = load_config(cwd=proj, env={"FLUID_SETTING_SOURCES": ""})
    assert config.default_model == "from-user-toml"


# ─── discovery gating ────────────────────────────────────────────────────────


def test_fluid_project_trees_are_discovered(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    _write(proj / ".fluid" / "commands" / "ship.md", "ship it")
    assert {s.name for s in discover_skills(proj)} == {"gskill", "pskill"}
    assert {a.name for a in discover_agents(proj)} == {"gagent", "pagent"}
    assert {c.name for c in discover_commands(proj)} == {"ship"}


def test_project_fluid_agent_beats_dot_agents_sibling(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    _write(proj / ".agents" / "agents" / "dup.md", AGENT_MD.format(name="dup") + "\nfrom-agents")
    _write(proj / ".fluid" / "agents" / "dup.md", AGENT_MD.format(name="dup") + "\nfrom-fluid")
    dup = next(a for a in discover_agents(proj) if a.name == "dup")
    assert "from-fluid" in dup.body


def test_discovery_toggles_gate_scopes(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    assert {s.name for s in discover_skills(proj, include_global=False)} == {"pskill"}
    assert {s.name for s in discover_skills(proj, include_project=False)} == {"gskill"}
    assert {a.name for a in discover_agents(proj, include_global=False)} == {"pagent"}
    assert {a.name for a in discover_agents(proj, include_project=False)} == {"gagent"}


# ─── compiler gating ─────────────────────────────────────────────────────────


def _kinds_and_text(compiled):
    return (compiled.text or ""), {s.kind for s in compiled.sections}


def test_compiler_user_source_off_drops_global_context(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    compiled = compile_prompt(PromptInputs(
        cwd=proj, include_env=False,
        setting_sources=("project", "local"),
    ))
    text, _ = _kinds_and_text(compiled)
    assert "global instructions" not in text
    assert "project instructions" in text
    assert "gskill" not in text and "pskill" in text
    assert "gagent" not in text and "pagent" in text


def test_compiler_project_source_off_drops_project_context(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    compiled = compile_prompt(PromptInputs(
        cwd=proj, include_env=False,
        setting_sources=("user",),
    ))
    text, _ = _kinds_and_text(compiled)
    assert "global instructions" in text
    assert "project instructions" not in text
    assert "gskill" in text and "pskill" not in text
    assert "gagent" in text and "pagent" not in text


def test_compiler_no_sources_is_filesystem_isolated(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    compiled = compile_prompt(PromptInputs(
        base="BASE", cwd=proj, include_env=False,
        setting_sources=(),
    ))
    text, kinds = _kinds_and_text(compiled)
    assert text == "BASE"
    assert kinds == {"base"}


def test_compiler_default_none_loads_everything(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    compiled = compile_prompt(PromptInputs(
        cwd=proj, include_env=False,
    ))
    text, _ = _kinds_and_text(compiled)
    for expected in ("global instructions", "project instructions", "gskill", "pskill", "gagent", "pagent"):
        assert expected in text


def test_compiler_lists_enabled_plugin_skills_deferred(tmp_path, monkeypatch) -> None:
    proj = _layout(tmp_path, monkeypatch)
    home = tmp_path / "home"
    root = home / ".fluid" / "plugins" / "cache" / "mp" / "plug" / "1.0.0"
    _write(root / "plugin.json", '{"name": "plug"}')
    _write(root / "skills" / "extra" / "SKILL.md",
           "---\nname: extra\ndescription: plugin skill\n---\nPLUGIN BODY")
    _write(home / ".fluid" / "settings.json", json.dumps({"enabledPlugins": {"plug@mp": True}}))

    compiled = compile_prompt(PromptInputs(cwd=proj, include_env=False))
    text = compiled.text or ""
    assert "plug:extra: plugin skill" in text
    assert "PLUGIN BODY" not in text  # deferred: description only

    # Dropping the user source drops the plugin listing with it.
    gated = compile_prompt(PromptInputs(
        cwd=proj, include_env=False, setting_sources=("project",),
    ))
    assert "plug:extra" not in (gated.text or "")
