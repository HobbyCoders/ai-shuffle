"""Engine contracts documented in docs/engines.md, using the wire-level fakes."""
import asyncio
from contextlib import aclosing

import pytest

from fluid.engine.native import INTERRUPTED_META_KEY
from fluid.events.model import (
    BlockStop, ErrorEvent, Usage, Interrupted, PermissionRequest, SessionParked, SessionWoken, SubagentEvent, TextDelta,
    ToolResult, ToolUse, TurnComplete, TurnStarted,
)
from fluid.events.ops import (
    BackgroundTaskFinished, Fork, Interrupt, RespondPermission, Resume, Rewind, UserTurn,
)
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.rules import Rule
from fluid.store.base import MemoryStore, SessionNode, interrupted_prompt_id
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from tests.unit.test_acp_engine import SerializingStore, _driver, _glob_registry, _received
from tests.unit.test_delegated_scripted_cli import Scripted, _interruptible_turn, _tool_turn
from tests.unit.test_native_scripted_provider import Harness, Messages, Sleep, server, tool_reply


async def _event(loop, kind, *, since=0):
    async with asyncio.timeout(10), aclosing(loop.events(since_seq=since)) as events:
        async for event in events:
            if isinstance(event, kind):
                return event
    raise AssertionError(f"no {kind} event")


async def _stored_terminal(loop):
    """The last terminal event the store holds: what a restarted host replays."""
    events = await loop.store.events_since(loop.session_id, 0)
    return next((e for e in reversed(events) if isinstance(e, (Interrupted, ErrorEvent, TurnComplete))), None)


async def test_native_resume_after_interrupt_reruns_the_prompt(server):
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
        prompt, partial = await loop.store.load_path(loop.session_id)
        assert partial.message.role == "assistant"
        assert partial.message.text() and "partial".startswith(partial.message.text())
        # Stored unmarked: the stored Interrupted naming it makes it resumable.
        assert INTERRUPTED_META_KEY not in partial.message.provider_meta
        end = await _stored_terminal(loop)
        assert isinstance(end, Interrupted) and end.node_id == partial.node_id
        seq = loop.bus.last_seq(loop.session_id)
        await loop.submit(Resume())
        assert isinstance(await _event(loop, (ErrorEvent, TurnComplete), since=seq), TurnComplete)
        await asyncio.wait_for(loop.wait_turn(), 10)
        # The re-run sends the interrupted call's history, without the partial reply.
        assert len(server.requests) == 2
        assert server.requests[1].body["messages"] == server.requests[0].body["messages"]
        path = await loop.store.load_path(loop.session_id)
        assert [(n.node_id, n.message.role, n.message.text()) for n in path] == [
            (prompt.node_id, "user", "original prompt"), (path[-1].node_id, "assistant", "resumed"),
        ]
        assert path[-1].parent_id == partial.parent_id == prompt.node_id
    finally:
        await run.aclose()


@pytest.mark.parametrize("fail", ["reply", "usage", "interrupted"])
async def test_native_interrupted_reply_whose_settling_write_fails_is_not_resumable(server, fail):
    # Each write that settles an interrupted reply fails once in turn: the
    # stored turn ends on an error, and Resume agrees with it.
    server.script([Messages.start(), *Messages.text_block(0, "partial"), Sleep(30)],
                  Messages.text("resumed"))
    run = Harness(server)
    loop = run.loop
    pick = {"reply": lambda item: isinstance(item, SessionNode) and item.message.role == "assistant",
            "usage": lambda item: isinstance(item, Usage),
            "interrupted": lambda item: isinstance(item, Interrupted)}[fail]
    armed = [pick]
    append, append_event = run.store.append, run.store.append_event

    def check(item):
        if armed and armed[0](item):
            armed.clear()
            raise RuntimeError("database went away")

    async def failing_append(session_id, node):
        check(node)
        await append(session_id, node)

    async def failing_append_event(session_id, event):
        check(event)
        await append_event(session_id, event)

    run.store.append, run.store.append_event = failing_append, failing_append_event
    try:
        await loop._ensure_session()
        await loop.submit(UserTurn(content="original prompt"))
        await _event(loop, TextDelta)
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert not armed  # the picked write happened
        end = await _stored_terminal(loop)
        assert isinstance(end, ErrorEvent) and "database went away" in end.message
        stored = [n.message.role for n in await loop.store.load_path(loop.session_id)]
        await loop.submit(Resume())
        await asyncio.wait_for(loop.wait_turn(), 10)
        resumed = await _stored_terminal(loop)
        if fail == "reply":
            # Nothing stored after the prompt: unanswered, retried as on main.
            assert stored == ["user"]
            assert isinstance(resumed, TurnComplete) and len(server.requests) == 2
        else:
            assert stored == ["user", "assistant"]
            assert isinstance(resumed, ErrorEvent) and resumed.message.startswith("nothing to resume")
            assert len(server.requests) == 1
    finally:
        await run.aclose()


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


