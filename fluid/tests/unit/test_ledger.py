"""Ledger: the model-curated, never-lossy pinned state.

The file is the state (survives resume with no engine memory); the engine
recites it onto every user turn as a system-reminder, and digests strip old
copies while the newest rides the recent window."""

import asyncio
from collections.abc import AsyncIterator

from fluid.context.compaction import CompactionConfig, render_span_digest
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins.ledger import (
    handler as ledger_handler,
)
from fluid.tools.builtins.ledger import (
    read_ledger,
    render_ledger_reminder,
)
from fluid.tools.registry import ToolCtx


async def test_add_remove_show_roundtrip(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    out = await ledger_handler(
        {"action": "add", "section": "constraints", "text": "never touch prod"}, ctx)
    assert "never touch prod" in out.text
    out = await ledger_handler(
        {"action": "add", "section": "threads", "text": "flaky test in CI"}, ctx)
    assert "constraints" in out.text and "threads" in out.text
    out = await ledger_handler(
        {"action": "remove", "section": "threads", "text": "flaky"}, ctx)
    assert "flaky" not in out.text and "never touch prod" in out.text
    out = await ledger_handler({"action": "show"}, ctx)
    assert "never touch prod" in out.text


async def test_validation_errors(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    out = await ledger_handler({"action": "add", "text": "x"}, ctx)
    assert out.is_error                       # missing section
    out = await ledger_handler({"action": "remove", "section": "state", "text": "zz"}, ctx)
    assert out.is_error                       # nothing matched
    out = await ledger_handler({"action": "explode"}, ctx)
    assert out.is_error


async def test_read_ledger_survives_resume(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    await ledger_handler(
        {"action": "add", "section": "decisions", "text": "use JsonlStore"}, ctx)
    # A brand-new ctx (fresh process) reads the same state from disk.
    fresh = ToolCtx(cwd=str(tmp_path), session_id="s1")
    assert "use JsonlStore" in read_ledger(fresh)
    assert read_ledger(ToolCtx(cwd=str(tmp_path), session_id="other")) == ""


def test_digest_strips_ledger_reminder_copies() -> None:
    """Old recited copies age out with their turns; only the newest (on the
    fresh user turn) stays resident."""
    reminder = render_ledger_reminder("# Session ledger\n## constraints\n- keep it small")
    span = [SessionNode(node_id="node_u0", parent_id=None, message=Message(
        role="user", content=[Text("old question"), Text(reminder)],
    ))]
    text = render_span_digest(span, CompactionConfig()).content[0].text
    assert "old question" in text
    assert "keep it small" not in text and "system-reminder" not in text


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


async def test_ledger_reminder_rides_user_turns(tmp_path) -> None:
    provider = _Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        ctx=ToolCtx(cwd=str(tmp_path)),
    )
    await loop.submit(UserTurn(content="first"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    first = provider.requests[0].messages[-1]
    assert not any("Session ledger" in getattr(b, "text", "") for b in first.content)

    # The model curates the ledger mid-session (simulated directly).
    ctx = ToolCtx(cwd=str(tmp_path), session_id=loop.session_id)
    await ledger_handler(
        {"action": "add", "section": "constraints", "text": "python 3.12 only"}, ctx)

    await loop.submit(UserTurn(content="second"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    last = provider.requests[-1].messages[-1]
    joined = "".join(getattr(b, "text", "") for b in last.content)
    assert "Session ledger" in joined and "python 3.12 only" in joined
    # And it is STORED (survives resume, visible to audit).
    nodes = await loop.store.load_path(loop.session_id)
    stored = "".join(getattr(b, "text", "") for b in nodes[-2].message.content)
    assert "python 3.12 only" in stored
