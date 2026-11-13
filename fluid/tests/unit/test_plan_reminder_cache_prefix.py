"""The session-plan reminder never rewrites bytes already sent to the provider.

Measured on native gpt-6.1-sol sessions (2026-11): while a session had an
active Plan, the reminder rode the LAST user message of every model call
without being stored. The next call moved it to the new last message, so the
previous request's tail changed and the cached prefix stopped growing
(cache reads frozen at 28.8k while the prompt grew 42k to 162k, every call a
``prefix_changed`` miss). The reminder is now written into the transcript when
its text changes, so each request is a strict prefix of the next.
"""

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from itertools import pairwise

from fluid.context.compaction import CompactionConfig, Compactor, is_turn_start
from fluid.context.trim import TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.messages.content import HARNESS_NOTE_META_KEY, is_harness_note
from fluid.permissions.engine import PermissionEngine
from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.local.serialize import build_request_body as local_body
from fluid.providers.local.text_tools import serialize_history
from fluid.providers.openai_responses.serialize import (
    build_request_body as responses_body,
)
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins import plan, register_builtins
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry


class ScriptedProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(len(self.requests), len(self.scripts) - 1)]
        # Snapshot: the engine must not be able to rewrite what was "sent".
        self.requests.append(copy.deepcopy(request))
        for chunk in script:
            yield chunk


def _tool_call(tuid: str, name: str, args: dict) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _plan_update(revision: int, status: str) -> dict:
    args = {"action": "update", "request_id": f"r{revision}", "expected_revision": revision,
            "tasks": [{"id": "a", "title": "Implement the fix", "status": status}]}
    if revision == 0:
        args.update(goal="Fix the cache bug", done_when="Prefix test passes")
    return args


class _PlanBackend:
    """In-memory host backend (the shape the app injects as ``session_plan``)."""

    def __init__(self) -> None:
        self.document = plan.empty_plan()
        self.receipts: list = []

    def __call__(self, args: dict) -> dict:
        if args["action"] == "get":
            return {"plan": self.document, "status_source": "model_reported"}
        self.document, self.receipts, result, _ = plan.apply_update(self.document, self.receipts, args)
        return result


