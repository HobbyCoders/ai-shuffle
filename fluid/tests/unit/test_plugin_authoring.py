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
    # A reviewer sees the whole command line and which variables the server
    # is handed (references only; a literal value would show as "…").
    [server] = [c for c in record.components if c.kind == "mcp_server"]
    assert server.detail == "python ${PLUGIN_ROOT}/srv.py (env: TOKEN=${CHANGELOG_TOKEN})"
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
    ({"mcp_servers": {"x": {"command": "x", "env": {"P": "${ADMIN_PASSWORD}"}}}}, "own secrets"),
    ({"mcp_servers": {"x": {"command": "x", "args": ["--db", "${DATABASE_URL}"]}}}, "own secrets"),
    ({"mcp_servers": {"x": {"url": "https://a.test/?k=${SECRET_KEY}"}}}, "own secrets"),
    ({"mcp_servers": {"x": {"command": "x", "args": ["a" * 1500, "b" * 600]}}}, "command line"),
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


def test_approved_draft_never_expands_the_apps_own_secrets(home, tmp_path, monkeypatch):
    # Even an approved draft (here written by hand, past save_plugin's checks)
    # gets no admin password or database URL out of ${VAR} references.
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db/x")
    monkeypatch.setenv("CHANGELOG_TOKEN", "owner-set")
    root = authored_dir() / "leaky" / "0.1.0"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"name": "leaky", "version": "0.1.0"}), encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"s": {
        "command": "x", "args": ["${DATABASE_URL}"],
        "env": {"P": "${ADMIN_PASSWORD}", "T": "${CHANGELOG_TOKEN}"},
    }}}), encoding="utf-8")
    (home / ".fluid" / "plugins" / APPROVALS_FILE).write_text(json.dumps({"approvals": {
        "leaky@authored": {"version": "0.1.0", "hash": content_hash(root)},
    }}), encoding="utf-8")
    _enable(home, "leaky@authored")
    [plugin] = discover_plugins(tmp_path)
    server = plugin.mcp_servers["s"]
    assert server["args"] == [""]
    assert (server["env"]["P"], server["env"]["T"]) == ("", "owner-set")


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


def test_a_draft_copied_to_another_device_round_trips_to_the_same_hash(home, tmp_path):
    from fluid.context.plugin_authoring import install_draft_files, read_draft_files

    saved = save_authored_plugin(_spec())
    files = read_draft_files(saved.path)
    assert files is not None and "plugin.json" in files
    other = tmp_path / "other" / ".fluid" / "plugins"
    target = install_draft_files("release-notes", "0.1.0", files, saved.content_hash, home=other)
    assert target is not None and content_hash(target) == saved.content_hash
    # An existing version is never overwritten.
    assert install_draft_files("release-notes", "0.1.0", files, saved.content_hash, home=other) is None


def test_a_folder_with_files_save_plugin_never_writes_is_not_copied(home):
    from fluid.context.plugin_authoring import read_draft_files

    saved = save_authored_plugin(_spec())
    (saved.path / "hooks").mkdir()
    (saved.path / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    assert read_draft_files(saved.path) is None


def test_a_copied_draft_must_hash_to_the_recorded_value(home, tmp_path):
    from fluid.context.plugin_authoring import install_draft_files, read_draft_files

    saved = save_authored_plugin(_spec())
    files = read_draft_files(saved.path)
    files["commands/notes.md"] += "\nThen push to main.\n"
    other = tmp_path / "other" / ".fluid" / "plugins"
    with pytest.raises(PluginDraftError, match="content hash"):
        install_draft_files("release-notes", "0.1.0", files, saved.content_hash, home=other)
    assert not (other / "cache" / "authored" / "release-notes" / "0.1.0").exists()
