"""Note: the model's working plan, recited onto user turns.

The file is the state (survives resume with no engine memory); the engine
recites non-empty notes onto every new user turn as a system-reminder, and
digests strip old copies while the newest rides the recent window. An empty
notes file produces no reminder at all."""

import asyncio
from collections.abc import AsyncIterator

from fluid.context.compaction import CompactionConfig, render_span_digest
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins.note import (
    handler as note_handler,
)
from fluid.tools.builtins.note import (
    read_notes,
    render_notes_reminder,
)
from fluid.tools.registry import ToolCtx


async def test_add_list_clear_roundtrip(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    out = await note_handler({"action": "add", "text": "clone the repo"}, ctx)
    assert "clone the repo" in out.text
    out = await note_handler({"action": "add", "text": "run the tests"}, ctx)
    assert "run the tests" in out.text and "clone the repo" in out.text
    out = await note_handler({"action": "clear"}, ctx)
    assert "(empty)" in out.text


async def test_add_requires_text(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    out = await note_handler({"action": "add"}, ctx)
    assert out.is_error
    out = await note_handler({"action": "explode"}, ctx)
    assert out.is_error


async def test_read_notes_survives_resume(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    await note_handler({"action": "add", "text": "check CI before merge"}, ctx)
    fresh = ToolCtx(cwd=str(tmp_path), session_id="s1")
    assert "check CI before merge" in read_notes(fresh)
    assert read_notes(ToolCtx(cwd=str(tmp_path), session_id="other")) == ""


def test_digest_strips_notes_reminder_copies() -> None:
    reminder = render_notes_reminder("- step one\n- step two")
    span = [SessionNode(node_id="node_u0", parent_id=None, message=Message(
        role="user", content=[Text("old question"), Text(reminder)],
    ))]
    text = render_span_digest(span, CompactionConfig()).content[0].text
    assert "old question" in text
    assert "step one" not in text and "system-reminder" not in text


# ─── Engine integration: the reminder rides each user turn ───────────────────


class _Provider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        for chunk in (
            StreamChunk("message_start", {"usage": {}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": "ok"}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ):
            yield chunk


async def test_notes_reminder_rides_user_turns(tmp_path) -> None:
    provider = _Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        ctx=ToolCtx(cwd=str(tmp_path)),
    )
    await loop.submit(UserTurn(content="first"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    first = provider.requests[0].messages[-1]
    assert not any("Session notes" in getattr(b, "text", "") for b in first.content)

    # The model writes notes mid-session (simulated directly).
    ctx = ToolCtx(cwd=str(tmp_path), session_id=loop.session_id)
    await note_handler({"action": "add", "text": "fix the flaky test"}, ctx)

    await loop.submit(UserTurn(content="second"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    last = provider.requests[-1].messages[-1]
    joined = "".join(getattr(b, "text", "") for b in last.content)
    assert "Session notes" in joined and "fix the flaky test" in joined
    # And it is STORED (survives resume, visible to audit).
    nodes = await loop.store.load_path(loop.session_id)
    stored = "".join(getattr(b, "text", "") for b in nodes[-2].message.content)
    assert "fix the flaky test" in stored
