"""The standalone CLI host: Task and Monitor work without an embedding app.

Offline end to end — a scripted provider drives the parent (and, for Task, the
child) through spawn → park → completion → wake. Monitor runs a real short
``python -c`` subprocess so the detached-process path is exercised on every
platform the suite runs on.
"""

import asyncio
import io
import json
import sys
from typing import AsyncIterator

import pytest

from fluid.cli.__main__ import run_once
from fluid.cli.config import host_of, make_engine
from fluid.cli.host import GENERIC_SUBAGENT_PROMPT, CliHost, Worker
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
from fluid.config.model import HarnessConfig
from fluid.events import (
    PermissionRequest,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    TurnComplete,
    UserTurn,
)
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore

from tests.unit.test_tool_loop import ScriptedProvider, _text, _tool

PY = sys.executable


def _config(tmp_path, **overrides) -> HarnessConfig:
    d = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path), **overrides})
    return HarnessConfig.from_dict(d)


async def _allow(event: PermissionRequest) -> str:
    return "allow"


def _monitor_call(command: str, tuid: str = "call-mon") -> list[StreamChunk]:
    return _tool(tuid, "Monitor", json.dumps({"command": command}))


def _task_call(mode: str, agent: str = "helper", tuid: str = "call-task") -> list[StreamChunk]:
    return _tool(
        tuid, "Task",
        json.dumps({"agent": agent, "prompt": "find the answer", "mode": mode, "label": "research"}),
    )


def _system_text(request: ProviderRequest) -> str:
    system = request.system
    if isinstance(system, list):
        return "\n".join(str(b.get("text", "")) for b in system)
    return system or ""


class RoutingProvider:
    """Parent and child share one ProviderClient; scripts are picked by the
    system prompt so each engine replays its own story."""

    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, parent: list[list[StreamChunk]], child: list[list[StreamChunk]],
                 child_marker: str = GENERIC_SUBAGENT_PROMPT[:40]) -> None:
        self.parent, self.child, self.marker = parent, child, child_marker
        self.parent_calls = 0
        self.child_calls = 0
        self.requests: list[ProviderRequest] = []
        self.child_requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        if self.marker in _system_text(request):
            self.child_requests.append(request)
            script = self.child[min(self.child_calls, len(self.child) - 1)]
            self.child_calls += 1
        else:
            script = self.parent[min(self.parent_calls, len(self.parent) - 1)]
            self.parent_calls += 1
        for chunk in script:
            yield chunk


# ── Monitor ──


async def test_monitor_receipt_park_finish_wake_in_one_shot_run(tmp_path) -> None:
    provider = ScriptedProvider([
        _monitor_call(f'"{PY}" -c "print(\'hi from monitor\')"'),
        _text("Started the build; waiting."),
        _text("The monitored command finished."),
    ])
    out = io.StringIO()
    store = MemoryStore()

    rc = await run_once(_config(tmp_path), store, "watch it", provider=provider,
                        out=out, on_permission=_allow)

    assert rc == 0
    text = out.getvalue()
    assert "Monitoring started" in text
    assert "[parked]" in text
    assert "[woken]" in text
    assert "[background] finished" in text
    assert "The monitored command finished." in text
    assert provider.calls == 3
    wake = provider.requests[-1].messages[-1].text()
    assert "hi from monitor" in wake
    assert "exit code 0" in wake


async def test_monitor_nonzero_exit_reports_failed(tmp_path) -> None:
    provider = ScriptedProvider([
        _monitor_call(f'"{PY}" -c "import sys; print(\'boom\'); sys.exit(3)"'),
        _text("waiting"),
        _text("noted the failure"),
    ])
    out = io.StringIO()
    await run_once(_config(tmp_path), MemoryStore(), "go", provider=provider,
                   out=out, on_permission=_allow)
    assert "[background] failed" in out.getvalue()
    wake = provider.requests[-1].messages[-1].text()
    assert "exit code 3" in wake and "boom" in wake


