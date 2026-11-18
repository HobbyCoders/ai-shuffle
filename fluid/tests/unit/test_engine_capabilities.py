"""Engine contracts documented in docs/engines.md, using the wire-level fakes."""
import asyncio
from contextlib import aclosing

import pytest

from fluid.events.model import (
    BlockStop, ErrorEvent, Usage, Interrupted, PermissionRequest, SessionParked, SessionWoken, SubagentEvent, TextDelta,
    ToolResult, ToolUse, TurnComplete,
)
from fluid.events.ops import (
    BackgroundTaskFinished, Fork, Interrupt, RespondPermission, Resume, Rewind, UserTurn,
)
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.rules import Rule
from fluid.store.base import SessionNode
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
        assert partial.message.provider_meta.get("interrupted")
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
        # Only the turn's last node is marked, naming the prompt to re-send.
        assert kept[-1].message.provider_meta == {"interrupted": "interrupt", "resume_node_id": user.node_id}
        assert not any(node.message.provider_meta for node in kept[:-1])
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
    # written first): the credential error ends the turn, nothing is marked,
    # and Resume is refused, as for any stopped turn.
    driver, end = await _interrupt_then_stop(tmp_path, lambda item: isinstance(item, BlockStop))
    loop = driver.loop
    try:
        assert isinstance(end, ErrorEvent) and end.message == "credential withdrawn"
        _user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "working"
        assert not (reply.message.provider_meta or {}).get("interrupted")
        await loop.submit(Resume())
        end = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and end.message.startswith("Nothing to resume")
        assert len(_received(tmp_path, "session/prompt")) == 1
    finally:
        await driver.close()


async def test_acp_stop_while_the_interrupted_reply_is_stored_keeps_it_resumable(tmp_path):
    # The stop lands after the outcome (interrupted) was decided and stored:
    # the process still ends, the turn ends Interrupted as its stored marker
    # says, and Resume re-sends the prompt to a fresh process.
    driver, end = await _interrupt_then_stop(tmp_path, _assistant_node)
    loop = driver.loop
    try:
        assert isinstance(end, Interrupted)
        user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "working" and reply.message.provider_meta.get("interrupted")
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
    # over an unmarked (so not resumable) head.
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
        _user, reply = await loop.store.load_path(loop.session_id)
        assert reply.message.text() == "bye"
        assert not (reply.message.provider_meta or {}).get("interrupted")
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


async def test_acp_failed_write_of_an_interrupted_turn_is_not_resumable(tmp_path):
    # The interrupted turn's second node fails to store: the turn ends on an
    # error and the head it left (unmarked) is not offered to Resume.
    class FailingStore(SerializingStore):
        appended = 0

        async def append(self, session_id, node):
            if _reply_node(node):
                self.appended += 1
                if self.appended == 2:
                    raise RuntimeError("database went away")
            await super().append(session_id, node)

    driver = await _driver(tmp_path, store=FailingStore())
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content="slowonce tool"))
        await driver.until(TextDelta, timeout=10)
        await loop.submit(Interrupt())
        end = await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and "database went away" in end.message
        await asyncio.wait_for(loop.wait_turn(), 10)
        _user, head = await loop.store.load_path(loop.session_id)
        assert head.message.role == "assistant" and not head.message.provider_meta
        await loop.submit(Resume())
        end = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        assert isinstance(end, ErrorEvent) and end.message.startswith("Nothing to resume")
        assert len(_received(tmp_path, "session/prompt")) == 1
    finally:
        await driver.close()


class _FaultStore(SerializingStore):
    """Raises once, on the write ``fail(index, item)`` picks. ``index``
    counts the turn's writes from its first reply node (0); None before."""

    def __init__(self, fail):
        super().__init__()
        self.fail, self.fired, self.failed, self.index = fail, False, None, None
        self.marked_at = None  # index of the marked (resumable) node

    def _check(self, item):
        if self.index is not None:
            self.index += 1
        elif _reply_node(item):
            self.index = 0
        if isinstance(item, SessionNode) and item.message.provider_meta.get("resume_node_id"):
            self.marked_at = self.index
        if not self.fired and self.fail(self.index, item):
            self.fired, self.failed = True, self.index
            raise RuntimeError("database went away")

    async def append(self, session_id, node):
        if _reply_node(node):
            self._check(node)
        await super().append(session_id, node)

    async def append_event(self, session_id, event):
        self._check(event)
        await super().append_event(session_id, event)


