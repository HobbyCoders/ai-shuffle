"""#1610 — AI-authored plugins: save_plugin drafts, owner approval pinned to a hash.

HOME is redirected so the drafts land in tmp, never in a real ~/.fluid.
"""

import asyncio
import json
from pathlib import Path

import pytest

from fluid.context.plugin_authoring import (
    APPROVALS_FILE,
    PluginDraftError,
    authored_dir,
    is_approved,
    save_authored_plugin,
)
from fluid.context.plugin_formats import FORMAT_AGENT_PLUGINS, content_hash, read_plugin
from fluid.context.plugins import discover_plugins


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _spec(**overrides):
    spec = {
        "name": "release-notes",
        "version": "0.1.0",
        "description": "Drafts release notes",
        "skills": [{"name": "draft-notes", "description": "Writes notes from a diff", "body": "Read the diff."}],
        "commands": [{"name": "notes", "body": "Write release notes for $ARGUMENTS"}],
        "agents": [{"name": "editor", "description": "Edits prose", "body": "You edit prose."}],
        "mcp_servers": {"changelog": {"command": "python", "args": ["${PLUGIN_ROOT}/srv.py"],
                                       "env": {"TOKEN": "${CHANGELOG_TOKEN}"}}},
    }
    spec.update(overrides)
    return spec


def _enable(home: Path, plugin_id: str) -> None:
    path = home / ".fluid" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabledPlugins": {plugin_id: True}}), encoding="utf-8")


def _approve(home: Path, saved) -> None:
    path = home / ".fluid" / "plugins" / APPROVALS_FILE
    path.write_text(json.dumps({"approvals": {saved.id: {
        "version": saved.version, "hash": saved.content_hash,
    }}}), encoding="utf-8")


# ─── drafting ────────────────────────────────────────────────────────────────


