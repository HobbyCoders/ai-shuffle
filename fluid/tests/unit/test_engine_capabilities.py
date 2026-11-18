"""Engine contracts documented in docs/engines.md, using the wire-level fakes."""
import asyncio
from contextlib import aclosing

import pytest

from fluid.events.model import (
    ErrorEvent, Interrupted, PermissionRequest, SessionParked, SessionWoken, SubagentEvent, TextDelta,
    ToolUse, TurnComplete,
)
from fluid.events.ops import BackgroundTaskFinished, Interrupt, RespondPermission, Resume, UserTurn
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from tests.unit.test_acp_engine import _driver, _received
from tests.unit.test_delegated_scripted_cli import Scripted, _interruptible_turn, _tool_turn
from tests.unit.test_native_scripted_provider import Harness, Messages, Sleep, server, tool_reply


async def _event(loop, kind, *, since=0):
    async with asyncio.timeout(10), aclosing(loop.events(since_seq=since)) as events:
        async for event in events:
            if isinstance(event, kind):
                return event
    raise AssertionError(f"no {kind} event")


async def test_native_interrupt_and_resume_unanswered_prompt(server):
    server.script([Messages.start(), *Messages.text_block(0, "partial"), Sleep(30)],
                  Messages.text("resumed"))
    run = Harness(server)
    loop = run.loop
    try:
        await loop._ensure_session()
        await loop.submit(UserTurn(content="original prompt"))
        await _event(loop, TextDelta)
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert isinstance(loop.bus.log(loop.session_id)[-1], Interrupted)
        # Resume targets an unanswered user node, not a partial reply kept by Stop.
        [prompt, partial] = await loop.store.load_path(loop.session_id)
        assert partial.message.role == "assistant"
        assert partial.message.text() and "partial".startswith(partial.message.text())
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(Resume())
        refused = await _event(loop, (ErrorEvent, TurnComplete), since=seq)
        assert isinstance(refused, ErrorEvent) and "nothing to resume" in refused.message
        await asyncio.wait_for(loop.wait_turn(), 10)
        await loop.store.set_head(loop.session_id, prompt.node_id)
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(Resume())
        await _event(loop, TurnComplete, since=seq)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert len(server.requests) == 2
        stored = await run.stored()
        assert [(m.role, m.text()) for m in stored] == [
            ("user", "original prompt"), ("assistant", "resumed"),
        ]
    finally:
        await run.aclose()


async def test_delegated_rejects_resume_but_can_interrupt(tmp_path):
    run = Scripted(tmp_path, {"turns": [_interruptible_turn("success")]}, partial=True)
    try:
        with pytest.raises(NotImplementedError, match="Resume"):
            await run.loop.submit(Resume())

        async def interrupt(event):
            if isinstance(event, TextDelta):
                await run.loop.submit(Interrupt())

        assert isinstance((await run.turn("stop me", on_event=interrupt))[-1], Interrupted)
        assert run.stdin_frames("control_request", "interrupt")
    finally:
        await run.aclose()


async def test_acp_interrupt_and_resume_unanswered_prompt(tmp_path):
    driver = await _driver(tmp_path)
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content="slow"))
        await driver.until(TextDelta, timeout=10)
        await loop.submit(Interrupt())
        assert isinstance(await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10),
                          Interrupted)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert _received(tmp_path, "session/cancel")
        [prompt, partial] = await loop.store.load_path(loop.session_id)
        assert partial.message.text() == "working"
        await loop.submit(Resume())
        refused = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(refused, ErrorEvent) and "Nothing to resume" in refused.message
        await asyncio.wait_for(loop.wait_turn(), 10)
        await loop.store.set_head(loop.session_id, prompt.node_id)
        await loop.submit(Resume(context_note="\nretry note"))
        # The fake waits again on the retried prompt; the new prompt is observable
        # on the wire before it is cancelled, just like the first one.
        await driver.until(TextDelta, timeout=10)
        first, second = _received(tmp_path, "session/prompt")
        assert [b["text"] for b in first["params"]["prompt"]] == ["slow"]
        assert [b["text"] for b in second["params"]["prompt"]] == ["slow", "\nretry note"]
        await loop.submit(Interrupt())
        assert isinstance(await driver.until(Interrupted, timeout=10), Interrupted)
        path = await loop.store.load_path(loop.session_id)
        assert [node.message.text() for node in path if node.message.role == "user"] == ["slow"]
    finally:
        await driver.close()


