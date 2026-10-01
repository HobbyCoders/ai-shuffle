"""The query->SDK boundary->full prompt uses runtime bindings, not permissions."""
import dataclasses
from unittest.mock import AsyncMock

import pytest

from app.core import query_options as qo
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from fluid.tools.registry import ToolRegistry
from fluid.tools.builtins import read, task, monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [False, True])
@pytest.mark.parametrize("bound", [False, True])
async def test_query_options_compile_from_actual_wired_surface(mock_db, tmp_path, monkeypatch, preset, bound):
    user_text = "USER keeps Task and Monitor in their personal vocabulary."
    config = {"cwd": str(tmp_path), "model": "claude-sonnet-4-6",
              "tool_selection_mode": "allow", "allowed_tools": ["Read", "Task", "Monitor"],
              "system_prompt": ({"type": "preset", "preset": "claude_code", "append": user_text}
                                if preset else user_text)}
    monkeypatch.setattr(qo, "get_security_instructions", lambda: "SECURITY")
    sdk, _ = qo.build_options_from_profile({"id": "proof", "config": config},
                                           overrides={"cwd": str(tmp_path)})
    assert sdk.aih_automatic_workflows is True
    assert "belong in Monitor" not in str(sdk.system_prompt)
    adapter = ha.HarnessAdapter()
    capture = AsyncMock()
    monkeypatch.setattr(adapter, "connect", capture)
    await adapter.connect_with_sdk_options(sdk)
    options = capture.call_args.args[0]
    assert options.extra["automatic_workflows"] is True
    registry = ToolRegistry()
    for module in (read, task, monitor):
        registry.register_builtin(module.SPEC, module.handler)
    system, append = ha._system_parts(options.system_prompt)
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, append, options, native=True, code_mode=True,
        registry=registry,
        wired=frozenset(key for module in (task, monitor) for key in module.SPEC.requires_extra)
        if bound else frozenset(),
    )
    assert user_text in compiled.text and "SECURITY" in compiled.text
    assert ("Use only the roster IDs exposed by the current provider" in compiled.text) is bound
    assert ("belong in Monitor" in compiled.text) is bound
    assert ("# Subagent delegation" in compiled.text) is bound
    if not bound:
        assert "Task(mode" not in compiled.text


def test_worker_context_survives_without_nested_task(tmp_path, monkeypatch):
    from app.core import task_dispatch as td
    monkeypatch.setattr(td, "_is_worker_session", lambda sid: sid == "worker")
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "parent", 1))
    options = SessionOptions(model="fake", cwd=str(tmp_path), tool_allowlist=["Read", "Task"])
    options.extra.update(automatic_workflows=True, app_session_id="worker")
    registry = ToolRegistry()
    for module in (read, task): registry.register_builtin(module.SPEC, module.handler)
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "USER", None, options, registry=registry, wired=frozenset({"Read"}),
    )
    assert "# Subagent context" in compiled.text
    assert "Further delegation is unavailable" in compiled.text
    assert "Task roster" not in compiled.text
    assert "Top-level session orchestration is unavailable" in compiled.text


def test_delegated_preset_keeps_automatic_guidance_and_profile_text(tmp_path):
    options = SessionOptions(model="fake", cwd=str(tmp_path), tool_allowlist=["Read", "Monitor"])
    options.extra["automatic_workflows"] = True
    registry = ToolRegistry()
    registry.register_builtin(read.SPEC, read.handler)
    # The delegated leg serves Monitor only through the bundled bridge, as
    # mcp__builtins__Monitor, so register it under that name.
    registry.register_builtin(
        dataclasses.replace(monitor.SPEC, name="mcp__builtins__Monitor"), monitor.handler,
    )
    text = ha._delegated_append_text("USER", options, registry)
    assert "USER" in text and "belong in Monitor" in text
    assert "# Subagent delegation" not in text


@pytest.mark.parametrize("extra", [None, {}, {"automatic_workflows": False}])
def test_legacy_empty_extras_do_not_add_workflows(extra):
    options = SessionOptions(model="fake")
    options.extra = extra
    assert ha._workflow_prompt_blocks(options, frozenset({"Read"})) == []
