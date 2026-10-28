"""Fitting the window is a postcondition of native compaction.

The ladder protects recent work and error results, so a prompt whose newest
part is itself too large (one huge error, a flood of fresh results) used to
be detected as over the window, reclaim nothing, and be sent anyway. The
native loop now checks the assembled request before every provider call:
it cuts what it must to an excerpt plus an exact HistoryExpand ref, and
ends the turn with an explicit error when even that cannot fit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fluid.context.compaction import CompactionConfig
from fluid.context.trim import estimate_tokens_of_messages
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompaction, UserTurn
from fluid.events.model import ErrorEvent
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore

WINDOW = 100_000
RESERVE = 4_000


class _Provider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, calls: int) -> None:
        self.calls_left = calls
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
        if self.calls_left:
            self.calls_left -= 1
            yield StreamChunk("block_start", {
                "index": 0, "block_type": "tool_use",
                "tool_use_id": f"t{len(self.requests)}", "tool_name": "mcp__b__fail"})
            yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"})
        else:
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


def _loop(tmp_path, provider=None, **kw) -> NativeLoop:
    return NativeLoop(
        provider=provider or _Provider(0), store=JsonlStore(tmp_path), model="m",
        context_window=WINDOW, max_tokens=RESERVE, compaction=CompactionConfig(), **kw,
    )


def _node(nid, role, blocks):
    return SessionNode(node_id=nid, parent_id=None, message=Message(role=role, content=blocks))


def _with_newest_error(chars: int) -> list[SessionNode]:
    return [
        _node("node_u1", "user", [Text("run the build")]),
        _node("node_a1", "assistant", [ToolUseBlock(id="c1", name="Bash", input={"command": "make"})]),
        _node("node_r1", "user", [ToolResultBlock(
            tool_use_id="c1", content=[Text("ERROR-HEAD " + "e" * chars)], is_error=True)]),
    ]


async def test_an_over_window_request_is_compacted_before_it_is_sent(tmp_path) -> None:
    """The overflow escalation runs now, not after a provider rejection."""
    loop = _loop(tmp_path)
    try:
        nodes = _with_newest_error(600_000)
        promotions, refusal = loop._admission_promotions(
            nodes, overhead_tokens=0, measured=None, measured_current=False,
        )
        assert refusal is None and promotions
        assert {p.decision_reason for p in promotions} == {"admission"}
        view = loop._compactor.view(nodes)
        assert estimate_tokens_of_messages(view) <= WINDOW - RESERVE
        # The digest names the nodes it stands for (this loop has no recall
        # tools; with them the digest cites HistoryExpand on the same ids).
        assert "node_u1..node_r1" in str(view)
    finally:
        await loop.aclose()


async def test_a_newest_error_the_ladder_cannot_fold_is_cut_to_an_excerpt(tmp_path, monkeypatch) -> None:
    """Even when nothing may be digested, an error result too large for the
    window becomes its opening plus an exact ref, persisted like any trim."""
    loop = _loop(tmp_path)
    monkeypatch.setattr(loop._compactor, "escalate", lambda *a, **k: [])
    try:
        nodes = _with_newest_error(600_000)
        promotions, refusal = loop._admission_promotions(
            nodes, overhead_tokens=0, measured=None, measured_current=False,
        )
        assert refusal is None
        [cut] = promotions
        assert (cut.reason, cut.tier, cut.trimmed) == ("admission", 1, (("node_r1", "c1"),))
        view = loop._compactor.view(nodes)
        assert estimate_tokens_of_messages(view) <= WINDOW - RESERVE
        text = "".join(c.text for b in view[-1].content for c in b.content)
        # The opening survives; the rest is behind the trim placeholder
        # (an exact HistoryExpand ref when the session has recall tools).
        assert "ERROR-HEAD" in text and "600011 chars removed" in text
    finally:
        await loop.aclose()


async def test_a_fitting_request_is_left_alone(tmp_path) -> None:
    loop = _loop(tmp_path)
    try:
        assert loop._admission_promotions(
            _with_newest_error(10_000), overhead_tokens=0, measured=None, measured_current=False,
        ) == ([], None)
    finally:
        await loop.aclose()


async def test_an_irreducible_request_is_refused_not_sent(tmp_path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    monkeypatch.setattr(loop._compactor, "escalate", lambda *a, **k: [])
    try:
        nodes = [_node("node_a1", "assistant", [ToolUseBlock(
            id="c1", name="Write", input={"content": "w" * 600_000})])]
        _, refusal = loop._admission_promotions(
            nodes, overhead_tokens=0, measured=None, measured_current=False,
        )
        assert refusal and "cannot fit" in refusal
    finally:
        await loop.aclose()


async def test_a_current_measurement_counts_toward_admission(tmp_path) -> None:
    """chars/4 says it fits; the provider measured the same view at the
    window. A current measurement wins."""
    loop = _loop(tmp_path)
    try:
        nodes = _with_newest_error(200_000)
        estimate = estimate_tokens_of_messages(loop._compactor.view(nodes))
        assert estimate < WINDOW - RESERVE
        loop._compactor.last_decision = {"before_tokens": estimate}
        promotions, refusal = loop._admission_promotions(
            nodes, overhead_tokens=0, measured=estimate * 2, measured_current=True,
        )
        assert refusal is None and promotions
        # A stale measurement of the same size does not.
        stale = _loop(tmp_path / "stale")
        stale._compactor.last_decision = {"before_tokens": estimate}
        assert stale._admission_promotions(
            nodes, overhead_tokens=0, measured=estimate * 2, measured_current=False,
        ) == ([], None)
        await stale.aclose()
    finally:
        await loop.aclose()


async def test_engine_never_sends_an_over_window_request(tmp_path) -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    @tool("fail", "Fail", {"type": "object", "properties": {}})
    async def fail(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "TRACE " + "x" * 600_000}], "isError": True}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("b", [fail]))
    provider = _Provider(1)
    loop = _loop(tmp_path, provider=provider, registry=registry,
                 permissions=PermissionEngine("never_ask"))
    try:
        await loop.submit(UserTurn(content="Run it."))
        await asyncio.wait_for(loop.wait_turn(), timeout=20)
        events = loop.bus.log(loop.session_id)
        assert len(provider.requests) == 2
        for request in provider.requests:
            assert estimate_tokens_of_messages(request.messages) <= WINDOW - RESERVE
        rungs = [e for e in events if isinstance(e, ContextCompaction)]
        assert any(e.decision_reason == "admission" for e in rungs)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
    finally:
        await loop.aclose()
