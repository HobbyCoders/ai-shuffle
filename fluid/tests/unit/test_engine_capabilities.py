"""Engine contracts documented in docs/engines.md, using the wire-level fakes."""
import asyncio
from contextlib import aclosing

import pytest

from fluid.events.model import (
    ErrorEvent, Interrupted, PermissionRequest, SessionParked, SessionWoken, SubagentEvent, TextDelta,
    ToolResult, ToolUse, TurnComplete,
)
from fluid.events.ops import (
    BackgroundTaskFinished, Fork, Interrupt, RespondPermission, Resume, Rewind, UserTurn,
)
from fluid.messages import Message, Text
from fluid.permissions.rules import Rule
from fluid.store.base import SessionNode
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


async def test_native_current_limitation_interrupt_then_resume_is_rejected(server):
    server.script([Messages.start(), *Messages.text_block(0, "partial"), Sleep(30)])
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
        _, partial = await loop.store.load_path(loop.session_id)
        assert partial.message.role == "assistant"
        assert partial.message.text() and "partial".startswith(partial.message.text())
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(Resume())
        refused = await _event(loop, (ErrorEvent, TurnComplete), since=seq)
        assert isinstance(refused, ErrorEvent) and "nothing to resume" in refused.message
        await asyncio.wait_for(loop.wait_turn(), 10)
    finally:
        await run.aclose()


async def _seed_unanswered_prompt(loop, text):
    await loop.store.append(loop.session_id, SessionNode(
        node_id="unanswered", parent_id=None, message=Message(role="user", content=[Text(text)]),
    ))


async def test_native_refuses_fork_and_rewind_while_a_turn_runs(server):
    server.script(Messages.text("first"), Messages.text("second"))
    run = Harness(server)
    loop = run.loop
    # Turn two is held open while its reply is being stored, so the ops below
    # land in a running turn, not by timing.
    storing, release = asyncio.Event(), asyncio.Event()
    append = run.store.append

    async def gated_append(session_id, node):
        if node.message.role == "assistant" and node.message.text() == "second":
            storing.set()
            await release.wait()
        await append(session_id, node)

    run.store.append = gated_append
    try:
        assert isinstance((await run.turn("one"))[-1], TurnComplete)
        first_prompt = (await loop.store.load_path(loop.session_id))[0]
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(UserTurn(content="two"))
        assert "second".startswith((await _event(loop, TextDelta, since=seq)).text)
        await asyncio.wait_for(storing.wait(), 10)
        assert loop.turn_running
        for op in (Fork(from_node_id=first_prompt.node_id), Rewind(to_node_id=first_prompt.node_id)):
            with pytest.raises(RuntimeError, match="a turn is running"):
                await loop.submit(op)
        release.set()
        await asyncio.wait_for(loop.wait_turn(), 10)
        path = await loop.store.load_path(loop.session_id)
        assert [n.message.text() for n in path] == ["one", "first", "two", "second"]
        assert (await loop.store.get_session(loop.session_id)).head_node_id == path[-1].node_id
        # Between turns both still move the head.
        await loop.submit(Rewind(to_node_id=path[1].node_id))
        assert (await loop.store.get_session(loop.session_id)).head_node_id == path[1].node_id
        await loop.submit(Fork(from_node_id=first_prompt.node_id))
        assert (await loop.store.get_session(loop.session_id)).head_node_id == first_prompt.node_id
    finally:
        release.set()
        await run.aclose()


async def test_native_resumes_an_unanswered_prompt(server):
    server.script(Messages.text("resumed"))
    run = Harness(server)
    loop = run.loop
    try:
        await loop._ensure_session()
        await _seed_unanswered_prompt(loop, "original prompt")
        await loop.submit(Resume())
        assert isinstance(await _event(loop, (ErrorEvent, TurnComplete)), TurnComplete)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert len(server.requests) == 1
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


async def test_acp_current_limitation_interrupt_then_resume_is_rejected(tmp_path):
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
        _, partial = await loop.store.load_path(loop.session_id)
        assert partial.message.text() == "working"
        await loop.submit(Resume())
        refused = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(refused, ErrorEvent) and "Nothing to resume" in refused.message
        await asyncio.wait_for(loop.wait_turn(), 10)
    finally:
        await driver.close()


async def test_acp_resends_an_unanswered_prompt(tmp_path):
    driver = await _driver(tmp_path)
    loop = driver.loop
    try:
        await _seed_unanswered_prompt(loop, "original prompt")
        await loop.submit(Resume(context_note="\nretry note"))
        assert isinstance(await driver.until((TurnComplete, ErrorEvent), timeout=10), TurnComplete)
        [request] = _received(tmp_path, "session/prompt")
        assert [b["text"] for b in request["params"]["prompt"]] == ["original prompt", "\nretry note"]
        path = await loop.store.load_path(loop.session_id)
        assert [node.message.text() for node in path if node.message.role == "user"] == ["original prompt"]
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


async def test_native_dispatch_deny_rule_does_not_remove_tool_schema(server):
    server.script(tool_reply("anthropic", [{"id": "e1", "name": "Echo", "args": {"text": "denied"}}]),
                  Messages.text("done"))
    run = Harness(server)
    run.loop.permissions.rules.append(Rule(tool="Echo", decision="deny"))
    try:
        events = await run.turn()
        assert isinstance(events[-1], TurnComplete)
        assert "Echo" in {tool["name"] for tool in server.requests[0].body["tools"]}
        [result] = [event for event in events if isinstance(event, ToolResult)]
        assert result.name == "Echo" and result.is_error
        assert not run.ran
    finally:
        await run.aclose()


async def test_acp_passes_stdio_without_advertised_mcp_transports(tmp_path):
    driver = await _driver(tmp_path, mcp_servers={
        "local": {"command": "unused-stdio-command"},
        "http": {"type": "http", "url": "https://example.invalid/mcp"},
        "sse": {"type": "sse", "url": "https://example.invalid/sse"},
    })
    try:
        assert isinstance(await driver.turn("answer"), TurnComplete)
        [session] = _received(tmp_path, "session/new")
        assert [entry["name"] for entry in session["params"]["mcpServers"]] == ["local"]
    finally:
        await driver.close()


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
