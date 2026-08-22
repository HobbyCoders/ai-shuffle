"""Reasoning-channel answer fallback (GLM-4.5 quirk).

Some model families (observed: GLM-4.5 via OpenRouter) end a turn with the
entire answer in thinking_deltas and an empty text channel. The engine
promotes the thinking content to a synthetic text block on a completed,
non-tool call — streamed as a TextDelta so live text-channel clients render
it, and present in the assembled Message so ``text()`` is non-empty.
"""

from collections.abc import AsyncIterator

from fluid.engine.native import NativeLoop
from fluid.events import TextDelta, TurnComplete, UserTurn
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore


class OneScriptProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        for chunk in self._chunks:
            yield chunk


def _thinking_only_turn() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking"}),
        StreamChunk("thinking_delta", {"index": 0, "text": "The answer "}),
        StreamChunk("thinking_delta", {"index": 0, "text": "is 42."}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


async def test_thinking_only_answer_promoted_to_text() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=OneScriptProvider(_thinking_only_turn()),
        store=store, model="z-ai/glm-4.5", session_id=sid,
    )
    await loop.submit(UserTurn(content="what is the answer?"))
    await loop.wait_turn()

    events = loop.bus.log(sid)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "The answer is 42."
    assert any(isinstance(e, TurnComplete) for e in events)

    # The assembled assistant message carries a real text channel too.
    path = await store.load_path(sid)
    assert path[-1].message.role == "assistant"
    assert path[-1].message.text() == "The answer is 42."


async def test_normal_text_turn_gets_no_synthetic_delta() -> None:
    chunks = [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking"}),
        StreamChunk("thinking_delta", {"index": 0, "text": "hmm"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("block_start", {"index": 1, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 1, "text": "Answer."}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=OneScriptProvider(chunks), store=store,
        model="claude-sonnet-5", session_id=sid,
    )
    await loop.submit(UserTurn(content="q"))
    await loop.wait_turn()

    text = "".join(e.text for e in loop.bus.log(sid) if isinstance(e, TextDelta))
    assert text == "Answer."  # thinking NOT duplicated into the text channel