async def _seed_unanswered_prompt(loop, text):
    await loop.store.append(loop.session_id, SessionNode(
        node_id="unanswered", parent_id=None, message=Message(role="user", content=[Text(text)]),
    ))


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


@pytest.mark.parametrize("prompt", ["slowonce", "slowonce tool"])
async def test_acp_resume_after_interrupt_resends_the_prompt(tmp_path, prompt):
    driver = await _driver(tmp_path)
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content=prompt))
        await driver.until(TextDelta, timeout=10)
        await loop.submit(Interrupt())
        assert isinstance(await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10),
                          Interrupted)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert _received(tmp_path, "session/cancel")
        user, *kept = await loop.store.load_path(loop.session_id)
        assert kept[-1].message.text() == "working"
        # With a tool call: assistant call, tool result, assistant text.
        assert len(kept) == (3 if "tool" in prompt else 1)
        # Stored unmarked; the stored Interrupted names the head it settled on.
        assert not any(node.message.provider_meta for node in kept)
        end = await _stored_terminal(loop)
        assert isinstance(end, Interrupted) and end.node_id == kept[-1].node_id
        await loop.submit(Resume())
        assert isinstance(await driver.until((ErrorEvent, TurnComplete), timeout=10), TurnComplete)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert [r["params"]["prompt"][0]["text"] for r in _received(tmp_path, "session/prompt")] == [
            prompt, prompt,
        ]
        path = await loop.store.load_path(loop.session_id)
        assert [(n.message.role, n.message.text()) for n in path] == [
            ("user", prompt), ("assistant", "resumed"),
        ]
        assert path[0].node_id == user.node_id
    finally:
        await driver.close()


class _GatedStore(SerializingStore):
    """Holds the first node or event ``hold`` matches until ``release``, as
    the app's PostgreSQL store can yield mid-write."""

    def __init__(self, hold):
        super().__init__()
        self.hold, self.held, self.release = hold, asyncio.Event(), asyncio.Event()

    async def _gate(self, item):
        if not self.held.is_set() and self.hold(item):
            self.held.set()
            await self.release.wait()

    async def append(self, session_id, node):
        await self._gate(node)
        await super().append(session_id, node)

    async def append_event(self, session_id, event):
        await self._gate(event)
        await super().append_event(session_id, event)


def _assistant_node(item):
    return isinstance(item, SessionNode) and item.message.role == "assistant"


async def _interrupt_then_stop(tmp_path, hold):
    """Interrupt a streaming turn, then stop_agent while ``hold`` is held."""
    store = _GatedStore(hold)
    driver = await _driver(tmp_path, store=store)
    await driver.loop.submit(UserTurn(content="slowonce"))
    await driver.until(TextDelta, timeout=10)
    await driver.loop.submit(Interrupt())
    await asyncio.wait_for(store.held.wait(), 10)
    await driver.loop.stop_agent("credential withdrawn")
    store.release.set()
    end = await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10)
    await asyncio.wait_for(driver.loop.wait_turn(), 10)
    return driver, end


async def test_acp_stop_before_the_reply_is_stored_wins_over_interrupt(tmp_path):
    # The stop lands before the outcome is decided (the closing BlockStop is
    # written first): the credential error ends the turn, and Resume is
    # refused, as for any stopped turn.
    driver, end = await _interrupt_then_stop(tmp_path, lambda item: isinstance(item, BlockStop))
    loop = driver.loop
    try:
        assert isinstance(end, ErrorEvent) and end.message == "credential withdrawn"
        assert await _stored_terminal(loop) == end
        _user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "working"
        await loop.submit(Resume())
        end = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and end.message.startswith("Nothing to resume")
        assert len(_received(tmp_path, "session/prompt")) == 1
    finally:
        await driver.close()