@pytest.mark.parametrize("engine", ["native", "delegated"])
async def test_background_completion_parks_and_wakes_in_the_engine(engine, server, tmp_path):
    if engine == "native":
        server.script(*(Messages.text(text) for text in ("ready", "parked", "awake")))
        run = Harness(server)
    else:
        run = Scripted(tmp_path, {})
    try:
        assert isinstance((await run.turn("establish session"))[-1], TurnComplete)
        loop = run.loop
        sid = loop.session_id
        # This is the receipt seam that the host's Task/Monitor dispatcher uses.
        started = loop.ctx.extra["background_task_started"]
        await started(BackgroundTaskReceipt(task_id="job-1"))
        events = await run.turn("work is still running")
        [parked] = [event for event in events if isinstance(event, SessionParked)]
        assert parked.pending_task_ids == ("job-1",)
        seq = loop.bus.last_seq(sid)
        await loop.submit(BackgroundTaskFinished(task_id="job-1", summary="job done"))
        await _event(loop, TurnComplete, since=seq)
        events = [event for event in loop.bus.log(sid) if event.seq > seq]
        [done] = [event for event in events if isinstance(event, SubagentEvent)]
        assert (done.task_id, done.kind) == ("job-1", "finished")
        [wake] = [event for event in events if isinstance(event, SessionWoken)]
        assert wake.task_ids == ("job-1",)
        if engine == "native":
            assert len(server.requests) == 3
        else:
            assert len(run.stdin_frames("user")) == 3
    finally:
        await run.aclose()


async def test_acp_leaves_completion_and_wake_to_host(tmp_path):
    driver = await _driver(tmp_path)
    loop = driver.loop
    try:
        assert isinstance(await driver.turn("hello"), TurnComplete)
        await asyncio.wait_for(loop.wait_turn(), 10)
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(BackgroundTaskFinished(task_id="job-1"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert loop.bus.last_seq(loop.session_id) == seq
        assert len(_received(tmp_path, "session/prompt")) == 1
    finally:
        await driver.close()


async def test_native_advertises_and_dispatches_registry_builtins(server):
    server.script(tool_reply("anthropic", [{"id": "e1", "name": "Echo", "args": {"text": "hi"}}]),
                  Messages.text("done"))
    run = Harness(server)
    try:
        assert isinstance((await run.turn())[-1], TurnComplete)
        assert {t["name"] for t in server.requests[0].body["tools"]} == {"Echo", "Write"}
        assert run.ran == [("Echo", {"text": "hi"})]
    finally:
        await run.aclose()


async def test_delegated_builtins_are_announced_and_run_by_cli(tmp_path):
    run = Scripted(tmp_path, {"init": {"tools": ["Bash", "Read"]},
                              "turns": [_tool_turn(ask=False)]})
    try:
        events = await run.turn("list files")
        assert run.loop.cli_tools == ("Bash", "Read")
        assert [event.name for event in events if isinstance(event, ToolUse)] == ["Bash"]
        assert run.loop.registry is None
    finally:
        await run.aclose()


async def test_acp_tools_come_from_agent_or_host_bridge(tmp_path):
    registry = ToolRegistry()
    ran = []

    async def echo(args, ctx):
        ran.append(args)
        return ToolOutcome(text="host reply")

    registry.register_builtin(ToolSpec(name="Echo", input_schema={"type": "object"},
                                       description="echo"), echo)
    driver = await _driver(tmp_path, registry=registry, ctx=ToolCtx(cwd=str(tmp_path)))
    try:
        assert isinstance(await driver.turn('mcp Echo {"text":"hi"}'), TurnComplete)
        assert ran == [{"text": "hi"}]
        assert driver.loop.bridged_tools() == ["Echo"]
        servers = _received(tmp_path, "session/new")[0]["params"]["mcpServers"]
        assert [entry["name"] for entry in servers] == ["fluid_bundled"]
        assert [event.name for event in driver.events if isinstance(event, ToolUse)] == [
            "mcp__fluid_bundled__Echo",
        ]
        assert not driver.loop.caps.inproc_tools
        await driver.loop.submit(UserTurn(content="tool"))
        ask = await driver.until(PermissionRequest)
        await driver.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow"))
        assert isinstance(await driver.until((TurnComplete, ErrorEvent)), TurnComplete)
        assert [event.name for event in driver.events if isinstance(event, ToolUse)] == [
            "mcp__fluid_bundled__Echo", "Edit",
        ]
        assert ran == [{"text": "hi"}]
    finally:
        await driver.close()
