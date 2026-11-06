"""Resume refreshes the actual producer lease, including pooled/fresh engines."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core.config import settings
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.core.provider.events import ResultMessage
from app.core.session_plan import CURRENT_PLAN_RUN, run_context
from app.core.turn_recorder import TurnRecorder
from app.db import database, runs, session_events, session_plans
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolRegistry
from fluid.tools.builtins import plan


def update(revision=0, request_id="initial"):
    return {"action": "update", "expected_revision": revision, "request_id": request_id,
            "goal": "Resume owns its run", "done_when": "Plan survives a retry",
            "tasks": [{"id": "one", "title": "Retry", "status": "in_progress"}]}


class RetryProvider:
    name = "controlled"
    caps = ProviderCaps()

    def __init__(self):
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk("error", {"message": "Retry this request", "status": 529, "retriable": True})
            return
        yield StreamChunk("message_start", {"usage": {"input_tokens": 5}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use", "tool_use_id": "plan-update", "tool_name": "Plan"})
        yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(update())})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 4}, "stop_reason": "tool_use"})
        # On the following model call return text, not another mutation.
        self.stream = self.finished

    async def finished(self, request):
        yield StreamChunk("message_start", {"usage": {"input_tokens": 5}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "Plan saved"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": "end_turn"})


def adapter_for(sid, provider, store, fluid_id=None):
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="controlled", extra={"app_session_id": sid})
    ctx = adapter._with_away_hook(ToolCtx())
    registry = ToolRegistry()
    registry.register_builtin(plan.SPEC, plan.handler)
    adapter._registry = registry
    adapter._engine = NativeLoop(provider=provider, store=store, model="controlled",
                                 ctx=ctx, registry=registry, session_id=fluid_id)
    adapter._connected = True
    return adapter


async def finish(adapter):
    events = [event async for event in adapter.receive_events()]
    await adapter._engine.wait_turn()
    return events[-1]


@pytest.mark.parametrize("fresh", [False, True])
async def test_resume_claims_new_run_and_retained_backend_cannot_borrow_it(mock_db, fresh):
    sid = "retry-plan"
    database.create_session(sid, "default")
    provider, store = RetryProvider(), MemoryStore()
    adapter = adapter_for(sid, provider, store)
    first = await TurnRecorder.maybe_create(sid)
    try:
        with run_context(first):
            await adapter.send_query("Write a durable plan")
            result = await finish(adapter)
            assert result.is_error
            stale_backend = adapter._engine.ctx.extra["session_plan"]
            stale_call = stale_backend.capture()
        fluid_id = adapter._engine.session_id
    finally:
        await first.close()
    if fresh:
        await adapter.disconnect()
        adapter = adapter_for(sid, provider, store, fluid_id)
        await adapter._engine._ensure_session()
        adapter._sdk_session_id = fluid_id
        await adapter._anchor_replay_cursor(fluid_id)
    second = await TurnRecorder.maybe_create(sid)
    try:
        with run_context(second):
            await adapter.send_resume()
            result = await finish(adapter)
            assert isinstance(result, ResultMessage) and not result.is_error
            assert session_plans.read(sid)["plan"]["revision"] == 1
            with pytest.raises(ValueError, match="superseded"):
                stale_call(update(1, "late-thread"))
            with pytest.raises(ValueError):
                stale_backend(update(1, "retained-backend"))
        assert CURRENT_PLAN_RUN.get() is None
    finally:
        await second.close()
        await adapter.disconnect()
    assert runs.get_active_run(sid) is None
    assert len([e for e in session_events.get_events(sid) if e["event_type"] == "plan.updated"]) == 1


def test_unfenced_producer_cannot_borrow_enclosing_context():
    token = CURRENT_PLAN_RUN.set(("parent", "parent-run", 99))
    try:
        with run_context(None):
            assert CURRENT_PLAN_RUN.get() is None
        assert CURRENT_PLAN_RUN.get() == ("parent", "parent-run", 99)
    finally:
        CURRENT_PLAN_RUN.reset(token)


async def test_delayed_delegated_call_keeps_backend_before_pretool_await(mock_db):
    """The persistent MCP socket's old request cannot read the next turn's ctx."""
    from fluid.hooks.bus import HookDecision
    from fluid.tools.mcp.bundled_server import _gate_call
    from app.core.session_plan import Backend

    sid = "delayed-delegated-plan"
    database.create_session(sid, "default")
    old = runs.claim_run(sid, settings.effective_instance_id, 120)
    old_proof = (sid, old["id"], old["fence_token"])
    ctx = ToolCtx()
    ctx.extra["session_plan"] = Backend(lambda: sid, lambda: old_proof)
    registry = ToolRegistry()
    registry.register_builtin(plan.SPEC, plan.handler)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def pre_tool(*args):
        entered.set()
        await proceed.wait()
        return HookDecision()

    hooks = SimpleNamespace(emit=pre_tool, tool_allowed=AsyncMock())
    task = asyncio.create_task(_gate_call(registry, ctx, "Plan", update(),
                                        permissions=None, hooks=hooks))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runs.release_run(old["id"], old["fence_token"])
        new = runs.claim_run(sid, settings.effective_instance_id, 120)
        new_proof = (sid, new["id"], new["fence_token"])
        ctx.extra["session_plan"] = Backend(lambda: sid, lambda: new_proof)
        proceed.set()
        result = await asyncio.wait_for(task, 2)
        assert result.is_error and "superseded" in result.text
        assert session_plans.read(sid)["plan"]["revision"] == 0
        # The same persistent socket still permits a genuine new call.
        result = await _gate_call(registry, ctx, "Plan", update(), permissions=None, hooks=None)
        assert not result.is_error, result.text
        assert session_plans.read(sid)["plan"]["revision"] == 1
    finally:
        proceed.set()
        await task
        active = runs.get_active_run(sid)
        if active:
            runs.release_run(active["id"], active["fence_token"])
