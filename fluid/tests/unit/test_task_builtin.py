"""Task builtin (A6/A13): fluid owns the shape, the host injects the dispatcher."""

import asyncio

import pytest
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.task import SPEC, handler
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
)


def test_task_registered_bare_named():
    registry = ToolRegistry()
    register_builtins(registry)
    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert "Task" in names


def test_spec_shape():
    assert SPEC.name == "Task"
    assert SPEC.risk == "write"
    props = SPEC.input_schema["properties"]
    assert set(SPEC.input_schema["required"]) == {"agent", "prompt"}
    assert set(props["mode"]["enum"]) == {"sync", "background"}


@pytest.mark.asyncio
async def test_no_dispatcher_reports_unavailable():
    out = await handler({"agent": "x", "prompt": "y"}, ToolCtx())
    assert out.is_error
    assert "not available" in out.text


@pytest.mark.asyncio
async def test_dispatcher_invoked_and_outcome_passthrough():
    seen = {}

    async def dispatch(input, ctx):
        seen.update(input)
        return ToolOutcome(text="report: done")

    ctx = ToolCtx(extra={"task_dispatch": dispatch})
    out = await handler({"agent": "helper", "prompt": "go", "mode": "background"}, ctx)
    assert not out.is_error
    assert out.text == "report: done"
    assert seen["agent"] == "helper" and seen["mode"] == "background"


@pytest.mark.asyncio
async def test_dispatcher_string_result_wrapped():
    async def dispatch(input, ctx):
        return "plain string"

    out = await handler({"agent": "a", "prompt": "p"}, ToolCtx(extra={"task_dispatch": dispatch}))
    assert isinstance(out, ToolOutcome) and out.text == "plain string"


@pytest.mark.asyncio
async def test_background_receipt_notifies_session_lifecycle():
    seen = []

    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(
            task_id="task-1", worker_session_id="worker-1", description="research"
        )

    async def started(receipt):
        seen.append(receipt)

    out = await handler(
        {"agent": "helper", "prompt": "go", "mode": "background"},
        ToolCtx(extra={
            "task_dispatch": dispatch,
            "background_task_started": started,
        }),
    )

    assert out.background_task == seen[0]
    assert out.background_task.task_id == "task-1"
    assert not out.is_error


@pytest.mark.asyncio
async def test_background_spawn_crosses_parent_cancellation_boundary():
    dispatched = asyncio.Event()
    release = asyncio.Event()
    registered = asyncio.Event()

    async def dispatch(input, ctx):
        dispatched.set()
        await release.wait()
        return BackgroundTaskReceipt(task_id="task-detached")

    async def started(receipt):
        registered.set()

    tracked = set()
    call = asyncio.create_task(handler(
        {"agent": "helper", "prompt": "go", "mode": "background"},
        ToolCtx(extra={
            "task_dispatch": dispatch,
            "background_task_started": started,
            "background_spawn_tasks": tracked,
        }),
    ))
    await dispatched.wait()
    call.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.wait_for(registered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not tracked


def test_delegated_disallowed_tools_argv():
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.claude_cmd = ["claude"]
    loop.model = "fable"
    loop.permission_mode = None
    loop.cli_session_id = None
    loop._mcp_config_path = None
    loop.append_system_prompt = None
    loop.system = None
    loop.add_dirs = None
    loop.allowed_tools = None
    loop.disallowed_tools = ["Task", "Agent", "Workflow"]
    loop.partial = False
    loop.settings = None
    loop.effort = None
    argv = loop._build_argv()
    i = argv.index("--disallowed-tools")
    assert argv[i + 1] == "Task,Agent,Workflow"


def test_delegated_effort_argv():
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.claude_cmd = ["claude"]
    loop.model = "fable"
    loop.permission_mode = None
    loop.cli_session_id = None
    loop._mcp_config_path = None
    loop.append_system_prompt = None
    loop.system = None
    loop.add_dirs = None
    loop.allowed_tools = None
    loop.disallowed_tools = None
    loop.partial = False
    loop.settings = None
    loop.effort = "xhigh"
    argv = loop._build_argv()
    i = argv.index("--effort")
    assert argv[i + 1] == "xhigh"
