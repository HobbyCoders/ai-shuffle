"""Config-driven shell hooks: TOML/hooks.json parsing, matchers, and real
subprocess hooks (deny, rewrite, timeout, payload shape) through the engine."""

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluid.cli.repl import render_hook_event
from fluid.config.loader import (
    _load_hooks_json,
    build_engine,
    deep_merge,
    load_config,
)
from fluid.config.model import HarnessConfig, HookConfig
from fluid.engine.native import NativeLoop
from fluid.events import HookEvent, ToolResult, UserTurn
from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, TURN_END, HookBus, HookDecision
from fluid.hooks.command import CommandHook, matcher_matches, register_config_hooks
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry

# ─── config parsing ──────────────────────────────────────────────────────────


def test_deep_merge_appends_hooks_but_replaces_other_lists() -> None:
    base = {"hooks": [{"event": "TurnEnd", "command": "a"}], "tools": {"disallowed": ["x"]}}
    overlay = {"hooks": [{"event": "TurnEnd", "command": "b"}], "tools": {"disallowed": ["y"]}}
    merged = deep_merge(base, overlay)
    assert [h["command"] for h in merged["hooks"]] == ["a", "b"]
    assert merged["tools"]["disallowed"] == ["y"]
    assert len(base["hooks"]) == 1  # base untouched


def test_hooks_merge_user_then_project(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".fluid").mkdir(parents=True)
    (home / ".fluid" / "config.toml").write_text(
        '[[hooks]]\nevent = "PreToolUse"\nmatcher = "Bash"\ncommand = "user-hook"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    project = tmp_path / "proj"
    (project / ".fluid").mkdir(parents=True)
    (project / ".fluid" / "config.toml").write_text(
        '[[hooks]]\nevent = "TurnEnd"\ncommand = "project-hook"\ntimeout = 5\n',
        encoding="utf-8",
    )
    config = load_config(cwd=project, include_system=False, env={})
    assert [h.command for h in config.hooks] == ["user-hook", "project-hook"]
    assert config.hooks[0].matcher == "Bash"
    assert config.hooks[0].timeout == 60.0
    assert config.hooks[1].matcher is None
    assert config.hooks[1].timeout == 5.0
    assert config.to_dict()["hooks"][1]["command"] == "project-hook"


def test_hooks_json_import_shape(tmp_path, caplog) -> None:
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash|Edit", "hooks": [
                    {"type": "command", "command": "guard.sh", "timeout": 10},
                    {"type": "prompt", "prompt": "ignored"},
                ]},
                {"hooks": [{"command": "audit.sh"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
            "UserPromptSubmit": [{"hooks": [{"command": "prompt.sh"}]}],
            "SessionStart": [{"hooks": [{"command": "start.sh"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "notice.sh"}]}],
            "PermissionRequest": [{"hooks": [{"command": "skip.sh"}]}],
        }
    }), encoding="utf-8")
    with caplog.at_level("WARNING", logger="fluid.config.loader"):
        overlay = _load_hooks_json(path)
    assert overlay["hooks"] == [
        {"event": "PreToolUse", "command": "guard.sh", "matcher": "Bash|Edit", "timeout": 10},
        {"event": "PreToolUse", "command": "audit.sh"},
        {"event": "TurnEnd", "command": "notify.sh"},
        {"event": "TurnStart", "command": "prompt.sh"},
        {"event": "SessionStart", "command": "start.sh"},
        {"event": "Notification", "command": "notice.sh"},
    ]
    # Every dropped entry is named in the log rather than vanishing.
    dropped = [r.getMessage() for r in caplog.records]
    assert any("'PermissionRequest'" in m and "no such event" in m for m in dropped)
    assert any("type 'prompt'" in m for m in dropped)
    # The file is read at both scopes, ahead of the TOML in the same scope.
    (tmp_path / ".fluid").mkdir()
    (tmp_path / ".fluid" / "hooks.json").write_text(path.read_text(encoding="utf-8"))
    (tmp_path / ".fluid" / "config.toml").write_text(
        '[[hooks]]\nevent = "TurnStart"\ncommand = "toml-hook"\n', encoding="utf-8"
    )
    config = load_config(cwd=tmp_path, include_system=False, include_user=False, env={})
    assert [h.command for h in config.hooks] == [
        "guard.sh", "audit.sh", "notify.sh", "prompt.sh", "start.sh", "notice.sh", "toml-hook"
    ]


def test_hooks_json_malformed_is_ignored(tmp_path, caplog) -> None:
    path = tmp_path / "hooks.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fluid.config.loader"):
        assert _load_hooks_json(path) == {}
    assert any("hooks file ignored" in r.getMessage() for r in caplog.records)


