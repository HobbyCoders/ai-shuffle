"""Every Python cell is write-class; in-process guards are defence in depth."""

from __future__ import annotations

import asyncio

import pytest

from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.engine.native import NativeLoop
from fluid.events import PermissionRequest, RespondPermission, ToolResult, UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

from .test_codemode import ScriptedProvider, _run_python, _text


def _spec_and_session():
    registry = ToolRegistry()
    session = CodeModeSession(registry)
    register_code_mode_tools(registry, session)
    spec = next(s for s in registry.list_tools(loaded_only=False) if s.name == "run_python")
    return spec, session


def test_run_python_is_always_write_class() -> None:
    spec, _ = _spec_and_session()
    assert spec.risk == "write"
    assert spec.effective_risk({"code": "1"}) == "write"
    assert PermissionEngine("on_write").evaluate(spec, {"code": "1"}) == "ask"
    assert PermissionEngine("never_ask").evaluate(spec, {"code": "1"}) == "allow"


async def test_ask_mode_prompts_for_each_python_cell() -> None:
    registry = ToolRegistry()
    session = CodeModeSession(registry)
    provider = ScriptedProvider([_run_python("print(6 * 7)"), _run_python("print(7 * 6)"), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session, permissions=PermissionEngine("on_write"),
    )
    asked: list[PermissionRequest] = []

    async def _answer() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                asked.append(event)
                await loop.submit(RespondPermission(
                    request_id=event.request_id, decision="allow",
                ))

    watcher = asyncio.create_task(_answer())
    await asyncio.sleep(0)
    try:
        await loop.submit(UserTurn(content="compute"))
        await asyncio.wait_for(loop.wait_turn(), timeout=60)
        rp = next(
            e for e in loop.bus.log(sid)
            if isinstance(e, ToolResult) and e.name == "run_python"
        )
        assert "42" in rp.output
        assert [(r.name, r.risk) for r in asked] == [
            ("run_python", "write"), ("run_python", "write"),
        ]
    finally:
        watcher.cancel()
        await loop.aclose()


async def test_bypass_runs_cell_and_projected_write_without_approval() -> None:
    registry = ToolRegistry()
    executed = []

    async def write_marker(input, ctx):
        executed.append(input["value"])
        return ToolOutcome(text="marker written")

    registry.register_builtin(
        ToolSpec(
            name="write_marker", description="Write a test marker", risk="write",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        write_marker,
    )
    session = CodeModeSession(registry)
    provider = ScriptedProvider([
        _run_python("from tools import write_marker\nwrite_marker(value='bypass')"),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session, permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="write marker"))
        await asyncio.wait_for(loop.wait_turn(), timeout=60)
        assert executed == ["bypass"]
        assert not any(isinstance(e, PermissionRequest) for e in loop.bus.log(sid))
        assert any(
            isinstance(e, ToolResult) and e.name == "run_python" and "marker written" in e.output
            for e in loop.bus.log(sid)
        )
    finally:
        await loop.aclose()


def test_run_python_prompt_shows_plain_code_and_why() -> None:
    """A readable title and explanation, with code cut at 20 lines / 1500 chars."""
    from fluid.codemode.session import RUN_PYTHON_REASON, RUN_PYTHON_TITLE, code_preview, run_python_prompt

    twenty = "\n".join(f"a{i} = {i}" for i in range(20))
    assert code_preview(twenty) == twenty
    assert code_preview(twenty + "\nb = 1").endswith("\n… (1 more line not shown)")
    assert code_preview("x" * 2000) == "x" * 1500 + " … (truncated)"
    shown = run_python_prompt({"code": 'print("hi")\n'})
    assert shown["title"] == RUN_PYTHON_TITLE == "Run Python code"
    assert shown["description"] == RUN_PYTHON_REASON + '\n\nprint("hi")'
    assert "not an OS security boundary" in shown["description"]
    assert run_python_prompt({"code": "  "})["description"] == RUN_PYTHON_REASON
    assert run_python_prompt("junk")["description"] == RUN_PYTHON_REASON
