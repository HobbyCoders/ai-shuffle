"""SuggestTask is for separate, nonblocking work, on every surface that offers it.

The tool description is the one text every caller reads: it rides the native
request and the delegated bridge's always-loaded tier, including an agent whose
plain-string prompt replaces Claude Code's and so never carries the shared rule.
It owns the limits; the shared rule says what to do when noticing work, and a
subagent's role says where work the parent may need goes. These read what the
producers actually send. They do not show that a model follows the words.
"""
from __future__ import annotations

import pytest

from app.core import task_dispatch
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from fluid.cli.prompt import ADJACENT_WORK_RULE
from fluid.tools.builtins.session_orchestration import suggest_task_spec
from fluid.tools.mcp.bundled_server import _answer
from tests.core.test_delegated_bridge_tiers import _bridge, _is_deferred, _servers
from tests.core.test_delegated_bridge_tiers import _isolated as _bridge_isolated  # noqa: F401
from tests.core.test_native_first_request_budget import _first_request

# Each boundary the owner approved, in the words the description carries.
TOOL_LIMITS = (
    "separate, nonblocking work outside the user's request",
    "Never use it to defer what the request needs",
    "deliverables",
    "necessary fixes or investigation",
    "verification",
    "review",
    "authorized merge",
    "to replace Task/Swarm delegation",
    "a subagent returns such work to its parent",
    "Unrelated discoveries still earn a chip, no check needed",
    "For work related to the request or its root cause, first check existing tracking and active owners you can reach",
    "name same-root work and say why this is separate and nonblocking",
)
# What the old wording already promised and a chip still means.
TOOL_KEEPS = (
    "one click starts it in a separate chat with its own worktree",
    "Starts nothing now: do not fix it here, carry on",
)
RULE_LIMITS = (
    "Work outside the user's request:",
    "call SuggestTask with a self-contained brief and keep going",
    "If it blocks, make the smallest fix and say so",
    "not nitpicks or work that is part of the request",
    "What the request needs stays with this chat, even when it takes its own PR",
)
CHILD_RULE = (
    "Return anything the parent's request may need there, never as a SuggestTask chip; "
    "still suggest what is clearly separate from it."
)


def _suggest_task(tools: list[dict]) -> dict:
    """The one SuggestTask entry, under its bare or bridged name."""
    found = [tool for tool in tools if tool["name"].rsplit("__", 1)[-1] == "SuggestTask"]
    assert len(found) == 1, [tool["name"] for tool in tools]
    return found[0]


def _assert_tool_limits(description: str) -> None:
    for phrase in (*TOOL_LIMITS, *TOOL_KEEPS):
        assert phrase in description, phrase


def _worker(monkeypatch) -> None:
    monkeypatch.setattr(task_dispatch, "_is_worker_session", lambda _sid: True)
    monkeypatch.setattr(task_dispatch, "caller_chain", lambda _sid: ([], "parent", 1))


def _worker_options(tmp_path, **kwargs) -> SessionOptions:
    options = SessionOptions(model="fake", cwd=str(tmp_path), **kwargs)
    options.extra.update(automatic_workflows=True, app_session_id="worker")
    return options


def test_shared_rule_keeps_necessary_work_with_this_chat():
    for phrase in RULE_LIMITS:
        assert phrase in ADJACENT_WORK_RULE, phrase