def test_hook_config_validation() -> None:
    with pytest.raises(ValueError, match="unknown hook event"):
        HookConfig.from_dict({"event": "PermissionRequest", "command": "x"})
    # Claude Code's names are accepted in config.toml too, stored as fluid's.
    assert HookConfig.from_dict({"event": "UserPromptSubmit", "command": "x"}).event == "TurnStart"
    assert HookConfig.from_dict({"event": "Stop", "command": "x"}).event == "TurnEnd"
    assert HookConfig.from_dict({"event": "PreCompact", "command": "x"}).event == "PreCompact"
    with pytest.raises(ValueError, match="non-empty command"):
        HookConfig.from_dict({"event": "TurnEnd", "command": "  "})
    with pytest.raises(ValueError, match="timeout"):
        HookConfig.from_dict({"event": "TurnEnd", "command": "x", "timeout": 0})
    hook = HookConfig.from_dict({"event": "PostToolUse", "command": "fmt", "matcher": ""})
    assert hook.matcher is None


# ─── matcher semantics ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("matcher", "name", "expected"),
    [
        (None, "Bash", True),
        ("", "Bash", True),
        ("*", "Bash", True),
        ("Bash", "Bash", True),
        ("Bash", "BashX", False),
        ("mcp__*", "mcp__wx__get_weather", True),
        ("mcp__*", "Bash", False),
        ("Edit|Write", "Write", True),
        ("Edit|Write", "Read", False),
        ("^mcp__", "mcp__x", True),
        ("(Bash|Monitor)", "Monitor", True),
        ("Bash", None, False),
        ("(", "Bash", False),  # invalid regex never matches
        ("Notebook.*", "NotebookEdit", True),
        ("Bas.", "Bash", True),
        ("Bash", "BashOutput", False),
        ("mcp__chrome__.*", "mcp__chrome__click", True),
        ("mcp__chrome__.*", "mcp__wx__get_weather", False),
    ],
)
def test_matcher_matches(matcher, name, expected) -> None:
    assert matcher_matches(matcher, name) is expected


def test_interpret_exit_codes() -> None:
    hook = CommandHook(event=PRE_TOOL_USE, command="x")
    deny = hook.interpret(PRE_TOOL_USE, 2, "", "bad idea\n")
    assert deny.action == "deny" and deny.note == "bad idea"
    assert hook.interpret(PRE_TOOL_USE, 2, "", "").note == "blocked by hook"
    warn = hook.interpret(PRE_TOOL_USE, 3, "", "boom")
    assert warn.action == "allow" and "code 3" in warn.warnings[0] and "boom" in warn.warnings[0]
    json_deny = hook.interpret(PRE_TOOL_USE, 0, '{"decision": "deny", "reason": "nope"}', "")
    assert json_deny.action == "deny" and json_deny.note == "nope"
    rewrite = hook.interpret(PRE_TOOL_USE, 0, '{"updated_input": {"a": 1}}', "")
    assert rewrite.action == "modify" and rewrite.replacement_input == {"a": 1}
    # updated_input is PreToolUse-only. A PostToolUse block reaches the engine
    # as a deny, which appends the reason to the result.
    assert hook.interpret(POST_TOOL_USE, 0, '{"updated_input": {"a": 1}}', "").action == "allow"
    late = hook.interpret(POST_TOOL_USE, 2, "", "too late")
    assert late.action == "deny" and late.note == "too late"
    assert hook.interpret(PRE_TOOL_USE, 0, "plain text", "").action == "allow"
    # Events with nothing left to block turn a block into a warning.
    start = CommandHook(event="SessionStart", command="x")
    early = start.interpret("SessionStart", 2, "", "no")
    assert early.action == "allow" and "cannot be blocked" in early.warnings[0]


def test_bus_accumulates_warnings() -> None:
    import asyncio

    bus = HookBus()

    async def warn(event, payload):
        return HookDecision(warnings=("first",))

    async def deny(event, payload):
        return HookDecision(action="deny", note="no", warnings=("second",))

    bus.on(PRE_TOOL_USE, warn)
    bus.on(PRE_TOOL_USE, deny)
    decision = asyncio.run(bus.emit(PRE_TOOL_USE, {"name": "t", "input": {}}))
    assert decision.action == "deny"
    assert decision.warnings == ("first", "second")


# ─── real subprocess hooks ───────────────────────────────────────────────────

_HOOK_SCRIPT = """\
import json, sys, time
payload = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\\n")
mode = sys.argv[2]
if mode == "deny":
    sys.stderr.write("no rm for you")
    sys.exit(2)
if mode == "rewrite":
    print(json.dumps({"updated_input": {"city": "Berlin"}}))
if mode == "sleep":
    time.sleep(30)
if mode == "fail":
    sys.exit(3)
"""


