"""plugin_hooks: agent frontmatter, file-based agents and hook discovery shapes."""

import json
from types import SimpleNamespace

from app.core import plugin_hooks as ph


def _plugin(path, pid="demo@market", name="Demo"):
    return SimpleNamespace(id=pid, name=name, install_path=str(path) if path else None)


# ---------------------------------------------------------------------------
# Agent frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_full():
    content = "---\nDescription: Reviews code\nmodel: opus\ntools: Read, Grep, ,Bash\nnoise line\n---\n\nBody text\n"
    assert ph._parse_agent_frontmatter(content) == {
        "description": "Reviews code",
        "model": "opus",
        "tools": ["Read", "Grep", "Bash"],
        "prompt": "Body text",
    }


def test_frontmatter_absent_or_unterminated_keeps_whole_prompt():
    assert ph._parse_agent_frontmatter("just a prompt")["prompt"] == "just a prompt"
    unterminated = "---\ndescription: x\nno close"
    parsed = ph._parse_agent_frontmatter(unterminated)
    assert parsed["prompt"] == unterminated and parsed["description"] == ""


def test_frontmatter_crlf_delimiters():
    parsed = ph._parse_agent_frontmatter("---\r\nmodel: haiku\r\n---\r\nHi")
    assert parsed["model"] == "haiku" and parsed["prompt"] == "Hi"


# ---------------------------------------------------------------------------
# File-based agents
# ---------------------------------------------------------------------------


def test_file_based_agents_skip_disabled_and_bad_files(tmp_path):
    good = tmp_path / "good"
    (good / "agents" / "nested").mkdir(parents=True)
    (good / "agents" / "reviewer.md").write_text("---\ndescription: R\n---\nReview.", encoding="utf-8")
    (good / "agents" / "nested" / "deep.md").write_text("Deep prompt", encoding="utf-8")
    # A non-UTF-8 file must not take the other agents down with it.
    (good / "agents" / "latin1.md").write_bytes(b"caf\xe9 \xff\xfe")
    no_agents = tmp_path / "empty"
    no_agents.mkdir()
    disabled = tmp_path / "off"
    (disabled / "agents").mkdir(parents=True)
    (disabled / "agents" / "x.md").write_text("x")

    svc = SimpleNamespace(
        get_installed_plugins=lambda: [
            _plugin(good, "good@m"), _plugin(no_agents, "empty@m"), _plugin(disabled, "off@m"),
        ],
        get_enabled_plugins=lambda: {"good@m": True, "empty@m": True, "off@m": False},
    )
    agents = {a["id"]: a for a in ph.get_file_based_agents(svc)}
    assert set(agents) == {"good@m:reviewer", "good@m:deep"}
    assert agents["good@m:reviewer"]["description"] == "R"
    assert agents["good@m:reviewer"]["source_plugin"] == "good@m"
    assert agents["good@m:deep"]["prompt"] == "Deep prompt"


# ---------------------------------------------------------------------------
# Hook discovery
# ---------------------------------------------------------------------------


def _hooks(tmp_path, **files):
    root = tmp_path / "plug"
    for rel, body in files.items():
        p = root / rel.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    root.mkdir(exist_ok=True)
    return ph.get_hooks_from_plugins([_plugin(root)])


def test_missing_install_path_is_skipped(tmp_path):
    assert ph.get_hooks_from_plugins([_plugin(None), _plugin(tmp_path / "gone")]) == []


def test_markdown_hook_files(tmp_path):
    rows = _hooks(
        tmp_path,
        **{
            "hooks__guard.md": "---\nevent: \"PreToolUse\"\ntoolMatcher: 'Bash|Edit'\ndescription: Guards\n---\nPrompt",
            "hooks__alt.md": "---\nevent: Stop\ntool_matcher: X\n---\n",
            "hooks__noevent.md": "---\ndescription: no event\n---\n",
            "hooks__plain.md": "no frontmatter",
            "hooks__open.md": "---event: x",
        },
    )
    by_event = {r["event"]: r for r in rows}
    assert set(by_event) == {"PreToolUse", "Stop"}
    assert by_event["PreToolUse"]["tool_matcher"] == "Bash|Edit"
    assert by_event["PreToolUse"]["description"] == "Guards"
    assert by_event["Stop"]["tool_matcher"] == "X"
    assert by_event["Stop"]["plugin_id"] == "demo@market"


