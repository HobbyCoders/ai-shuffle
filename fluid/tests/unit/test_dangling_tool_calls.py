"""Dangling tool calls: a call whose result never landed must not poison the
session.

A process death between the model call and the tool-result append (server
restart / crash mid-dispatch) leaves an assistant tool_use with no matching
tool result in the tree. Providers reject the whole request for it — the OpenAI
dialect with "No tool output found for function call call_XXX" — on EVERY later
turn, which makes the session permanently unusable. The loop repairs the
rendered history at request-build time instead of mutating the store.
"""

from typing import AsyncIterator

from omni.engine.native import (
    LOST_TOOL_RESULT_TEXT,
    NativeLoop,
    repair_dangling_tool_calls,
)
from omni.events import TurnComplete, UserTurn
from omni.messages import Message, Text, ToolResultBlock, ToolUseBlock
from omni.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from omni.store.base import MemoryStore, SessionNode, new_id


def _assistant_call(*call_ids: str, text: str | None = None) -> Message:
    blocks: list = [Text(text)] if text else []
    blocks += [ToolUseBlock(id=cid, name="Task", input={"prompt": "go"}) for cid in call_ids]
    return Message(role="assistant", content=blocks)


def _result(call_id: str, text: str = "done") -> Message:
    return Message(
        role="user",
        content=[ToolResultBlock(tool_use_id=call_id, content=[Text(text)])],
    )


def _repairs(history: list[Message]) -> list[tuple[int, str, bool]]:
    """(index, call_id, is_error) of every synthetic result in a repaired list."""
    out = []
    for i, message in enumerate(history):
        for block in message.content:
            if (
                isinstance(block, ToolResultBlock)
                and any(getattr(c, "text", "") == LOST_TOOL_RESULT_TEXT for c in block.content)
            ):
                out.append((i, block.tool_use_id, block.is_error))
    return out


# ─── The pure repair ─────────────────────────────────────────────────────────


def test_dangling_call_is_repaired_before_the_next_message() -> None:
    history = [
        Message(role="user", content=[Text("hi")]),
        _assistant_call("call_X"),
        Message(role="user", content=[Text("still there?")]),  # restart landed here
    ]
    repaired = repair_dangling_tool_calls(history)

    assert len(repaired) == 4
    # The synthetic output must sit between the call and the later message —
    # the OpenAI dialect requires a call's output to precede any later item.
    assert _repairs(repaired) == [(2, "call_X", True)]
    assert repaired[1] is history[1] and repaired[3] is history[2]


def test_two_dangling_calls_in_one_message_get_one_result_each() -> None:
    history = [
        _assistant_call("call_A", "call_B"),
        Message(role="user", content=[Text("next")]),
    ]
    repaired = repair_dangling_tool_calls(history)

    assert len(repaired) == 3
    assert _repairs(repaired) == [(1, "call_A", True), (1, "call_B", True)]


def test_dangling_calls_across_messages_are_each_repaired_in_place() -> None:
    history = [
        _assistant_call("call_A"),
        _assistant_call("call_B"),
    ]
    repaired = repair_dangling_tool_calls(history)

    assert [m.role for m in repaired] == ["assistant", "user", "assistant", "user"]
    assert _repairs(repaired) == [(1, "call_A", True), (3, "call_B", True)]


def test_fully_matched_history_is_untouched() -> None:
    history = [
        Message(role="user", content=[Text("hi")]),
        _assistant_call("call_X"),
        _result("call_X"),
        Message(role="assistant", content=[Text("all done")]),
    ]
    assert repair_dangling_tool_calls(history) is history  # identity, no copy


def test_mixed_text_and_call_message_keeps_its_blocks() -> None:
    history = [_assistant_call("call_X", text="I will look into it.")]
    repaired = repair_dangling_tool_calls(history)

    assert len(repaired) == 2
    assert repaired[0] is history[0]  # the assistant message is not rewritten
    assert repaired[0].text() == "I will look into it."
    assert _repairs(repaired) == [(1, "call_X", True)]


def test_repair_is_idempotent() -> None:
    once = repair_dangling_tool_calls([
        _assistant_call("call_X"),
        Message(role="user", content=[Text("next")]),
    ])
    assert repair_dangling_tool_calls(once) is once


def test_partially_answered_message_only_repairs_the_hole() -> None:
    history = [
        _assistant_call("call_A", "call_B"),
        _result("call_A"),
    ]
    repaired = repair_dangling_tool_calls(history)

    assert _repairs(repaired) == [(1, "call_B", True)]
    assert repaired[2] is history[1]  # the real result still follows


# ─── The loop actually applies it ────────────────────────────────────────────


class _ScriptedProvider:
    name = "openai_responses"
    caps = ProviderCaps(explicit_cache_breakpoints=False, max_cache_breakpoints=0)

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})


async def test_poisoned_session_still_builds_a_valid_request() -> None:
    store = MemoryStore()
    meta = await store.create_session()
    # A session whose last turn died mid tool dispatch: the tool_use node was
    # persisted, its result node never was.
    node = SessionNode(
        node_id=new_id("node"), parent_id=None,
        message=Message(role="user", content=[Text("do the thing")]),
    )
    await store.append(meta.session_id, node)
    orphan = SessionNode(
        node_id=new_id("node"), parent_id=node.node_id,
        message=_assistant_call("call_2I4nxHleTtYIdiBnk1vfnRy5"),
    )
    await store.append(meta.session_id, orphan)

    provider = _ScriptedProvider()
    loop = NativeLoop(
        provider=provider, store=store, session_id=meta.session_id,
        model="gpt-5", max_tokens=64,
    )
    await loop.submit(UserTurn(content="still there?"))
    await loop.wait_turn()

    assert len(provider.requests) == 1
    sent = provider.requests[0].messages
    assert _repairs(sent) == [(2, "call_2I4nxHleTtYIdiBnk1vfnRy5", True)]
    # ...and the new user turn still comes last.
    assert sent[-1].text() == "still there?"

    events = await store.events_since(meta.session_id, 0)
    assert any(isinstance(e, TurnComplete) for e in events)
    # The store keeps its true shape — repair is view-level only.
    nodes = await store.load_path(meta.session_id)
    assert not any(
        isinstance(b, ToolResultBlock) for n in nodes for b in n.message.content
    )
    await loop.aclose()