async def test_monitor_cancel_terminates_process_and_wakes_with_cancelled(tmp_path) -> None:
    provider = ScriptedProvider([
        _monitor_call(f'"{PY}" -c "import time; time.sleep(60)"'),
        _text("waiting"),
        _text("ok, cancelled"),
    ])
    store = MemoryStore()
    meta = await store.create_session()
    engine = make_engine(_config(tmp_path, permission={"approval_policy": "never_ask"}),
                         store, provider=provider, session_id=meta.session_id)
    host = host_of(engine)
    assert isinstance(host, CliHost)
    try:
        await engine.submit(UserTurn(content="start"))
        # The parent turn ends while the process sleeps: the session parks.
        for _ in range(200):
            if any(isinstance(e, SessionParked) for e in engine.bus.log(meta.session_id)):
                break
            await asyncio.sleep(0.02)
        running = host.running()
        assert len(running) == 1 and running[0].kind == "monitor"
        worker = running[0]
        assert worker.process is not None and worker.process.returncode is None

        assert await host.cancel(worker.task_id)
        await engine.wait_turn()

        assert worker.process.returncode is not None
        events = engine.bus.log(meta.session_id)
        assert any(isinstance(e, SubagentEvent) and e.kind == "cancelled" for e in events)
        assert any(isinstance(e, SessionWoken) for e in events)
        assert "cancelled" in provider.requests[-1].messages[-1].text()
        assert not await host.cancel(worker.task_id)  # already done
    finally:
        await host.aclose()
        await engine.aclose()


def test_worker_output_buffer_keeps_head_and_tail() -> None:
    w = Worker(task_id="t", kind="monitor", description="x")
    w.feed("A" * 4_000)
    w.feed("B" * 100_000)
    w.feed("END")
    out = w.output()
    assert out.startswith("A" * 4_000)
    assert out.endswith("END")
    assert "[output truncated]" in out


# ── Task ──


async def test_task_background_child_text_reaches_parent_wake(tmp_path) -> None:
    provider = RoutingProvider(
        parent=[_task_call("background"), _text("delegated; waiting"), _text("42 it is")],
        child=[_text("The answer is 42.")],
    )
    out = io.StringIO()
    store = MemoryStore()

    rc = await run_once(_config(tmp_path), store, "research", provider=provider,
                        out=out, on_permission=_allow)

    assert rc == 0
    text = out.getvalue()
    assert "[background] started" in text
    # The scripted child finishes before the parent turn ends, so no park:
    # the completion queues and wakes the session right after TurnComplete.
    assert "[woken]" in text
    assert "42 it is" in text
    assert provider.child_calls == 1
    assert provider.parent_calls == 3
    assert "The answer is 42." in provider.requests[-1].messages[-1].text()
    # The child does not get a Task tool of its own (no recursive fan-out).
    child_tools = {t["name"] for t in provider.child_requests[0].tools}
    assert "Task" not in child_tools
    assert "Monitor" in child_tools
    # The child has its own session in the shared store.
    assert len(store._sessions) == 2


async def test_task_sync_returns_child_text_inline(tmp_path) -> None:
    provider = RoutingProvider(
        parent=[_task_call("sync"), _text("done")],
        child=[_text("sync child report")],
    )
    out = io.StringIO()
    await run_once(_config(tmp_path), MemoryStore(), "go", provider=provider,
                   out=out, on_permission=_allow)
    text = out.getvalue()
    assert "[tool] sync child report" in text
    assert "[parked]" not in text
    assert provider.parent_calls == 2
    # The child's report rides back as the Task tool_result block.
    result_block = provider.requests[-1].messages[-1].content[0]
    assert result_block.content[0].text == "sync child report"
    assert not result_block.is_error