def test_save_writes_an_agent_plugins_package_the_reader_loads(home):
    saved = save_authored_plugin(_spec())

    assert saved.id == "release-notes@authored"
    assert saved.path == authored_dir() / "release-notes" / "0.1.0"
    manifest = json.loads((saved.path / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == "./skills/" and manifest["mcpServers"] == "./mcp.json"
    assert manifest["extensions"]["ai.shuffle"]["commands"] == "./commands/"

    record = read_plugin(saved.path, name="release-notes", source="authored")
    assert record.format == FORMAT_AGENT_PLUGINS
    assert [s.name for s in record.skills] == ["release-notes:draft-notes"]
    assert record.skills[0].description == "Writes notes from a diff"
    assert [c.name for c in record.commands] == ["release-notes:notes"]
    assert [a.name for a in record.agents] == ["release-notes:editor"]
    assert set(record.mcp_servers) == {"changelog"}
    assert all(c.support == "loaded" for c in record.components)
    # A reviewer sees the server's command line, never its env.
    [server] = [c for c in record.components if c.kind == "mcp_server"]
    assert server.detail == "python ${PLUGIN_ROOT}/srv.py"
    assert "CHANGELOG_TOKEN" not in json.dumps([c.to_dict() for c in record.components])
    assert saved.content_hash == content_hash(saved.path)
    # Nothing staged is left behind.
    assert list((home / ".fluid" / "plugins" / "cache" / ".authored-staging").iterdir()) == []


def test_identical_resave_is_a_no_op_and_changed_content_needs_a_new_version(home):
    first = save_authored_plugin(_spec())
    again = save_authored_plugin(_spec())
    assert again.unchanged and again.content_hash == first.content_hash

    with pytest.raises(PluginDraftError, match="new version"):
        save_authored_plugin(_spec(description="Different"))
    bumped = save_authored_plugin(_spec(version="0.2.0", description="Different"))
    assert bumped.path.name == "0.2.0" and (first.path / "plugin.json").is_file()


@pytest.mark.parametrize("overrides, message", [
    ({"hooks": {"Stop": []}}, "hooks"),
    ({"mcp_servers": {"x": {"command": "x", "env": {"KEY": "sk-live-123"}}}}, "VAR"),
    ({"mcp_servers": {"x": {"url": "file:///etc/passwd"}}}, "https"),
    ({"mcp_servers": {"x": {"url": "https://a.test", "command": "x"}}}, "not both"),
    ({"name": "../escape"}, "lowercase"),
    ({"skills": [{"name": "Bad Name", "description": "d", "body": "b"}]}, "lowercase"),
    ({"version": "latest"}, "1.2.3"),
    ({"skills": [], "commands": [], "agents": [], "mcp_servers": {}}, "at least one"),
    ({"skills": [{"name": "a", "description": "d", "body": "x" * 60_000}]}, "longer"),
    ({"settings": {}}, "unsupported fields"),
])
def test_refused_drafts_write_nothing(home, overrides, message):
    with pytest.raises(PluginDraftError, match=message):
        save_authored_plugin(_spec(**overrides))
    assert not any(authored_dir().glob("*/*")) if authored_dir().exists() else True


# ─── approval gate ───────────────────────────────────────────────────────────


def test_enabled_but_unapproved_draft_does_not_load(home, tmp_path):
    save_authored_plugin(_spec())
    _enable(home, "release-notes@authored")
    assert discover_plugins(tmp_path) == []


@pytest.mark.parametrize("marketplace", ["Authored", "AUTHORED", "authored.", "authored "])
def test_another_spelling_of_the_authored_source_does_not_skip_review(home, tmp_path, marketplace):
    # Windows and macOS open cache/Authored as cache/authored (and Windows
    # drops trailing dots), so these keys must hit the same approval check.
    save_authored_plugin(_spec())
    _enable(home, f"release-notes@{marketplace}")
    assert discover_plugins(tmp_path) == []


def test_an_alias_folder_for_the_authored_source_does_not_skip_review(home, tmp_path):
    save_authored_plugin(_spec())
    cache = home / ".fluid" / "plugins" / "cache"
    try:
        (cache / "alias").symlink_to(cache / "authored", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    _enable(home, "release-notes@alias")
    assert discover_plugins(tmp_path) == []


def test_approved_content_loads_and_any_edit_stops_it(home, tmp_path):
    saved = save_authored_plugin(_spec())
    _enable(home, saved.id)
    _approve(home, saved)
    assert is_approved("release-notes", saved.path)
    [plugin] = discover_plugins(tmp_path)
    assert [s.name for s in plugin.skills] == ["release-notes:draft-notes"]
    assert plugin.mcp_servers["changelog"]["args"] == [f"{saved.path}/srv.py"]

    # An edit after approval changes the hash: the plugin stops loading.
    (saved.path / "skills" / "draft-notes" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert not is_approved("release-notes", saved.path)
    assert discover_plugins(tmp_path) == []


def test_a_new_version_waits_for_its_own_approval(home, tmp_path):
    saved = save_authored_plugin(_spec())
    _enable(home, saved.id)
    _approve(home, saved)
    save_authored_plugin(_spec(version="0.2.0", description="Newer"))
    # The newest version is the one that would load, and it is not approved.
    assert discover_plugins(tmp_path) == []


# ─── the code-mode tool ──────────────────────────────────────────────────────


def test_save_plugin_tool_is_registered_beside_save_skill_and_writes_a_draft(home, tmp_path):
    from fluid.codemode.session import (
        CodeModeSession,
        register_code_mode_tools,
        skill_store_tool_specs,
    )
    from fluid.codemode.skills import SkillStore
    from fluid.tools.registry import ToolRegistry

    specs = skill_store_tool_specs()
    assert specs["save_plugin"].risk == "write"
    assert specs["save_plugin"].source == specs["save_skill"].source

    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=SkillStore(tmp_path / "skills"))
    register_code_mode_tools(registry, session)
    handler = registry._entries["save_plugin"].handler
    outcome = asyncio.run(handler(_spec(), None))
    assert not outcome.is_error
    assert "stays off until the owner reviews" in outcome.text
    assert (authored_dir() / "release-notes" / "0.1.0" / "plugin.json").is_file()

    refused = asyncio.run(handler(_spec(hooks={"Stop": []}), None))
    assert refused.is_error and "hooks" in refused.text
