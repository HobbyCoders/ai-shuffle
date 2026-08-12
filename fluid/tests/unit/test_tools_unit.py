"""M2 unit tests — registry/inproc, Edit discipline, permissions, hooks."""

import pytest

from loom.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from loom.permissions.engine import PermissionEngine, plan_mode_tools
from loom.permissions.rules import Rule, evaluate_rules
from loom.tools.builtins import register_builtins
from loom.tools.inproc import create_inproc_server, tool
from loom.tools.registry import ToolCtx, ToolRegistry, ToolSpec


async def test_inproc_server_naming_and_dispatch() -> None:
    @tool("hello", "Say hi", {"type": "object", "properties": {"name": {"type": "string"}}})
    async def hello(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"hi {args['name']}"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("srv", [hello]))
    assert registry.get("mcp__srv__hello") is not None
    assert [s.name for s in registry.list_tools()] == ["mcp__srv__hello"]

    out = await registry.dispatch("mcp__srv__hello", {"name": "loom"}, ToolCtx())
    assert out.text == "hi loom"
    assert not out.is_error


async def test_inproc_accepts_plain_str_and_tooloutcome() -> None:
    from loom.tools.registry import ToolOutcome

    @tool("s", "returns str", {"type": "object"})
    async def s(args: dict):
        return "plain string"

    @tool("o", "returns outcome", {"type": "object"})
    async def o(args: dict):
        return ToolOutcome(text="structured", ui={"x": 1})

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("srv", [s, o]))
    assert (await registry.dispatch("mcp__srv__s", {}, ToolCtx())).text == "plain string"
    outcome = await registry.dispatch("mcp__srv__o", {}, ToolCtx())
    assert outcome.text == "structured" and outcome.ui == {"x": 1}


async def test_edit_read_before_write_and_uniqueness(tmp_path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 1\nz = 2\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))

    # Edit before Read → refused.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "z = 2", "new_string": "z = 3"}, ctx)
    assert out.is_error and "must Read" in out.text

    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    # Non-unique old_string without replace_all → refused.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "= 1", "new_string": "= 9"}, ctx)
    assert out.is_error and "not unique" in out.text

    # replace_all succeeds.
    out = await registry.dispatch(
        "Edit", {"file_path": str(f), "old_string": "= 1", "new_string": "= 9", "replace_all": True}, ctx
    )
    assert not out.is_error
    assert f.read_text() == "x = 9\ny = 9\nz = 2\n"

    # Unique edit succeeds.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "z = 2", "new_string": "z = 3"}, ctx)
    assert not out.is_error and f.read_text().endswith("z = 3\n")


async def test_write_then_edit_allowed(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    f = tmp_path / "new.txt"
    await registry.dispatch("Write", {"file_path": str(f), "content": "hello"}, ctx)
    # Write registers the file as known, so a follow-up Edit is allowed.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "hello", "new_string": "bye"}, ctx)
    assert not out.is_error and f.read_text() == "bye"


def test_permission_policies() -> None:
    safe = ToolSpec(name="Read", description="", input_schema={}, risk="safe")
    write = ToolSpec(name="Edit", description="", input_schema={}, risk="write")

    assert PermissionEngine("never_ask").evaluate(write, {}) == "allow"
    assert PermissionEngine("always_ask").evaluate(safe, {}) == "ask"
    on_write = PermissionEngine("on_write")
    assert on_write.evaluate(safe, {}) == "allow"
    assert on_write.evaluate(write, {}) == "ask"


def test_rules_last_match_wins_and_input_globs() -> None:
    rules = [
        Rule(tool="*", decision="allow"),
        Rule(tool="Bash", decision="ask"),
        Rule(tool="Bash", decision="deny", input_globs={"command": "rm *"}),
    ]
    assert evaluate_rules(rules, "Read", {}) == "allow"
    assert evaluate_rules(rules, "Bash", {"command": "ls"}) == "ask"
    assert evaluate_rules(rules, "Bash", {"command": "rm -rf /"}) == "deny"


def test_doom_loop_forces_ask_after_three() -> None:
    engine = PermissionEngine("never_ask")
    call = ("Bash", {"command": "flaky"})
    assert engine.note_execution(*call) is False  # 1
    assert engine.note_execution(*call) is False  # 2
    assert engine.note_execution(*call) is True   # 3 → force ask, then reset
    assert engine.note_execution(*call) is False  # streak reset
    # a distinct call resets the streak immediately
    assert engine.note_execution("Bash", {"command": "other"}) is False


def test_plan_mode_tools_filter() -> None:
    specs = [
        ToolSpec(name="Read", description="", input_schema={}, risk="safe"),
        ToolSpec(name="Bash", description="", input_schema={}, risk="write"),
        ToolSpec(name="Rm", description="", input_schema={}, risk="destructive"),
    ]
    kept = {t.name for t in plan_mode_tools(specs)}
    assert kept == {"Read"}


async def test_hook_modify_replaces_input() -> None:
    bus = HookBus()

    async def rewrite(event: str, payload: dict):
        return HookDecision(action="modify", replacement_input={"command": "safe"})

    bus.on(PRE_TOOL_USE, rewrite)
    decision = await bus.emit(PRE_TOOL_USE, {"name": "Bash", "input": {"command": "danger"}})
    assert decision.action == "modify"
    assert decision.replacement_input == {"command": "safe"}


def test_search_tools_ranks_by_match() -> None:
    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="Read", description="read a file", input_schema={}), _noop
    )
    registry.register_builtin(
        ToolSpec(name="Grep", description="search file contents by regex", input_schema={}), _noop
    )
    hits = [s.name for s in registry.search_tools("search file")]
    assert hits[0] == "Grep"  # matches both words, ranked first


async def _noop(input, ctx):  # pragma: no cover - registration placeholder
    return "ok"
