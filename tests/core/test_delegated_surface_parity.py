"""The delegated (Claude subscription) leg offers what the picker offers.

The tool picker lists fluid's names (Task, Monitor, Remember, Recall, AskUser,
...). On the delegated leg those features are the bridged ``mcp__builtins__``
copies and the CLI's own tools, some under other names (AskUserQuestion,
PowerShell, NotebookEdit, LSP, and ToolSearch for deferred schemas).
``_delegated_surface`` maps the picker's names to this leg's, and every seam
reads it: the bridge filter, the ``--disallowed-tools`` wall, the PreToolUse
policy, the grants and the appended notes.

The CLI reads a bare name on ``--allowed-tools`` as a blanket pre-approval, so
the surface never goes there: only ``_delegated_permission_grants`` does, and it
never names Bash, Read or a file-writing tool.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.types import SessionOptions

FAKE = (
    Path(__file__).resolve().parents[2]
    / "fluid" / "tests" / "fixtures" / "fake_claude_script.py"
)
SID = "11111111-1111-1111-1111-111111111111"
PICKED = ["Read", "Bash", "Task", "Monitor", "Remember", "Recall", "AskUser", "WebSearch"]
# The CLI surface production session 42595afe announced (built-ins only).
ANNOUNCED = [
    "AskUserQuestion", "Bash", "DesignSync", "Edit", "EnterWorktree", "ExitWorktree",
    "Glob", "Grep", "ListAgents", "LSP", "NotebookEdit", "PowerShell",
    "PushNotification", "Read", "RemoteTrigger", "ReportFindings", "SendMessage",
    "Skill", "ToolSearch", "WebFetch", "WebSearch", "Write",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from app.core import cli_tool_surface, guidance_surface

    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))
    # The CLI carries its own Glob/Grep: no bundled copies, on any OS.
    monkeypatch.setattr(ha, "_cli_ships_search_tools", lambda: True)
    cli_tool_surface.reset_cli_tools()
    yield
    cli_tool_surface.reset_cli_tools()


@pytest.fixture
def builtins_server(tmp_path):
    from app.mcp.builtin_tools import create_builtin_tools_server

    server = create_builtin_tools_server(
        session_id=SID, is_admin=True, model="opus", cwd=str(tmp_path),
    )
    assert server is not None
    return server


def _options(tmp_path, servers=None, *, surface=None, mode="default", lane="code", **kw):
    options = SessionOptions(
        model="opus", cwd=str(tmp_path), permission_mode=mode,
        mcp_servers=servers or {}, tool_allowlist=surface, **kw,
    )
    options.extra["session_mode"] = lane
    return options


def _loop(options, registry=None):
    """A DelegatedLoop wired the way ``HarnessAdapter.connect`` wires it."""
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    return DelegatedLoop(
        store=MemoryStore(), model="opus", registry=registry,
        permission_mode=options.permission_mode,
        allowed_tools=ha._delegated_allowed(options),
        disallowed_tools=ha._delegated_disallowed(options),
        permission_grants=ha._delegated_permission_grants(options),
        claude_cmd=["claude"],
    )


def _flag(argv, name):
    return argv[argv.index(name) + 1].split(",") if name in argv else []


# ─── Allow mode keeps the picker's tools ────────────────────────────────────


@pytest.mark.asyncio
async def test_picker_names_reach_the_bridged_app_tools(tmp_path, builtins_server):
    from fluid.tools.mcp.bundled_server import _answer

    options = _options(tmp_path, {"builtins": builtins_server}, surface=PICKED)
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    loop = _loop(options, registry)
    try:
        for tool in ("Task", "Monitor", "Remember", "Recall"):
            assert loop._bridge_tool_permitted(f"mcp__builtins__{tool}"), tool
        assert not loop._bridge_tool_permitted("mcp__builtins__CronCreate")
        listed = await _answer(registry, loop.ctx, {"op": "list"},
                               tool_filter=loop._bridge_tool_permitted)
        names = {tool["name"] for tool in listed["tools"]}
        assert {"mcp__builtins__Task", "mcp__builtins__Monitor",
                "mcp__builtins__Remember", "mcp__builtins__Recall"} <= names
        assert "mcp__builtins__CronCreate" not in names
    finally:
        await loop.aclose()


def test_policy_lets_the_cli_spellings_of_picked_tools_through(tmp_path):
    options = _options(tmp_path, surface=PICKED)
    policy = ha._delegated_tool_policy(options, None, ha._delegated_disallowed(options))
    for name in ("AskUserQuestion", "WebSearch", "ToolSearch", "PowerShell", "Bash", "Read"):
        assert policy(name, {}) is None, name
    assert policy("Write", {}) is not None
    assert policy("Edit", {}) is not None


def test_warm_cache_does_not_wall_the_cli_spellings_of_picked_tools(tmp_path):
    from app.core import cli_tool_surface

    cli_tool_surface.record_cli_tools(ANNOUNCED)
    denied = set(ha._delegated_disallowed(_options(tmp_path, surface=PICKED)))
    assert not {"AskUserQuestion", "WebSearch", "ToolSearch", "PowerShell"} & denied
    assert {"Write", "Edit", "WebFetch", "DesignSync", "Glob"} <= denied


@pytest.mark.asyncio
async def test_guidance_describes_the_picked_app_tools(tmp_path, builtins_server):
    options = _options(tmp_path, {"builtins": builtins_server}, surface=PICKED)
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    blocks = ha._delegated_guidance_blocks(options, registry)
    [app_tools] = [b for b in blocks if b.startswith("# App tools on this session")]
    assert "Task" in app_tools and "Monitor" in app_tools
    assert "__Remember" in app_tools and "__Recall" in app_tools
    assert ha._MEMORY_BLOCK_CORE in blocks
    # PICKED has no Edit: the block would forbid the only permitted way to edit.
    assert ha.DELEGATED_TOOL_USE_BLOCK not in blocks


def test_unrestricted_sessions_are_unchanged(tmp_path):
    options = _options(tmp_path)
    assert ha._delegated_surface(options) is None
    assert ha._delegated_allowed(options) is None
    assert ha._delegated_tool_predicate(options) is None
    assert ha._delegated_disallowed(options) == sorted(ha.DELEGATED_BANNED_TOOLS)
    policy = ha._delegated_tool_policy(options, None, [])
    for name in ("Bash", "Write", "Edit", "WebFetch", "AskUserQuestion", "mcp__browser__x"):
        assert policy(name, {}) is None


# ─── The CLI grant is not the surface ───────────────────────────────────────


def test_allow_mode_code_session_grants_no_shell_or_file_writes(tmp_path):
    options = _options(
        tmp_path, surface=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    )
    argv = _loop(options)._build_argv()
    assert argv[argv.index("--permission-mode") + 1] == "default"
    granted = _flag(argv, "--allowed-tools")
    assert {"Glob", "Grep"} <= set(granted)
    assert not {"Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "Read"} & set(granted)
    walled = set(_flag(argv, "--disallowed-tools"))
    # Unselected CLI tools are still walled; selected ones are not.
    assert {"WebFetch", "WebSearch", "Skill", "AskUserQuestion"} <= walled
    assert not {"Bash", "Write", "Edit", "Read"} & walled


def test_plan_mode_still_grants_nothing(tmp_path):
    options = _options(tmp_path, surface=["Read", "Grep"], mode="plan")
    assert "--allowed-tools" not in _loop(options)._build_argv()


def test_code_lane_grants_follow_fluids_web_risk(tmp_path):
    grants = ha._delegated_permission_grants(_options(tmp_path))
    assert grants == ["Glob", "Grep", "WebSearch"]  # WebFetch is 'write' in fluid


def test_chat_argv_grants_the_attachment_reader_as_the_cli_calls_it(tmp_path):
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE

    options = _options(tmp_path, surface=list(CHAT_MODE_TOOL_SURFACE), lane="chat")
    granted = _flag(_loop(options)._build_argv(), "--allowed-tools")
    assert "mcp__fluid_bundled__mcp__attachments__Read" in granted
    assert "WebSearch" in granted
    # Chat reads uploaded documents; an injected one must not fetch unprompted.
    assert not {"Read", "Glob", "Grep", "Bash", "WebFetch"} & set(granted)


# ─── Duplicate tools ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_registry_leaves_toolsearch_to_the_cli(tmp_path, builtins_server):
    from app.core.provider.fluid_tool_bridge import NATIVE_SHADOWED_BUILTINS

    options = _options(tmp_path, {"builtins": builtins_server})
    delegated = await ha.HarnessAdapter()._build_registry(options, native=False)
    names = {spec.name for spec in delegated.list_tools(loaded_only=False)}
    assert "mcp__builtins__ToolSearch" not in names
    assert "mcp__builtins__Task" in names and "mcp__builtins__Remember" in names

    native = await ha.HarnessAdapter()._build_registry(options, native=True)
    native_names = {spec.name for spec in native.list_tools(loaded_only=False)}
    assert not {f"mcp__builtins__{n}" for n in NATIVE_SHADOWED_BUILTINS} & native_names


def test_cli_copies_of_app_tools_are_banned(tmp_path):
    denied = set(ha._delegated_disallowed(_options(tmp_path)))
    assert {"RemoteTrigger", "PushNotification", "ListAgents", "SendMessage"} <= denied


@pytest.mark.asyncio
async def test_no_builtins_leaf_duplicates_a_cli_builtin(tmp_path, builtins_server):
    """Every app builtin named like a CLI built-in is either banned on the CLI
    or not bridged, so the model never sees two tools of one name."""
    from app.core.cli_tool_surface import KNOWN_CLI_TOOLS
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools

    leaves = {tool.name for tool in recorded_sdk_server_tools(builtins_server) or ()}
    assert leaves, "the builtins server recorded no tools"
    options = _options(tmp_path, {"builtins": builtins_server})
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    for leaf in leaves & set(KNOWN_CLI_TOOLS):
        banned = leaf in ha.DELEGATED_BANNED_TOOLS
        skipped = f"mcp__builtins__{leaf}" not in names
        assert banned or skipped, leaf


@pytest.mark.asyncio
async def test_app_tools_note_names_the_stand_ins(tmp_path, builtins_server):
    options = _options(tmp_path, {"builtins": builtins_server})
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    [app_tools] = [
        b for b in ha._delegated_guidance_blocks(options, registry)
        if b.startswith("# App tools on this session")
    ]
    assert "__RemoteTrigger and __NotifyDevices" in app_tools
    assert "Claude Code's own RemoteTrigger and PushNotification are not available" in app_tools


# ─── End to end: an allow-mode Bash call reaches the app's approver ─────────


class PermissionResultAllow:  # the app's callback result, matched by class name
    def __init__(self, updated_input: dict | None = None) -> None:
        self.updated_input = updated_input


@pytest.mark.asyncio
async def test_allow_mode_bash_call_is_asked_in_default_mode(tmp_path, monkeypatch):
    """Before the fix the surface rode --allowed-tools, so the CLI ran a
    selected Bash call without asking even in Ask mode."""
    from fluid.tools.registry import ToolRegistry

    from app.core.provider.events import ResultMessage

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    script = tmp_path / "script.json"
    record = tmp_path / "record.jsonl"
    args = {"command": "git status"}
    script.write_text(json.dumps({"turns": [[
        {"op": "tool_use", "id": "toolu_1", "name": "Bash", "input": args},
        {"op": "ask", "id": "cli_req_1", "tool_use_id": "toolu_1", "name": "Bash",
         "input": args, "skip_if_allowed": True},
        {"op": "tool_result", "tool_use_id": "toolu_1", "content": "On branch main"},
        {"op": "text", "text": "clean"},
        {"op": "result"},
    ]]}), encoding="utf-8")
    options = SessionOptions(
        model="opus",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path), permission_mode="default",
        tool_allowlist=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
        env={"FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_RECORD": str(record)},
        extra={"fluid_registry": ToolRegistry()},
    )
    asked: list = []

    async def can_use_tool(name: str, tool_input: dict, context: Any):
        asked.append(name)
        return PermissionResultAllow()

    adapter = ha.HarnessAdapter()
    adapter._can_use_tool = can_use_tool
    await adapter.connect(options)
    try:
        async def turn():
            await adapter.send_query("status?")
            async for message in adapter.receive_events():
                if isinstance(message, ResultMessage):
                    return
        await asyncio.wait_for(turn(), 20.0)
    finally:
        await adapter.disconnect()

    assert asked == ["Bash"]
    rows = [json.loads(line) for line in record.read_text().splitlines() if line]
    [start] = [r for r in rows if r["kind"] == "start"]
    assert "Bash" not in _flag(start["argv"], "--allowed-tools")
    [decision] = [r for r in rows if r["kind"] == "decision"]
    assert decision["response"]["behavior"] == "allow"
    assert decision["response"].get("by") != "cli_rules"


# ─── Blocklists use the leg's spellings too ─────────────────────────────────


def test_blocklist_reaches_the_cli_spellings_of_blocked_tools(tmp_path):
    options = _options(tmp_path)
    options.disallowed_tools = ["Bash", "Edit", "Remember", "AskUser"]
    denied = ha._delegated_disallowed(options)
    assert {"PowerShell", "NotebookEdit", "AskUserQuestion", "mcp__builtins__Remember"} <= set(denied)
    assert "ToolSearch" not in denied

    policy = ha._delegated_tool_policy(options, None, denied)
    for name in ("Bash", "PowerShell", "NotebookEdit", "AskUserQuestion"):
        assert policy(name, {}) is not None, name
    assert policy("Read", {}) is None

    permit = ha._delegated_tool_predicate(options)
    assert not permit("mcp__builtins__Remember")
    assert permit("mcp__builtins__Recall")
    # No permitted shell is left, so the Bash-vs-dedicated-tools block is moot.
    assert ha.DELEGATED_TOOL_USE_BLOCK not in ha._delegated_guidance_blocks(options, None)

    loop = _loop(options)
    assert not loop._bridge_tool_permitted("mcp__builtins__Remember")
    assert loop._bridge_tool_permitted("mcp__builtins__Recall")


def test_allow_mode_walls_only_names_this_cli_can_show(tmp_path):
    from app.core import cli_tool_surface

    cli_tool_surface.record_cli_tools([
        *ANNOUNCED,
        "mcp__fluid_bundled__mcp__builtins__CronCreate",
        "mcp__browser__navigate",
        "mcp__someone_elses_server__query",
    ])
    browser = {"type": "stdio", "command": "browser-mcp"}
    options = _options(tmp_path, {"browser": browser}, surface=["Read"])
    denied = set(ha._delegated_disallowed(options))
    assert {"Bash", "Write", "DesignSync"} <= denied
    # This session's external server is connected by the CLI, so it is walled.
    assert "mcp__browser__navigate" in denied
    # The bridge filters the bundle; another session's server is not connected.
    assert "mcp__fluid_bundled__mcp__builtins__CronCreate" not in denied
    assert "mcp__someone_elses_server__query" not in denied


def test_seed_walls_every_built_in_production_announced(tmp_path):
    from app.core.cli_tool_surface import KNOWN_CLI_TOOLS

    assert set(ANNOUNCED) <= set(KNOWN_CLI_TOOLS)
    denied = set(ha._delegated_disallowed(_options(tmp_path, surface=["Read"])))
    assert {"DesignSync", "ReportFindings"} <= denied
