"""Durable background-task park/wake behavior."""

import asyncio
import json

import pytest

import fluid.engine.native as native_module
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.native import NativeLoop
from fluid.events import (
    BackgroundTaskFinished,
    ErrorEvent,
    Interrupt,
    Interrupted,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    TurnComplete,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import register_builtins
from fluid.tools.mcp.bundled_server import _answer
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolRegistry
from tests.unit.test_tool_loop import ScriptedProvider, _text, _tool


def _task_call(task_id: str = "call-task"):
    return _tool(
        task_id,
        "Task",
        json.dumps({
            "agent": "helper",
            "prompt": "research it",
            "mode": "background",
            "label": "research",
        }),
    )


def _loop(provider, store, dispatch, *, session_id=None):
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(extra={"task_dispatch": dispatch})
    return NativeLoop(
        provider=provider,
        store=store,
        session_id=session_id,
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ctx,
    )


async def test_end_turn_parks_and_completion_wakes_session() -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(
            task_id="task-1",
            worker_session_id="worker-1",
            description=input.get("label"),
        )

    provider = ScriptedProvider([
        _task_call(),
        _text("I will wait for the background result."),
        _text("The background result is now incorporated."),
    ])
    loop = _loop(provider, MemoryStore(), dispatch)

    await loop.submit(UserTurn(content="start research"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    started = next(e for e in events if isinstance(e, SubagentEvent))
    parked = next(e for e in events if isinstance(e, SessionParked))
    terminal = next(e for e in events if isinstance(e, TurnComplete))
    assert started.kind == "started"
    assert started.worker_session_id == "worker-1"
    assert parked.pending_task_ids == ("task-1",)
    assert parked.seq < terminal.seq

    await loop.submit(BackgroundTaskFinished(
        task_id="task-1",
        summary="The answer is 42.",
        worker_session_id="worker-1",
    ))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    woke = next(e for e in events if isinstance(e, SessionWoken))
    assert woke.task_ids == ("task-1",)
    assert provider.calls == 3
    wake_request = provider.requests[-1]
    assert "The answer is 42." in wake_request.messages[-1].text()

    await loop.submit(BackgroundTaskFinished(task_id="task-1", summary="duplicate"))
    await loop.wait_turn()
    assert provider.calls == 3


async def test_interrupt_does_not_cancel_background_spawn_handshake() -> None:
    spawned = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def dispatch(input, ctx):
        spawned.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return BackgroundTaskReceipt(task_id="task-interrupt")

    provider = ScriptedProvider([_task_call(), _text("must not run")])
    loop = _loop(provider, MemoryStore(), dispatch)
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(spawned.wait(), timeout=1)
    await loop.submit(Interrupt())
    release.set()
    await asyncio.wait_for(loop.wait_turn(), timeout=2)

    events = loop.bus.log(loop.session_id)
    assert not cancelled.is_set()
    assert any(
        isinstance(e, SubagentEvent)
        and e.task_id == "task-interrupt"
        and e.kind == "started"
        for e in events
    )
    assert any(isinstance(e, SessionParked) for e in events)
    assert any(isinstance(e, Interrupted) for e in events)
    assert provider.calls == 1


async def test_interrupt_bounds_stalled_background_spawn_without_cancelling_it(
    monkeypatch,
) -> None:
    monkeypatch.setattr(native_module, "_BACKGROUND_SPAWN_INTERRUPT_GRACE", 0.01)
    spawned = asyncio.Event()
    release = asyncio.Event()
    registered = asyncio.Event()
    cancelled = asyncio.Event()

    async def dispatch(input, ctx):
        spawned.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return BackgroundTaskReceipt(task_id="task-stalled")

    provider = ScriptedProvider([_task_call(), _text("must not run")])
    loop = _loop(provider, MemoryStore(), dispatch)
    original_started = loop.ctx.extra["background_task_started"]

    async def observe_started(receipt):
        await original_started(receipt)
        registered.set()

    loop.ctx.extra["background_task_started"] = observe_started
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(spawned.wait(), timeout=1)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=1)

    assert not cancelled.is_set()
    assert any(
        isinstance(e, Interrupted) for e in loop.bus.log(loop.session_id)
    )
    release.set()
    await asyncio.wait_for(registered.wait(), timeout=1)
    assert not cancelled.is_set()
    await loop.aclose()


async def test_parked_tasks_rehydrate_and_wake_after_engine_restart(tmp_path) -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(task_id="task-restart")

    store = JsonlStore(tmp_path)
    first_provider = ScriptedProvider([
        _task_call(),
        _text("parked"),
    ])
    first = _loop(first_provider, store, dispatch, session_id="parent-session")
    await first.submit(UserTurn(content="start"))
    await first.wait_turn()
    await first.aclose()

    second_provider = ScriptedProvider([_text("resumed after restart")])
    second = _loop(
        second_provider,
        JsonlStore(tmp_path),
        dispatch,
        session_id="parent-session",
    )
    await second.submit(BackgroundTaskFinished(
        task_id="task-restart",
        summary="Durable result",
    ))
    await second.wait_turn()

    assert second_provider.calls == 1
    assert "Durable result" in second_provider.requests[0].messages[-1].text()
    persisted = await JsonlStore(tmp_path).events_since("parent-session", 0)
    woke = next(e for e in persisted if isinstance(e, SessionWoken))
    assert woke.task_ids == ("task-restart",)
    assert isinstance(woke.task_ids, tuple)


async def test_completion_before_receipt_is_serialized_without_phantom_pending() -> None:
    async def dispatch(input, ctx):
        raise AssertionError("not used")

    provider = ScriptedProvider([_text("consumed early completion")])
    loop = _loop(provider, MemoryStore(), dispatch, session_id="early-parent")

    await loop.submit(BackgroundTaskFinished(
        task_id="task-early",
        status="failed",
        summary="failed very quickly",
    ))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="task-early"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    lifecycle = [
        e.kind for e in events
        if isinstance(e, SubagentEvent) and e.task_id == "task-early"
    ]
    assert lifecycle == ["completion_pending", "started", "failed"]
    assert "task-early" not in loop._background_pending
    assert provider.calls == 1


async def test_wake_false_survives_restart_without_autonomous_turn(tmp_path) -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(task_id="task-no-wake")

    store = JsonlStore(tmp_path)
    first_provider = ScriptedProvider([_task_call(), _text("parked")])
    first = _loop(first_provider, store, dispatch, session_id="no-wake-parent")
    await first.submit(UserTurn(content="start"))
    await first.wait_turn()
    await first.submit(BackgroundTaskFinished(
        task_id="task-no-wake",
        summary="store only",
        wake=False,
    ))
    await first.wait_turn()
    assert first_provider.calls == 2
    await first.aclose()

    second_provider = ScriptedProvider([_text("manual user turn")])
    second = _loop(
        second_provider,
        JsonlStore(tmp_path),
        dispatch,
        session_id="no-wake-parent",
    )
    await second.submit(UserTurn(content="continue manually"))
    await second.wait_turn()
    assert second_provider.calls == 1
    assert not any(
        isinstance(e, SessionWoken)
        for e in second.bus.log(second.session_id)
    )


async def test_wait_turn_observes_already_failed_turn_task() -> None:
    async def dispatch(input, ctx):
        raise AssertionError("not used")

    loop = _loop(ScriptedProvider([_text("unused")]), MemoryStore(), dispatch)

    async def fail():
        raise RuntimeError("turn failed")

    loop._turn_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="turn failed"):
        await loop.wait_turn()


async def test_delegated_failed_wake_does_not_retry_forever() -> None:
    ctx = ToolCtx(extra={"task_dispatch": lambda input, ctx: None})
    loop = DelegatedLoop(
        store=MemoryStore(),
        model="test-model",
        ctx=ctx,
        claude_cmd=["/definitely/missing/claude"],
    )
    await loop._background_task_started(BackgroundTaskReceipt(task_id="task-fail-wake"))
    await loop.submit(BackgroundTaskFinished(
        task_id="task-fail-wake", summary="wake me"
    ))
    await asyncio.wait_for(loop.wait_turn(), timeout=1)

    errors = [
        e for e in loop.bus.log(loop.session_id) if isinstance(e, ErrorEvent)
    ]
    assert len(errors) == 1
    assert "spawn failed" in errors[0].message
    assert loop._background_wake_blocked
    assert "task-fail-wake" in loop._background_completions
    await loop.aclose()


async def test_delegated_bundled_task_bridge_registers_and_parks_receipt() -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(
            task_id="task-delegated", worker_session_id="worker-delegated"
        )

    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(extra={"task_dispatch": dispatch})
    loop = DelegatedLoop(
        store=MemoryStore(), model="test-model", registry=registry, ctx=ctx
    )

    response = await _answer(registry, ctx, {
        "op": "call",
        "name": "Task",
        "arguments": {
            "agent": "helper",
            "prompt": "go",
            "mode": "background",
        },
    })
    assert not response["is_error"]
    await loop._emit_terminal_with_park(TurnComplete(
        session_id=loop.session_id,
        stop_reason="end_turn",
        num_model_calls=1,
    ))
    events = loop.bus.log(loop.session_id)
    assert any(
        isinstance(e, SubagentEvent)
        and e.task_id == "task-delegated"
        and e.worker_session_id == "worker-delegated"
        for e in events
    )
    assert any(isinstance(e, SessionParked) for e in events)

    await loop.submit(BackgroundTaskFinished(
        task_id="task-delegated", summary="done", wake=False
    ))
    await loop.wait_turn()
    await loop.aclose()


# ─── serves: a service is tracked, but never parked on ───────────────────────


def _monitor_loop(provider, store, spawn, *, session_id=None):
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(extra={"monitor_spawn": spawn})
    return NativeLoop(
        provider=provider,
        store=store,
        session_id=session_id,
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ctx,
    )


def _monitor_call(command: str, *, serves: bool, call_id: str = "call-monitor"):
    args = {"command": command}
    if serves:
        args["serves"] = True
    return _tool(call_id, "Monitor", json.dumps(args))


async def test_serving_task_is_tracked_but_never_parked_on() -> None:
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="dev-server", description="npm run dev")

    provider = ScriptedProvider([
        _monitor_call("npm run dev", serves=True),
        _text("Dev server is up on 8010; carrying on."),
    ])
    loop = _monitor_loop(provider, MemoryStore(), spawn, session_id="serve-parent")

    await loop.submit(UserTurn(content="run the app"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    started = next(
        e for e in events if isinstance(e, SubagentEvent) and e.kind == "started"
    )
    # Registered and surfaced like any other background task…
    assert started.task_id == "dev-server"
    assert started.serves is True
    assert "dev-server" in loop._background_pending
    # …but the turn ended without parking on it.
    assert not any(isinstance(e, SessionParked) for e in events)
    assert loop._park_pending_ids() == ()


async def test_serving_and_ordinary_tasks_park_only_on_the_ordinary_one() -> None:
    async def spawn(input, ctx):
        command = input["command"]
        return BackgroundTaskReceipt(
            task_id="dev-server" if "dev" in command else "ci-job",
            description=command,
        )

    provider = ScriptedProvider([
        _monitor_call("npm run dev", serves=True, call_id="c1"),
        _monitor_call("gh pr checks 12 --watch --fail-fast", serves=False, call_id="c2"),
        _text("Server up, CI watching."),
    ])
    loop = _monitor_loop(provider, MemoryStore(), spawn, session_id="mixed-parent")

    await loop.submit(UserTurn(content="run app then wait on CI"))
    await loop.wait_turn()

    parked = next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, SessionParked)
    )
    assert parked.pending_task_ids == ("ci-job",)
    # Both are still tracked — only the park accounting differs.
    assert set(loop._background_pending) == {"dev-server", "ci-job"}