async def test_acp_stop_while_the_interrupted_reply_is_stored_keeps_it_resumable(tmp_path):
    # The stop lands after the outcome (interrupted) was decided: the process
    # still ends, the turn ends on a stored Interrupted naming the reply, and
    # Resume re-sends the prompt to a fresh process.
    driver, end = await _interrupt_then_stop(tmp_path, _assistant_node)
    loop = driver.loop
    try:
        assert isinstance(end, Interrupted)
        user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "working"
        assert await _stored_terminal(loop) == end and end.node_id == reply.node_id
        await loop.submit(Resume())
        assert isinstance(await driver.until((ErrorEvent, TurnComplete), timeout=10), TurnComplete)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert [r["params"]["prompt"][0]["text"] for r in _received(tmp_path, "session/prompt")] == [
            "slowonce", "slowonce",
        ]
        path = await loop.store.load_path(loop.session_id)
        assert [(n.node_id, n.message.text()) for n in path] == [
            (user.node_id, "slowonce"), (path[-1].node_id, "resumed"),
        ]
    finally:
        await driver.close()


async def test_acp_interrupt_while_the_reply_is_stored_does_not_change_the_outcome(tmp_path):
    # The agent answers and exits; an Interrupt landing while that reply is
    # stored no longer turns the agent's failure into an Interrupted event
    # (which would make the finished reply resumable).
    store = _GatedStore(_assistant_node)
    driver = await _driver(tmp_path, store=store)
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content="die"))
        await asyncio.wait_for(store.held.wait(), 10)
        await loop.submit(Interrupt())
        store.release.set()
        end = await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent)
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert await _stored_terminal(loop) == end
        _user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "bye"
    finally:
        store.release.set()
        await driver.close()


def _reply_node(item):
    """A node the turn stores after its prompt: assistant, or tool results."""
    return isinstance(item, SessionNode) and (
        item.message.role == "assistant" or any(not isinstance(b, Text) for b in item.message.content))


REFUSED = ("The agent ended its turn without a reply after mcp__fluid_bundled__Glob "
           "was refused by the session's tool rules.")


async def _deny(name, data):
    return f"Tool '{name}' may not run"


@pytest.mark.parametrize("late", ["interrupt", "close"])
@pytest.mark.parametrize("prompt", ['agycancelled Glob {"pattern": "*"}', 'agyquiet Glob {"pattern": "*"}'])
async def test_acp_late_stop_keeps_a_silent_refusal_a_failure(tmp_path, late, prompt):
    # The agent ends with no reply after a refused call ("cancelled" or
    # "end_turn"). A Stop or a close landing while that turn is stored does
    # not hide the failure: it was decided before the write.
    store = _GatedStore(_reply_node)
    driver = await _driver(tmp_path, store=store, registry=_glob_registry([]),
                           ctx=ToolCtx(cwd=str(tmp_path)), tool_policy=_deny)
    closing = None
    try:
        await driver.loop.submit(UserTurn(content=prompt))
        await asyncio.wait_for(store.held.wait(), 10)
        if late == "interrupt":
            await driver.loop.submit(Interrupt())
        else:
            closing = asyncio.create_task(driver.loop.aclose())
            while not driver.loop._closed:
                await asyncio.sleep(0)
        store.release.set()
        end = await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and end.message == REFUSED
    finally:
        store.release.set()
        if closing is not None:
            await closing
        await driver.close()


class _FaultStore(SerializingStore):
    """Records each write made while ``armed`` and raises once, on the one
    ``fail`` names: its index among them, or "flush" for the final flush."""

    def __init__(self, fail):
        super().__init__()
        self.fail, self.armed, self.writes = fail, False, []

    def _write(self, kind):
        if self.armed:
            self.writes.append(kind)
            if self.fail == (kind if kind == "flush" else len(self.writes) - 1):
                self.armed = False
                raise RuntimeError("database went away")

    async def append(self, session_id, node):
        self._write(node.message.role)
        await super().append(session_id, node)

    async def append_event(self, session_id, event):
        self._write(type(event).__name__)
        await super().append_event(session_id, event)

    async def flush_events(self, session_id):
        self._write("flush")


# Every write that finalizes an interrupted ``slowonce tool`` turn, in order:
# the mapper's trailing BlockStop, the call, its result, the reply, Usage, the
# Interrupted event, then the final flush.
FINAL_WRITES = ["BlockStop", "assistant", "user", "assistant", "Usage", "Interrupted", "flush"]