def _hook_command(tmp_path: Path, mode: str) -> tuple[str, Path]:
    """A shell command string running the hook script via this interpreter.

    Forward-slash paths keep the string valid for Git Bash, cmd.exe and POSIX sh.
    """
    script = tmp_path / "hook.py"
    script.write_text(_HOOK_SCRIPT, encoding="utf-8")
    out = tmp_path / f"{mode}.jsonl"
    py = Path(sys.executable).as_posix()
    return f'"{py}" "{script.as_posix()}" "{out.as_posix()}" {mode}', out


def _payloads(out: Path) -> list[dict]:
    if not out.exists():
        return []
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]


async def test_command_hook_denies_with_exit_2(tmp_path) -> None:
    command, out = _hook_command(tmp_path, "deny")
    hook = CommandHook(event=PRE_TOOL_USE, command=command, matcher="Bash", cwd=str(tmp_path))
    decision = await hook(PRE_TOOL_USE, {"name": "Bash", "input": {"command": "rm -rf /"},
                                         "session_id": "s1", "tool_use_id": "t1"})
    assert decision is not None and decision.action == "deny"
    assert decision.note == "no rm for you"
    (payload,) = _payloads(out)
    assert payload["event"] == "PreToolUse"
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"] == {"command": "rm -rf /"}
    assert payload["session_id"] == "s1"
    assert payload["cwd"] == str(tmp_path)
    # The matcher skips other tools without spawning anything.
    assert await hook(PRE_TOOL_USE, {"name": "Read", "input": {}}) is None
    assert len(_payloads(out)) == 1


async def test_command_hook_rewrites_input(tmp_path) -> None:
    command, _ = _hook_command(tmp_path, "rewrite")
    hook = CommandHook(event=PRE_TOOL_USE, command=command, cwd=str(tmp_path))
    decision = await hook(PRE_TOOL_USE, {"name": "wx", "input": {"city": "Paris"}})
    assert decision is not None and decision.action == "modify"
    assert decision.replacement_input == {"city": "Berlin"}


async def test_command_hook_timeout_allows_with_warning(tmp_path) -> None:
    command, _ = _hook_command(tmp_path, "sleep")
    hook = CommandHook(event=PRE_TOOL_USE, command=command, timeout=0.5, cwd=str(tmp_path))
    decision = await hook(PRE_TOOL_USE, {"name": "Bash", "input": {}})
    assert decision is not None and decision.action == "allow"
    assert len(decision.warnings) == 1 and "timed out" in decision.warnings[0]


async def test_command_hook_nonzero_exit_warns(tmp_path) -> None:
    command, _ = _hook_command(tmp_path, "fail")
    hook = CommandHook(event=TURN_END, command=command, cwd=str(tmp_path))
    decision = await hook(TURN_END, {"session_id": "s", "turn_id": "t", "text": "hi"})
    assert decision is not None and decision.action == "allow"
    assert "code 3" in decision.warnings[0]


# ─── through the engine ──────────────────────────────────────────────────────


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _tool(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _weather_registry() -> ToolRegistry:
    @tool("get_weather", "Get weather",
          {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("wx", [get_weather]))
    return registry


def _config_with_hooks(tmp_path: Path, hooks: list[dict]) -> HarnessConfig:
    return HarnessConfig.from_dict({
        "cwd": str(tmp_path),
        "providers": {"anthropic": {"kind": "anthropic"}},
        "permission": {"approval_policy": "never_ask"},
        "hooks": hooks,
    })


async def _run_turn(engine: NativeLoop, prompt: str = "weather?") -> list:
    await engine.submit(UserTurn(content=prompt))
    await engine.wait_turn()
    return list(engine.bus.log(engine.session_id))


async def test_engine_hook_deny_short_circuits_before_permissions(tmp_path) -> None:
    command, out = _hook_command(tmp_path, "deny")
    config = _config_with_hooks(tmp_path, [
        {"event": "PreToolUse", "matcher": "mcp__wx__*", "command": command},
    ])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("blocked")])
    # always_ask would park on a prompt if the hook did not run first.
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
        permissions=PermissionEngine("always_ask"),
    )
    assert isinstance(engine, NativeLoop)
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error and result.output == "blocked by hook: no rm for you"
    denial = next(e for e in events if isinstance(e, HookEvent))
    assert denial.level == "deny" and denial.tool_name == "mcp__wx__get_weather"
    assert render_hook_event(denial) == "[hook] denied mcp__wx__get_weather: no rm for you"
    # Only the PreToolUse hook is configured, so exactly one payload was written.
    assert [p["event"] for p in _payloads(out)] == ["PreToolUse"]