async def test_serves_survives_restart_and_still_is_not_parked_on(tmp_path) -> None:
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="dev-server")

    store = JsonlStore(tmp_path)
    first = _monitor_loop(
        ScriptedProvider([_monitor_call("npm run dev", serves=True), _text("up")]),
        store, spawn, session_id="serve-restart",
    )
    await first.submit(UserTurn(content="run the app"))
    await first.wait_turn()
    await first.aclose()

    # A durable SubagentEvent carries `serves`, so a fresh engine rebuilds the
    # receipt with it and the restart does not resurrect the park.
    persisted = await JsonlStore(tmp_path).events_since("serve-restart", 0)
    started = next(
        e for e in persisted if isinstance(e, SubagentEvent) and e.kind == "started"
    )
    assert started.serves is True

    second = _monitor_loop(
        ScriptedProvider([_text("still up")]), store, spawn,
        session_id="serve-restart",
    )
    await second._rehydrate_background_lifecycle_unlocked()
    assert second._background_pending["dev-server"].serves is True
    assert second._park_pending_ids() == ()
    await second._emit_terminal_with_park(TurnComplete(
        session_id=second.session_id, stop_reason="end_turn", num_model_calls=1,
    ))
    assert not any(
        isinstance(e, SessionParked) for e in second.bus.log(second.session_id)
    )
    await second.aclose()


async def test_serving_task_death_still_wakes_the_session() -> None:
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="dev-server")

    provider = ScriptedProvider([
        _monitor_call("npm run dev", serves=True),
        _text("up"),
        _text("The dev server died; restarting it."),
    ])
    loop = _monitor_loop(provider, MemoryStore(), spawn, session_id="serve-death")
    await loop.submit(UserTurn(content="run the app"))
    await loop.wait_turn()

    await loop.submit(BackgroundTaskFinished(
        task_id="dev-server",
        status="cancelled",
        summary="Stopped by user",
    ))
    await loop.wait_turn()

    assert provider.calls == 3
    assert "Stopped by user" in provider.requests[-1].messages[-1].text()