def _weather_server():
    @tool("get_weather", "Get weather", {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    return create_inproc_server("wx", [get_weather])


async def _run(scripts: list[list[StreamChunk]], *, backend: _PlanBackend | None = None,
               prompts: tuple[str, ...] = ("go",)):
    provider = ScriptedProvider(scripts)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    register_builtins(registry)
    registry.register_inproc(_weather_server())
    loop = NativeLoop(provider=provider, store=store, model="gpt-6.1-sol", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"))
    backend = backend or _PlanBackend()
    loop.ctx.extra["session_plan"] = backend
    for prompt in prompts:
        await loop.submit(UserTurn(content=prompt))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return provider, store, sid, backend


def _responses_input(request: ProviderRequest) -> list:
    return responses_body(request)["input"]


def _anthropic_messages(request: ProviderRequest) -> list:
    messages = anthropic_body(request)["messages"]

    def strip(value):
        # Breakpoints sit on the newest block by design; they are not content.
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(messages)


def _assert_append_only(requests: list[ProviderRequest], render) -> None:
    for index, (before, after) in enumerate(pairwise(requests), start=1):
        sent, nxt = render(before), render(after)
        assert len(nxt) > len(sent), f"call {index + 1} did not grow"
        assert nxt[:len(sent)] == sent, f"call {index} input is not a prefix of call {index + 1}"


async def test_requests_stay_append_only_across_a_tool_loop_with_an_active_plan() -> None:
    provider, _, _, backend = await _run([
        _tool_call("t1", "Plan", _plan_update(0, "in_progress")),
        _tool_call("t2", "mcp__wx__get_weather", {"city": "Oslo"}),
        _tool_call("t3", "mcp__wx__get_weather", {"city": "Rome"}),
        _tool_call("t4", "mcp__wx__get_weather", {"city": "Lima"}),
        _text("done"),
    ])
    assert backend.document["revision"] == 1
    assert len(provider.requests) == 5
    _assert_append_only(provider.requests, _responses_input)
    _assert_append_only(provider.requests, _anthropic_messages)

    note = plan.render_reminder(backend.document)
    assert note is not None
    # The update reached the very next call, and only once in each request.
    for request in provider.requests[1:]:
        assert sum(m.text().count(note) for m in request.messages) == 1


async def test_a_plan_change_reaches_the_next_call_without_touching_the_sent_prefix() -> None:
    provider, store, sid, _ = await _run([
        _tool_call("t1", "Plan", _plan_update(0, "in_progress")),
        _tool_call("t2", "mcp__wx__get_weather", {"city": "Oslo"}),
        _tool_call("t3", "Plan", _plan_update(1, "completed")),
        _tool_call("t4", "mcp__wx__get_weather", {"city": "Rome"}),
        _text("done"),
    ])
    _assert_append_only(provider.requests, _responses_input)
    _assert_append_only(provider.requests, _anthropic_messages)
    # Each new revision is in the very next call.
    assert "revision 1;" in provider.requests[1].messages[-1].text()
    assert "revision 2;" in provider.requests[3].messages[-1].text()
    assert "revision 2;" not in "".join(m.text() for m in provider.requests[2].messages)
    # Each version is stored exactly once: the transcript is the reminder's home.
    stored = "".join(n.message.text() for n in await store.load_path(sid))
    assert stored.count("# Session plan (revision 1;") == 1
    assert stored.count("# Session plan (revision 2;") == 1


async def test_the_plan_is_restored_once_after_a_fold_removes_it(monkeypatch) -> None:
    provider = ScriptedProvider([
        _tool_call("t1", "Plan", _plan_update(0, "in_progress")),
        _tool_call("t2", "mcp__wx__get_weather", {"city": "Oslo"}),
        _tool_call("t3", "mcp__wx__get_weather", {"city": "Rome"}),
        _tool_call("t4", "mcp__wx__get_weather", {"city": "Lima"}),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    register_builtins(registry)
    registry.register_inproc(_weather_server())
    loop = NativeLoop(provider=provider, store=store, model="gpt-6.1-sol", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"))
    backend = _PlanBackend()
    loop.ctx.extra["session_plan"] = backend
    real_apply = loop._compactor.apply
    folded: set[str] = set()
    applies = 0

    def apply(nodes, **kwargs):
        # Before the third call, fold everything except the newest call and
        # its result, including the node that held the plan: a summary
        # replaces them for the rest of the turn, as a real fold would.
        nonlocal applies
        applies += 1
        rendered, promotions = real_apply(nodes, **kwargs)
        if applies == 3:
            folded.update(n.node_id for n in nodes[:-2])
        if not folded:
            return rendered, promotions
        kept = [n.message for n in nodes if n.node_id not in folded]
        return [Message(role="user", content=[Text("[summary of earlier work]")]), *kept], promotions

    monkeypatch.setattr(loop._compactor, "apply", apply)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    note = plan.render_reminder(backend.document)
    assert note is not None
    assert len(provider.requests) == 5
    # The fold removed the stored copy; the next call carries the plan again.
    assert note in provider.requests[2].messages[-1].text()
    for request in provider.requests[1:]:
        assert sum(m.text().count(note) for m in request.messages) == 1
    # After the fold, growth is append-only again.
    _assert_append_only(provider.requests[2:], _responses_input)
    stored = "".join(n.message.text() for n in await store.load_path(sid))
    assert stored.count(note) == 2


async def test_an_unchanged_plan_is_not_restated_by_the_next_turn_calls() -> None:
    backend = _PlanBackend()
    backend(_plan_update(0, "in_progress"))
    provider, store, sid, _ = await _run([
        _tool_call("t1", "mcp__wx__get_weather", {"city": "Oslo"}),
        _tool_call("t2", "mcp__wx__get_weather", {"city": "Rome"}),
        _text("done"),
    ], backend=backend)
    _assert_append_only(provider.requests, _responses_input)
    note = plan.render_reminder(backend.document)
    assert note is not None
    # The turn's own prompt carries it; the tool loop adds no copies.
    assert note in provider.requests[0].messages[-1].text()
    stored = "".join(n.message.text() for n in await store.load_path(sid))
    assert stored.count(note) == 1


async def test_the_stored_reminder_is_marked_and_never_opens_a_turn() -> None:
    _, store, sid, _ = await _run([
        _tool_call("t1", "Plan", _plan_update(0, "in_progress")),
        _text("done"),
    ])
    path = await store.load_path(sid)
    notes = [n for n in path if is_harness_note(n.message)]
    assert len(notes) == 1 and "# Session plan (revision 1;" in notes[0].message.text()
    assert not is_turn_start(notes[0])
    # The marker is durable: a resumed session reads it back from storage.
    assert is_harness_note(Message.from_dict(notes[0].message.to_dict()))
    assert [n for n in path if is_turn_start(n)] == path[:1]


def _sn(nid: str, role: str, blocks: list, meta: dict | None = None) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks, provider_meta=meta or {}))


def test_a_mid_turn_plan_update_cannot_get_the_real_request_digested() -> None:
    # Review finding S1: an unmarked reminder node counted as a new turn, so
    # the open request became an "older turn" the compactor could digest
    # whole. A long request lost its late constraint mid-turn.
    request = "Refactor the module. " + "context " * 1500 + "LATE-CONSTRAINT: never touch prod.yml"
    nodes = [_sn("u0", "user", [Text("first turn")]), _sn("f0", "assistant", [Text("ok")]),
             _sn("u1", "user", [Text(request)])]
    for i in range(6):
        nodes += [
            _sn(f"a{i}", "assistant", [ToolUseBlock(id=f"c{i}", name="Read", input={"p": "x" * 3000})]),
            _sn(f"r{i}", "user", [ToolResultBlock(tool_use_id=f"c{i}", content=[Text("r" * 3000)],
                                                  delivery_metadata_version=1)]),
        ]
        if i == 2:
            nodes.append(_sn("plan", "user", [Text("# Session plan (revision 2; ...)")],
                             {HARNESS_NOTE_META_KEY: True}))
    compactor = Compactor(config=CompactionConfig(
        trigger_tokens=2_000, target_ratio=0.3, keep_recent_turns=1, trim=TrimConfig(enabled=False),
    ))
    for end in range(len(nodes) - 4, len(nodes) + 1):
        rendered, _ = compactor.apply(nodes[:end], turn_boundary=False, context_window=8_000)
    assert any(m.text() == request for m in rendered)
    assert not any(start == "u1" for start, _ in compactor._digested)


def _text_tool_history() -> list[Message]:
    return [
        Message(role="user", content=[Text("go")]),
        Message(role="assistant", content=[ToolUseBlock(id="t1", name="Plan", input={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content=[Text("ok")])]),
        Message(role="user", content=[Text("# Session plan (revision 1)")],
                provider_meta={HARNESS_NOTE_META_KEY: True}),
    ]


def test_text_tools_fold_the_reminder_into_the_tool_result_turn() -> None:
    # Review finding S2: strict chat templates (Mistral-7B-Instruct) refuse
    # two user turns in a row.
    history = _text_tool_history()
    sent = serialize_history(history)
    assert [m.role for m in sent] == ["user", "assistant", "user"]
    assert "<tool_result" in sent[-1].content[0].text
    assert sent[-1].content[1].text == "# Session plan (revision 1)"

    later = [*history,
             Message(role="assistant", content=[ToolUseBlock(id="t2", name="Plan", input={})]),
             Message(role="user", content=[ToolResultBlock(tool_use_id="t2", content=[Text("ok")])]),
             Message(role="user", content=[Text("# Session plan (revision 2)")])]
    nxt = serialize_history(later)
    assert nxt[:len(sent)] == sent  # still a strict prefix of the next request


def test_local_chat_completions_never_sends_two_user_turns_in_a_row() -> None:
    def roles(messages: list[Message]) -> list[str]:
        body = local_body(ProviderRequest(model="m", system="", messages=messages))
        return [m["role"] for m in body["messages"] if m["role"] != "system"]

    history = _text_tool_history()
    history[2] = Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content=[Text("ok")]),
                                               Text("a steer")])
    assert roles(history) == ["user", "assistant", "tool", "user"]