async def test_engine_hook_rewrites_input_and_post_hook_sees_output(tmp_path) -> None:
    pre_command, _ = _hook_command(tmp_path, "rewrite")
    post_command, post_out = _hook_command(tmp_path, "post")
    config = _config_with_hooks(tmp_path, [
        {"event": "PreToolUse", "command": pre_command},
        {"event": "PostToolUse", "matcher": "mcp__wx__get_weather", "command": post_command},
    ])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("done")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
    )
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error and "Berlin: sunny" in result.output
    (payload,) = _payloads(post_out)
    assert payload["event"] == "PostToolUse"
    assert payload["tool_name"] == "mcp__wx__get_weather"
    assert payload["tool_input"] == {"city": "Berlin"}
    assert "Berlin: sunny" in payload["tool_output"]
    assert payload["is_error"] is False
    assert payload["session_id"] == engine.session_id


async def test_engine_turn_hooks_receive_turn_id_and_final_text(tmp_path) -> None:
    start_command, start_out = _hook_command(tmp_path, "start")
    end_command, end_out = _hook_command(tmp_path, "end")
    config = _config_with_hooks(tmp_path, [
        {"event": "TurnStart", "command": start_command},
        {"event": "TurnEnd", "command": end_command},
    ])
    provider = ScriptedProvider([_text("It is sunny in Paris.")])
    engine = build_engine(config, MemoryStore(), provider=provider)
    await _run_turn(engine)
    (start,) = _payloads(start_out)
    (end,) = _payloads(end_out)
    assert start["event"] == "TurnStart" and start["turn_id"]
    assert end["event"] == "TurnEnd" and end["turn_id"] == start["turn_id"]
    assert end["text"] == "It is sunny in Paris."
    assert "tool_name" not in end


async def test_engine_timeout_surfaces_warning_event(tmp_path) -> None:
    command, _ = _hook_command(tmp_path, "sleep")
    config = _config_with_hooks(tmp_path, [
        {"event": "TurnStart", "command": command, "timeout": 0.5},
    ])
    engine = build_engine(config, MemoryStore(), provider=ScriptedProvider([_text("ok")]))
    events = await _run_turn(engine)
    warning = next(e for e in events if isinstance(e, HookEvent))
    assert warning.level == "warning" and "timed out" in warning.message
    assert render_hook_event(warning).startswith("[hook] TurnStart hook")
    assert any(type(e).__name__ == "TurnComplete" for e in events)


def test_build_engine_appends_config_hooks_to_programmatic_bus(tmp_path) -> None:
    config = _config_with_hooks(tmp_path, [{"event": "TurnEnd", "command": "echo hi"}])
    bus = HookBus()

    async def mine(event, payload):
        return None

    bus.on(TURN_END, mine)
    engine = build_engine(config, MemoryStore(), provider=ScriptedProvider([_text("x")]),
                          hooks=bus)
    assert engine.hooks is bus
    callbacks = bus._hooks[TURN_END]
    assert callbacks[0] is mine
    assert isinstance(callbacks[1], CommandHook)
    assert callbacks[1].cwd == str(tmp_path)

    # No config hooks and no bus passed: the engine keeps hooks=None.
    plain = HarnessConfig.from_dict({"providers": {"anthropic": {"kind": "anthropic"}}})
    assert build_engine(plain, MemoryStore(), provider=ScriptedProvider([_text("x")])).hooks is None


def test_register_config_hooks_accepts_bus_shaped_object(tmp_path) -> None:
    seen: list[tuple[str, object]] = []

    class Bus:
        def on(self, event, callback):
            seen.append((event, callback))

    registered = register_config_hooks(
        Bus(), [HookConfig(event="PostToolUse", command="fmt", matcher="Edit", timeout=3)],
        cwd="/x",
    )
    assert [e for e, _ in seen] == ["PostToolUse"]
    assert registered[0].matcher == "Edit" and registered[0].timeout == 3.0



async def test_post_tool_hook_sees_the_input_of_a_denied_call(tmp_path) -> None:
    """CL-6: a denial is the most useful negative evidence there is, and the
    PostToolUse payload (and so the journal) used to carry ``input: None``
    for it."""
    from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
    from fluid.hooks.bus import POST_TOOL_USE, HookBus
    from fluid.permissions.rules import Rule

    seen: list[dict] = []
    bus = HookBus()

    async def capture(event: str, payload: dict):
        seen.append(dict(payload))
        return None

    bus.on(POST_TOOL_USE, capture)
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("denied")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
        permissions=PermissionEngine(
            "never_ask", rules=[Rule(tool="mcp__wx__get_weather", decision="deny")],
        ),
        hooks=bus,
    )
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error
    assert seen and seen[0]["input"] == {"city": "Paris"} and seen[0]["is_error"]
