"""Monitor builtin: fluid owns the shape, the host injects the spawner."""

import asyncio

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.monitor import SPEC, handler
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
)


def test_monitor_registered_bare_named():
    registry = ToolRegistry()
    register_builtins(registry)
    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert "Monitor" in names


def test_spec_shape():
    assert SPEC.name == "Monitor"
    assert SPEC.risk == "write"
    props = SPEC.input_schema["properties"]
    assert set(SPEC.input_schema["required"]) == {"command"}
    assert "cwd" in props


@pytest.mark.asyncio
async def test_no_spawner_reports_unavailable():
    out = await handler({"command": "make build"}, ToolCtx())
    assert out.is_error
    assert "not available" in out.text


@pytest.mark.asyncio
async def test_receipt_result_notifies_session_lifecycle():
    seen = []

    async def spawn(input, ctx):
        return BackgroundTaskReceipt(
            task_id="mon-1", worker_session_id=None, description="ci check"
        )

    async def started(receipt):
        seen.append(receipt)

    ctx = ToolCtx(extra={"monitor_spawn": spawn, "background_task_started": started})
    out = await handler({"command": "npm run build"}, ctx)
    assert not out.is_error
    assert out.background_task == seen[0]
    assert out.background_task.task_id == "mon-1"


@pytest.mark.asyncio
async def test_mapping_and_outcome_results_normalized():
    async def spawn_mapping(input, ctx):
        return {"task_id": "mon-2", "text": "watching logs"}

    out = await handler(
        {"command": "tail -f app.log"},
        ToolCtx(extra={"monitor_spawn": spawn_mapping}),
    )
    assert not out.is_error
    assert out.background_task is not None
    assert out.background_task.task_id == "mon-2"
    assert out.text == "watching logs"

    receipt = BackgroundTaskReceipt(task_id="mon-3")

    async def spawn_receipt(input, ctx):
        return ToolOutcome(text=f"ok {receipt.task_id}", background_task=receipt)

    out2 = await handler(
        {"command": "cargo build"}, ToolCtx(extra={"monitor_spawn": spawn_receipt})
    )
    assert out2.text == "ok mon-3" and out2.background_task is receipt


@pytest.mark.asyncio
async def test_spawn_error_is_surfaced_without_receipt():
    async def spawn(input, ctx):
        raise RuntimeError("spawn failed: no such file")

    # Registry.dispatch converts handler exceptions into error outcomes, so
    # exercise the same path the engine uses.
    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Monitor", {"command": "x"}, ToolCtx(extra={"monitor_spawn": spawn}))
    assert out.is_error
    assert out.background_task is None
    assert "spawn failed" in out.text


@pytest.mark.asyncio
async def test_spawn_crosses_parent_cancellation_boundary():
    spawned = asyncio.Event()
    release = asyncio.Event()
    registered = asyncio.Event()
    cancelled = asyncio.Event()

    async def spawn(input, ctx):
        spawned.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return BackgroundTaskReceipt(task_id="mon-detached")

    async def started(receipt):
        registered.set()

    tracked = set()
    call = asyncio.create_task(handler(
        {"command": "long-ci-run"},
        ToolCtx(extra={
            "monitor_spawn": spawn,
            "background_task_started": started,
            "background_spawn_tasks": tracked,
        }),
    ))
    await spawned.wait()
    call.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.wait_for(registered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not cancelled.is_set()
    assert not tracked