async def _interrupt_and_resume(tmp_path, store):
    """Interrupt ``slowonce tool``, then Resume (no fault armed): (terminal
    event, roles stored after the prompt, Resume's outcome, prompts sent)."""
    driver = await _driver(tmp_path, store=store)
    loop = driver.loop
    try:
        await loop.submit(UserTurn(content="slowonce tool"))
        await driver.until(TextDelta, timeout=10)
        await loop.submit(Interrupt())
        end = await driver.until((Interrupted, ErrorEvent, TurnComplete), timeout=10)
        await asyncio.wait_for(loop.wait_turn(), 10)
        stored = [n.message.role for n in await loop.store.load_path(loop.session_id)][1:]
        store.fail = lambda *_: False
        await loop.submit(Resume())
        resumed = await driver.until((ErrorEvent, TurnComplete), timeout=10)
        await asyncio.wait_for(loop.wait_turn(), 10)
        return end, stored, resumed, [r["params"]["prompt"] for r in _received(tmp_path, "session/prompt")]
    finally:
        await driver.close()


@pytest.mark.parametrize("fail", [
    pytest.param(lambda i, item: isinstance(item, Usage), id="usage"),
    # Each write from the first reply node on: call, tool result, the marked
    # reply, then the Interrupted event.
    *(pytest.param(lambda i, item, k=k: i == k, id=f"write-{k}") for k in range(4)),
])
async def test_acp_interrupted_turn_outcome_and_resume_agree_when_a_write_fails(tmp_path, fail):
    # The marked node is the turn's last write that can fail: whichever write
    # fails, the turn either ended Interrupted and is resumable, or ended on
    # an error that Resume agrees with; never an error behind a resumable head.
    store = _FaultStore(fail)
    end, stored, resumed, prompts = await _interrupt_and_resume(tmp_path, store)
    assert store.fired  # the picked write happened
    if isinstance(end, Interrupted):
        assert isinstance(resumed, TurnComplete)
        assert len(prompts) == 2 and prompts[1] == prompts[0]
    elif stored:
        # An error behind a stored (unmarked) reply: Resume refuses it.
        assert isinstance(end, ErrorEvent) and "database went away" in end.message
        assert isinstance(resumed, ErrorEvent) and resumed.message.startswith("Nothing to resume")
        assert len(prompts) == 1
    else:
        # Nothing of the turn was stored: the prompt is unanswered and
        # Resume re-sends it, as after any error before a reply (as on main).
        assert isinstance(end, ErrorEvent) and "database went away" in end.message
        assert isinstance(resumed, TurnComplete) and len(prompts) == 2
    # A write after the marked node cannot fail the turn.
    if store.marked_at is not None and store.failed is not None and store.failed > store.marked_at:
        assert isinstance(end, Interrupted)


async def test_acp_failed_final_write_after_a_tool_result_is_not_resumable(tmp_path):
    # The interrupted turn's last node (the reply after a tool result) fails
    # to store: the head is the tool-result node (role "user"), which is no
    # prompt; Resume refuses instead of sending an empty one.
    seen = []

    def third_reply_node(_i, item):
        if isinstance(item, SessionNode):
            seen.append(item)
        return len(seen) == 3

    end, _stored, resumed, prompts = await _interrupt_and_resume(tmp_path, _FaultStore(third_reply_node))
    assert isinstance(end, ErrorEvent)
    assert isinstance(seen[1].message.content[0], ToolResultBlock)
    assert isinstance(resumed, ErrorEvent) and resumed.message.startswith("Nothing to resume")
    assert len(prompts) == 1


def _node(node_id, parent_id, role, *content, **meta):
    return SessionNode(node_id=node_id, parent_id=parent_id,
                       message=Message(role=role, content=list(content), provider_meta=meta))


@pytest.mark.parametrize("case", ["unmarked", "not-an-ancestor", "tool-result", "empty-prompt"])
async def test_acp_resume_refuses_a_head_that_names_no_valid_prompt(tmp_path, case):
    marker = {"interrupted": "interrupt"}
    nodes = {
        # The pointer without the interrupted marker.
        "unmarked": [_node("p", None, "user", Text("prompt")),
                     _node("a", "p", "assistant", Text("partial"), resume_node_id="p")],
        # A marked head naming a prompt on another branch.
        "not-an-ancestor": [_node("other", None, "user", Text("other prompt")),
                            _node("p", None, "user", Text("prompt")),
                            _node("a", "p", "assistant", Text("partial"), resume_node_id="other", **marker)],
        # A marked head naming its tool-result node.
        "tool-result": [_node("p", None, "user", Text("prompt")),
                        _node("call", "p", "assistant", ToolUseBlock(id="t1", name="Read", input={})),
                        _node("result", "call", "user", ToolResultBlock(tool_use_id="t1", content=[Text("x")])),
                        _node("a", "result", "assistant", Text("partial"), resume_node_id="result", **marker)],
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