def test_hooks_json_shapes(tmp_path):
    # List of names + objects (and junk that is ignored).
    rows = _hooks(tmp_path, **{"hooks__hooks.json": [
        "PreToolUse",
        {"event": "PostToolUse", "type": "command", "description": "d", "toolMatcher": "Bash"},
        {},
        7,
    ]})
    assert [(r["event"], r["hook_type"]) for r in rows] == [
        ("PreToolUse", "prompt"), ("PostToolUse", "command"), ("Unknown", "prompt"),
    ]
    assert rows[1]["tool_matcher"] == "Bash"


def test_hooks_json_claude_code_object_format(tmp_path):
    rows = _hooks(tmp_path, **{"hooks__hooks.json": {
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}], "Stop": []},
    }})
    assert sorted(r["event"] for r in rows) == ["PreToolUse", "Stop"]


def test_hooks_json_event_map_and_malformed(tmp_path):
    rows = _hooks(tmp_path, **{"hooks__hooks.json": {"SessionStart": "start.py"}})
    assert [r["event"] for r in rows] == ["SessionStart"]
    assert _hooks(tmp_path / "b", **{"hooks__hooks.json": "{broken"}) == []
    assert _hooks(tmp_path / "c", **{"hooks__hooks.json": {"hooks": None}}) == []
    assert _hooks(tmp_path / "d", **{"hooks__hooks.json": 42}) == []


def test_plugin_json_hooks_list(tmp_path):
    rows = _hooks(tmp_path, **{"plugin.json": {"hooks": [
        {"event": "Notification", "toolMatcher": "*"}, "Stop",
    ]}})
    assert [(r["event"], r["tool_matcher"]) for r in rows] == [("Notification", "*"), ("Stop", None)]


def test_plugin_json_hooks_object_format_is_not_dropped(tmp_path):
    """The Claude Code plugin.json shape maps events to matcher lists."""
    rows = _hooks(tmp_path, **{".claude-plugin__plugin.json": {
        "name": "demo", "hooks": {"PreToolUse": [{"matcher": "Write"}], "UserPromptSubmit": []},
    }})
    assert sorted(r["event"] for r in rows) == ["PreToolUse", "UserPromptSubmit"]


def test_plugin_json_hooks_path_string_and_bad_json(tmp_path):
    # A string is a path to a hooks file, not a list of one-letter events.
    assert _hooks(tmp_path, **{"plugin.json": {"hooks": "./hooks/hooks.json"}}) == []
    assert _hooks(tmp_path / "b", **{"plugin.json": "[not json"}) == []
    assert _hooks(tmp_path / "c", **{"plugin.json": ["a", "list"]}) == []
    assert _hooks(tmp_path / "d", **{"plugin.json": {"name": "no hooks"}}) == []


def test_claude_manifest_wins_like_the_runtime_loader(tmp_path):
    # Listed hooks come from the manifest fluid's shared reader picks (the
    # one the runtime loads): .claude-plugin/plugin.json over a root plugin.json.
    rows = _hooks(tmp_path, **{
        "plugin.json": {"hooks": ["RootEvent"]},
        ".claude-plugin__plugin.json": {"hooks": ["NestedEvent"]},
    })
    assert [r["event"] for r in rows] == ["NestedEvent"]


def test_agent_plugins_legacy_manifest_hooks_are_listed(tmp_path):
    rows = _hooks(tmp_path, **{".codex-plugin__plugin.json": {"hooks": {"Stop": []}}})
    assert [r["event"] for r in rows] == ["Stop"]


def test_get_all_plugin_hooks_uses_the_service(tmp_path):
    root = tmp_path / "p"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps(["Stop"]))
    svc = SimpleNamespace(get_installed_plugins=lambda: [_plugin(root)])
    assert [r["event"] for r in ph.get_all_plugin_hooks(lambda: svc)] == ["Stop"]