async def test_task_uses_agent_definition_prompt_and_tools(tmp_path) -> None:
    agents = tmp_path / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.md").write_text(
        "---\nname: helper\ndescription: finds things\ntools: Read, Grep\n---\n"
        "You are HELPER-PROMPT. Only read.\n",
        encoding="utf-8",
    )
    provider = RoutingProvider(
        parent=[_task_call("sync"), _text("done")],
        child=[_text("helper says hi")],
        child_marker="HELPER-PROMPT",
    )
    out = io.StringIO()
    await run_once(_config(tmp_path), MemoryStore(), "go", provider=provider,
                   out=out, on_permission=_allow)
    assert "helper says hi" in out.getvalue()
    assert provider.child_calls == 1
    child_req = provider.child_requests[0]
    assert "HELPER-PROMPT" in _system_text(child_req)
    assert GENERIC_SUBAGENT_PROMPT not in _system_text(child_req)
    assert {t["name"] for t in child_req.tools} == {"Read", "Grep"}


async def test_child_permission_requests_route_to_responder(tmp_path) -> None:
    asked: list[PermissionRequest] = []

    async def responder(event: PermissionRequest) -> str:
        asked.append(event)
        return "allow" if event.name == "Task" else "deny"

    provider = RoutingProvider(
        parent=[_task_call("sync"), _text("done")],
        child=[
            _tool("c1", "Write", json.dumps({"file_path": str(tmp_path / "x"), "content": "x"})),
            _text("could not write"),
        ],
    )
    out = io.StringIO()
    await run_once(_config(tmp_path, permission={"approval_policy": "always_ask"}),
                   MemoryStore(), "go", provider=provider, out=out, on_permission=responder)
    names = [e.name for e in asked]
    assert "Task" in names and "Write" in names
    assert not (tmp_path / "x").exists()
    assert "could not write" in out.getvalue()


async def test_background_task_cancel(tmp_path) -> None:
    release = asyncio.Event()

    class SlowChildProvider(RoutingProvider):
        async def stream(self, request):
            if self.marker in _system_text(request):
                await release.wait()
            async for chunk in super().stream(request):
                yield chunk

    provider = SlowChildProvider(
        parent=[_task_call("background"), _text("waiting"), _text("fine, cancelled")],
        child=[_text("never delivered")],
    )
    store = MemoryStore()
    meta = await store.create_session()
    engine = make_engine(_config(tmp_path, permission={"approval_policy": "never_ask"}),
                         store, provider=provider, session_id=meta.session_id)
    host = host_of(engine)
    try:
        await engine.submit(UserTurn(content="start"))
        for _ in range(200):
            if any(isinstance(e, SessionParked) for e in engine.bus.log(meta.session_id)):
                break
            await asyncio.sleep(0.02)
        worker = host.running()[0]
        assert worker.kind == "task" and worker.worker_session_id is not None
        assert await host.cancel(worker.task_id)
        await engine.wait_turn()
        events = engine.bus.log(meta.session_id)
        assert any(isinstance(e, SubagentEvent) and e.kind == "cancelled" for e in events)
        assert any(isinstance(e, SessionWoken) for e in events)
        assert "never delivered" not in provider.requests[-1].messages[-1].text()
    finally:
        release.set()
        await host.aclose()
        await engine.aclose()


async def test_host_close_kills_running_monitor(tmp_path) -> None:
    provider = ScriptedProvider([
        _monitor_call(f'"{PY}" -c "import time; time.sleep(60)"'),
        _text("waiting"),
    ])
    store = MemoryStore()
    meta = await store.create_session()
    engine = make_engine(_config(tmp_path, permission={"approval_policy": "never_ask"}),
                         store, provider=provider, session_id=meta.session_id)
    host = host_of(engine)
    await engine.submit(UserTurn(content="start"))
    for _ in range(200):
        if host.running():
            break
        await asyncio.sleep(0.02)
    worker = host.running()[0]
    await host.aclose()
    await engine.aclose()
    assert worker.process.returncode is not None
    assert worker.done


def test_make_engine_without_host_leaves_tools_unavailable(tmp_path) -> None:
    engine = make_engine(_config(tmp_path), MemoryStore(),
                         provider=ScriptedProvider([_text("x")]), background_host=False)
    assert host_of(engine) is None
    assert "task_dispatch" not in engine.ctx.extra
