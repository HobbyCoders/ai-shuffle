"""CE6/CE7 — agent definitions: discovery, precedence, prompt index, seams.

HOME (and USERPROFILE, which is what ``Path.expanduser`` reads on Windows) is
redirected to a temporary directory so the developer's real Fluid or legacy
provider files never leak into an assertion.
"""

import json
import logging
from pathlib import Path

import pytest

from fluid.context.agents import AgentDefinition, discover_agents, split_tools
from fluid.context.instructions import parse_frontmatter

REVIEWER_MD = """---
name: reviewer
description: Reviews code for correctness
model: claude-opus-5
tools: [Read, Grep, Bash]
permissionMode: acceptEdits
maxTurns: 12
color: blue
---
You are a meticulous code reviewer. Read the diff before judging it.
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _agent_md(name: str, description: str, body: str = "BODY", extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


@pytest.fixture()
def project(tmp_path, monkeypatch) -> Path:
    """A git-bounded project dir with an empty HOME beside it."""
    _set_home(monkeypatch, tmp_path / "home")
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


def _home(project: Path) -> Path:
    return project.parent / "home"


def _by_name(agents) -> dict[str, AgentDefinition]:
    return {a.name: a for a in agents}


# ─── frontmatter list support ────────────────────────────────────────────────


def test_frontmatter_parses_inline_arrays() -> None:
    fields, body = parse_frontmatter(
        '---\nname: x\ntools: [Read, "Grep", Bash]\nempty: []\n---\nBODY'
    )
    assert fields["tools"] == ["Read", "Grep", "Bash"]
    assert fields["empty"] == []
    assert fields["name"] == "x"  # scalars unchanged
    assert body == "BODY"


def test_frontmatter_scalars_are_untouched() -> None:
    fields, _ = parse_frontmatter('---\nname: "quoted"\ndesc: a, b, c\n---\n')
    assert fields == {"name": "quoted", "desc": "a, b, c"}


def test_tools_comma_split_from_either_shape() -> None:
    assert split_tools(["Read", "Grep"]) == ("Read", "Grep")
    assert split_tools("Read, Grep, Bash") == ("Read", "Grep", "Bash")
    assert split_tools("Read") == ("Read",)
    assert split_tools("") == ()
    assert split_tools(None) == ()


# ─── discovery ───────────────────────────────────────────────────────────────


def test_project_agent_is_fully_loaded(project: Path) -> None:
    path = _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)

    agent = _by_name(discover_agents(project))["reviewer"]
    assert agent.description == "Reviews code for correctness"
    assert agent.scope == "project"
    assert agent.path == str(path)
    assert agent.model == "claude-opus-5"
    assert agent.tools == ("Read", "Grep", "Bash")
    # The body IS the agent's system prompt, loaded eagerly (CE6).
    assert "meticulous code reviewer" in agent.body
    # Unconsumed frontmatter survives for a host dispatcher that understands it.
    assert agent.raw["permissionMode"] == "acceptEdits"
    assert agent.raw["maxTurns"] == "12"
    assert agent.raw["color"] == "blue"


def test_tools_bare_string_also_splits(project: Path) -> None:
    _write(
        project / ".agents" / "agents" / "t.md",
        _agent_md("t", "d", extra="tools: Read, Grep, Bash\n"),
    )
    assert _by_name(discover_agents(project))["t"].tools == ("Read", "Grep", "Bash")


def test_no_tools_means_inherit_everything(project: Path) -> None:
    _write(project / ".agents" / "agents" / "t.md", _agent_md("t", "d"))
    assert _by_name(discover_agents(project))["t"].tools == ()


def test_name_falls_back_to_the_file_stem(project: Path) -> None:
    _write(project / ".agents" / "agents" / "nested" / "helper.md", "no frontmatter here")
    agent = _by_name(discover_agents(project))["helper"]
    assert agent.body == "no frontmatter here"
    assert agent.description == ""


def test_nested_project_dirs_nearest_to_cwd_wins(project: Path) -> None:
    inner = project / "pkg" / "sub"
    inner.mkdir(parents=True)
    _write(project / ".agents" / "agents" / "dup.md", _agent_md("dup", "outer"))
    _write(inner / ".agents" / "agents" / "dup.md", _agent_md("dup", "inner"))

    agents = _by_name(discover_agents(inner))
    assert agents["dup"].description == "inner"
    assert agents["dup"].path.startswith(str(inner))


def test_global_agent_discovered_and_loses_to_project(project: Path) -> None:
    home = _home(project)
    _write(home / ".fluid" / "agents" / "dup.md", _agent_md("dup", "from home"))
    _write(home / ".fluid" / "agents" / "only-global.md", _agent_md("solo", "home only"))
    _write(project / ".agents" / "agents" / "dup.md", _agent_md("dup", "from project"))

    agents = _by_name(discover_agents(project))
    assert agents["dup"].description == "from project" and agents["dup"].scope == "project"
    assert agents["solo"].scope == "global"

    # include_global=False drops the home scope entirely.
    assert "solo" not in _by_name(discover_agents(project, include_global=False))


def test_results_are_sorted_by_name(project: Path) -> None:
    for name in ("zulu", "alpha", "mike"):
        _write(project / ".agents" / "agents" / f"{name}.md", _agent_md(name, name))
    assert [a.name for a in discover_agents(project)] == ["alpha", "mike", "zulu"]


def test_unreadable_file_is_skipped_with_a_warning(project: Path, caplog) -> None:
    good = project / ".agents" / "agents" / "good.md"
    _write(good, _agent_md("good", "fine"))
    # A directory named like an agent file: rglob finds it, reading it fails.
    (project / ".agents" / "agents" / "broken.md").mkdir()

    with caplog.at_level(logging.WARNING):
        agents = _by_name(discover_agents(project))
    assert "good" in agents and "broken" not in agents
    assert any("unreadable agent definition" in r.getMessage() for r in caplog.records)


def test_no_native_agent_setup_yields_nothing(project: Path) -> None:
    assert discover_agents(project) == []


# ─── plugin agents ───────────────────────────────────────────────────────────


def _install_plugin(home: Path, *, agent_name: str = "reviewer") -> Path:
    root = home / ".claude" / "plugins" / "cache" / "testmp" / "myplug" / "1.0.0"
    _write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "myplug"}))
    _write(
        root / "agents" / "reviewer.md",
        _agent_md(agent_name, "packaged reviewer", body="PLUGIN AGENT PROMPT"),
    )
    _write(
        home / ".claude" / "settings.json",
        json.dumps({"enabledPlugins": {"myplug@testmp": True}}),
    )
    return root


def test_plugin_agents_are_inert_even_with_legacy_flag(project: Path) -> None:
    _install_plugin(_home(project))
    _write(project / ".agents" / "agents" / "reviewer.md", _agent_md("reviewer", "the project's"))

    agents = _by_name(discover_agents(project, include_plugins=True))
    assert set(agents) == {"reviewer"}
    assert agents["reviewer"].description == "the project's"


def test_include_plugins_false_drops_plugin_agents(project: Path) -> None:
    _install_plugin(_home(project))
    assert discover_agents(project, include_plugins=False) == []


# The compiled agents index (position, sorting, toggles) lives in
# test_compiler.py, next to the skills-index tests it mirrors.


# ─── engine seams (registry + usage) ─────────────────────────────────────────


def _loop(cwd, *, agents=None, ctx_extra=None, native_agents=True):
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover - never streamed
            if False:
                yield None

    ctx = ToolCtx(cwd=str(cwd))
    if ctx_extra:
        ctx.extra.update(ctx_extra)
    return NativeLoop(
        provider=_Provider(),
        store=MemoryStore(),
        model="m",
        ctx=ctx,
        agents=agents,
        native_agents=native_agents,
    )


def test_native_loop_publishes_the_agent_registry(project: Path) -> None:
    _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)
    loop = _loop(project)

    registry = loop.ctx.extra["agent_registry"]
    assert set(registry) == {"reviewer"}
    assert registry["reviewer"].body.startswith("You are a meticulous")
    assert [d.name for d in loop.native_agents] == ["reviewer"]


def test_host_declared_agent_wins_a_name_collision(project: Path, caplog) -> None:
    _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)
    host_entry = object()

    with caplog.at_level(logging.WARNING):
        loop = _loop(project, ctx_extra={"agent_registry": {"reviewer": host_entry}})

    assert loop.ctx.extra["agent_registry"]["reviewer"] is host_entry
    assert any("already declared by the host" in r.getMessage() for r in caplog.records)


def test_toggle_off_discovers_nothing(project: Path) -> None:
    _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)
    loop = _loop(project, native_agents=False)
    assert loop.native_agents == []
    assert "agent_registry" not in loop.ctx.extra
    assert loop.agents == []


async def test_discovered_agents_are_not_priced_as_custom_agents(project: Path) -> None:
    """The body is the worker's prompt; only the index line enters this
    session's prompt, and the compiler prices that as System prompt."""
    _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)
    loop = _loop(project)
    assert [d.name for d in loop.native_agents] == ["reviewer"]
    assert loop.agents == []

    usage = await loop.get_context_usage()
    assert usage["agents"] == []
    assert not [c for c in usage["categories"] if c["name"] == "Custom agents"]


async def test_host_declared_agents_are_not_replaced(project: Path) -> None:
    """A host that priced its own catalog keeps it — no double counting."""
    _write(project / ".agents" / "agents" / "reviewer.md", REVIEWER_MD)
    loop = _loop(project, agents=[{"agentType": "app-agent", "text": "host text"}])

    usage = await loop.get_context_usage()
    assert [a["agentType"] for a in usage["agents"]] == ["app-agent"]
    # The registry seam is still populated — only the pricing row is the host's.
    assert set(loop.ctx.extra["agent_registry"]) == {"reviewer"}