def test_the_limits_change_words_only():
    """Model choice, the chip's inputs, its risk and direct calling all stay."""
    spec = suggest_task_spec()
    _assert_tool_limits(spec.description)
    schema = spec.input_schema
    assert set(schema["properties"]) == {"title", "summary", "prompt", "model", "effort", "base", "request_id"}
    assert schema["required"] == ["title", "summary", "prompt", "model", "request_id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["summary"]["description"] == "What is wrong and why it matters."
    assert schema["properties"]["prompt"]["description"] == "Standalone brief; the new chat has no transcript."
    assert "following the owner's saved model-routing memory" in schema["properties"]["model"]["description"]
    assert spec.risk == "safe" and spec.requires_extra == ("task_chips",) and spec.code_mode_allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code-mode", "json-tools"])
async def test_native_provider_request_carries_rule_and_limits(monkeypatch, tmp_path, code_mode):
    """Through a real connect, as captured at the provider."""
    # _first_request makes its own home folder; the autouse fixture already did.
    root = tmp_path / "native"
    root.mkdir()
    _engine, request, _reported = await _first_request(monkeypatch, root, code_mode=code_mode)
    assert request.system.count(ADJACENT_WORK_RULE) == 1
    tool = _suggest_task(request.tools)
    _assert_tool_limits(tool["description"])
    # The limits have one owner, the description the model reads to call it.
    assert "Never use it to defer" not in request.system


@pytest.mark.asyncio
async def test_delegated_preset_carries_rule_and_loaded_limits(tmp_path):
    """The bridge's tool list as the CLI child reads it, and its appended prompt."""
    options, registry, tools = await _bridge(tmp_path)
    tool = _suggest_task(tools)
    assert not _is_deferred(tool)
    _assert_tool_limits(tool["description"])
    text = ha._delegated_append_text("PROFILE", options, registry)
    assert text.count(ADJACENT_WORK_RULE) == 1
    assert "Never use it to defer" not in text


def test_child_role_returns_needed_work_to_the_parent(monkeypatch):
    _worker(monkeypatch)
    role = task_dispatch.subagent_system_prompt("worker")
    assert role.count(CHILD_RULE) == 1
    assert "You do not have the parent's conversation" in role
    assert "returned to the parent" in role


def test_child_role_does_not_name_a_tool_it_cannot_call(monkeypatch):
    _worker(monkeypatch)
    role = task_dispatch.subagent_system_prompt("worker", suggest_task_available=False)
    assert "SuggestTask" not in role
    assert "returned to the parent" in role


def test_native_child_brief_follows_the_wired_surface(monkeypatch, tmp_path):
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    _worker(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)

    def compiled(wired):
        return ha.HarnessAdapter._compile_prompt_sections(
            "AGENT", None, _worker_options(tmp_path), registry=registry, wired=wired).text

    offered = compiled(frozenset({"task_chips"}))
    assert offered.count(CHILD_RULE) == 1 and offered.count(ADJACENT_WORK_RULE) == 1
    missing = compiled(frozenset())
    assert "# Subagent context" in missing
    assert "SuggestTask" not in missing


@pytest.mark.asyncio
async def test_delegated_preset_child_brief_follows_the_bridge(monkeypatch, tmp_path):
    _worker(monkeypatch)
    options, registry, _tools = await _bridge(tmp_path)
    options.extra.update(automatic_workflows=True, app_session_id="worker")
    offered = ha._delegated_append_text("PROFILE", options, registry)
    assert offered.count(CHILD_RULE) == 1 and offered.count(ADJACENT_WORK_RULE) == 1

    # An API-user or automation chain's server does not list the tool.
    from app.mcp.builtin_tools import create_builtin_tools_server

    options.mcp_servers["builtins"] = create_builtin_tools_server(
        session_id="worker", profile_id="p", project_id=None, is_admin=True,
        model="opus", provider_id=None, cwd=str(tmp_path), suggest_task=False,
    )
    without = await ha.HarnessAdapter()._build_registry(options, native=False)
    missing = ha._delegated_append_text("PROFILE", options, without)
    assert "# Subagent context" in missing
    assert CHILD_RULE not in missing and ADJACENT_WORK_RULE not in missing


def test_custom_prompt_delegated_child_brief_carries_the_parent_rule(monkeypatch, tmp_path):
    _worker(monkeypatch)
    compiled = ha.HarnessAdapter._compile_prompt("CUSTOM AGENT", None, _worker_options(tmp_path))
    assert compiled.count(CHILD_RULE) == 1


def _connect_options(tmp_path, system_prompt) -> SessionOptions:
    options = SessionOptions(
        model="opus", cwd=str(tmp_path), system_prompt=system_prompt, permission_mode="default",
        mcp_servers=_servers(str(tmp_path)),
    )
    options.extra.update(session_mode="code", automatic_workflows=True, app_session_id="worker")
    return options


@pytest.mark.asyncio
@pytest.mark.parametrize("system_prompt, shared_rule", [
    ({"type": "preset", "preset": "claude_code", "append": "PROFILE"}, True),
    ("CUSTOM AGENT", False),
], ids=["preset-append", "custom-replace"])
async def test_a_real_delegated_connect_hands_the_cli_the_child_rule(monkeypatch, tmp_path, system_prompt, shared_rule):
    """The prompt a real connect hands the CLI for a Task worker, and the tool
    that CLI loads, on the two shapes a delegated prompt takes."""
    _worker(monkeypatch)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    adapter = ha.HarnessAdapter()
    await adapter.connect(_connect_options(tmp_path, system_prompt))
    try:
        loop = adapter.engine
        sent = "\n\n".join(part for part in (loop.system, loop.append_system_prompt) if part)
        registry = adapter._registry
        listed = await _answer(registry, loop.ctx, {"op": "list"},
                               tool_filter=loop._bridge_tool_permitted, deferred=loop.bridge_deferred)
    finally:
        await adapter.disconnect()
    assert sent.count(CHILD_RULE) == 1
    if shared_rule:
        assert sent.count(ADJACENT_WORK_RULE) == 1
    # A custom prompt replaces the preset and adds no shared rule, so this
    # always-loaded description is what tells that agent what a chip is for.
    tool = _suggest_task(listed["tools"])
    assert not _is_deferred(tool)
    _assert_tool_limits(tool["description"])