@pytest.mark.parametrize(("fail", "kept"), [
    (None, 3), (0, 0), (1, 0), (2, 1), (3, 2), (4, 3), (5, 3), ("flush", 3),
], ids=["none", *(f"{i}-{w}" for i, w in enumerate(FINAL_WRITES[:-1])), "flush"])
async def test_acp_stored_outcome_of_an_interrupted_turn_decides_resume(tmp_path, fail, kept):
    # A write failing at any point of finalization leaves a stored error, and
    # Resume refuses the reply; only a stored Interrupted naming the head
    # makes it resumable. ``kept``: nodes stored after the prompt.
    store = _FaultStore(fail)
    driver = await _driver(tmp_path, store=store)
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content="slowonce tool"))
        await driver.until(TextDelta, timeout=10)
        store.armed = True
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 10)
        store.armed = False
        assert store.writes == (FINAL_WRITES if fail in (None, "flush") else FINAL_WRITES[:fail + 1])
        end = await _stored_terminal(loop)
        path = await loop.store.load_path(loop.session_id)
        assert len(path) == 1 + kept
        if fail in (None, "flush"):
            assert isinstance(end, Interrupted) and end.node_id == path[-1].node_id
        else:
            assert isinstance(end, ErrorEvent) and "database went away" in end.message
        await loop.submit(Resume())
        await asyncio.wait_for(loop.wait_turn(), 10)
        resumed = await _stored_terminal(loop)
        prompts = [r["params"]["prompt"] for r in _received(tmp_path, "session/prompt")]
        if isinstance(end, Interrupted) or not kept:
            # Resumable, or nothing stored after the prompt: an unanswered
            # prompt, re-sent as on main.
            assert isinstance(resumed, TurnComplete) and len(prompts) == 2 and prompts[1] == prompts[0]
        else:
            assert isinstance(resumed, ErrorEvent) and resumed.message.startswith("Nothing to resume")
            assert len(prompts) == 1
    finally:
        await driver.close()


async def test_interrupted_prompt_id_reads_only_the_last_stored_terminal_event():
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    seq = iter(range(1, 100))

    async def log(event_type, **fields):
        await store.append_event(sid, event_type(session_id=sid, seq=next(seq), **fields))

    assert await interrupted_prompt_id(store, sid, "reply") is None
    await log(TurnStarted, node_id="prompt", turn_id="t1", model="m", provider="p")
    await log(Interrupted, node_id="reply", turn_id="t1")
    assert await interrupted_prompt_id(store, sid, "reply") == "prompt"
    assert await interrupted_prompt_id(store, sid, "prompt") is None  # names another head
    await log(TurnStarted, node_id="next", turn_id="t2", model="m", provider="p")
    assert await interrupted_prompt_id(store, sid, "reply") is None  # a later turn never settled
    await log(ErrorEvent, turn_id="t2", message="failed")
    assert await interrupted_prompt_id(store, sid, "reply") is None  # a later terminal event


@pytest.mark.parametrize("case", ["reply", "tool-result", "empty-prompt"])
async def test_acp_resume_refuses_a_head_that_is_no_prompt_to_resend(tmp_path, case):
    nodes = {
        # A reply with no stored Interrupted naming it.
        "reply": [_node("p", None, "user", Text("prompt")), _node("a", "p", "assistant", Text("partial"))],
        # A tool-result node (role "user" too).
        "tool-result": [_node("p", None, "user", Text("prompt")),
                        _node("call", "p", "assistant", ToolUseBlock(id="t1", name="Read", input={})),
                        _node("result", "call", "user", ToolResultBlock(tool_use_id="t1", content=[Text("x")]))],
        # An unanswered "prompt" with nothing to send.
        "empty-prompt": [_node("p", None, "user", Text("  "))],
    }[case]
    driver = await _driver(tmp_path)
    loop = driver.loop
    try:
        for node in nodes:
            await loop.store.append(loop.session_id, node)
        await loop.submit(Resume())
        end = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and end.message.startswith("Nothing to resume")
        assert not _received(tmp_path, "session/prompt")
    finally:
        await driver.close()


def _node(node_id, parent_id, role, *content):
    return SessionNode(node_id=node_id, parent_id=parent_id, message=Message(role=role, content=list(content)))


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
