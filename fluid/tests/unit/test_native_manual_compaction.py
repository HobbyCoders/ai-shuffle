"""A bare ``/compact`` compacts a native session on request (#1831).

Before this, a native (non-delegated) session had no way to compact when its
owner asked: the ladder ran only on its own triggers, and "/compact" reached
the model as text. ``NativeLoop`` now runs it as a turn with no model call:
the guarded ladder (``Compactor.compact_now``), its decisions persisted like
any batch, then a ``manual_compact`` summary event with the sizes. A request
that reclaims nothing reports ``compaction_failed`` instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.context.compaction import MANUAL_COMPACT_REASON, CompactionConfig
from fluid.context.trim import TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompaction, UserTurn
from fluid.events.model import DelegatedDiagnostic, TurnComplete
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.jsonl import JsonlStore


class _ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, text: str = "ok", gate: asyncio.Event | None = None) -> None:
        self.text = text
        self.gate = gate
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        for chunk in [
            StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": self.text}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ]:
            yield chunk


# A trigger far above anything these tests send, so only the request compacts.
_CONFIG = CompactionConfig(
    trigger_tokens=10_000_000, keep_recent_turns=1,
    trim=TrimConfig(trigger_tokens=10_000_000, keep_recent_tool_results=1, min_result_chars=200),
)


async def _turn(loop: NativeLoop, content: str) -> None:
    await loop.submit(UserTurn(content=content))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)


async def test_compact_now_reduces_the_view_without_a_model_call(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    provider = _ScriptedProvider("ok " + "pad " * 300)
    loop = NativeLoop(provider=provider, store=store, model="m", compaction=_CONFIG)
    for i in range(6):
        await _turn(loop, f"turn {i} " + "words " * 200)
    sid = loop.session_id
    calls_before = provider.calls
    nodes_before = await store.load_path(sid)
    view_before = loop._compactor.estimate_view_tokens(nodes_before)
    assert not [e for e in loop.bus.log(sid) if isinstance(e, ContextCompaction)]

    await _turn(loop, "/compact")

    assert provider.calls == calls_before  # no model call
    nodes_after = await store.load_path(sid)
    assert len(nodes_after) == len(nodes_before)  # no node was written
    events = loop.bus.log(sid)
    summary = [e for e in events if isinstance(e, ContextCompaction) and e.reason == MANUAL_COMPACT_REASON]
    assert len(summary) == 1
    assert summary[0].before_tokens > summary[0].after_tokens > 0
    assert summary[0].trimmed == [] and summary[0].spans == []
    decisions = [e for e in events if isinstance(e, ContextCompaction) and e.reason != MANUAL_COMPACT_REASON]
    assert decisions and all(e.decision_reason == "manual" for e in decisions)
    assert loop._compactor.estimate_view_tokens(nodes_after) < view_before
    terminal = [e for e in events if isinstance(e, TurnComplete)][-1]
    assert terminal.num_model_calls == 0
    assert not [e for e in events if isinstance(e, DelegatedDiagnostic)]

    # The decisions are durable: a fresh engine renders the same compacted view.
    view = loop._compactor.view(nodes_after)
    await loop.aclose()
    fresh = NativeLoop(provider=_ScriptedProvider(), store=JsonlStore(tmp_path), model="m",
                       session_id=sid, compaction=_CONFIG)
    await fresh._rehydrate_compaction(sid)
    assert fresh._compactor.view(nodes_after) == view
    await fresh.aclose()


async def test_compact_now_with_nothing_to_reclaim_reports_failure(tmp_path) -> None:
    loop = NativeLoop(provider=_ScriptedProvider(), store=JsonlStore(tmp_path), model="m",
                      compaction=_CONFIG)
    await _turn(loop, "hello")
    await _turn(loop, "/compact")
    events = loop.bus.log(loop.session_id)
    [failure] = [e for e in events if isinstance(e, DelegatedDiagnostic)]
    assert failure.kind == "compaction_failed" and "nothing could be compacted" in failure.message
    assert not [e for e in events if isinstance(e, ContextCompaction)]
    assert [e for e in events if isinstance(e, TurnComplete)][-1].stop_reason == "end_turn"
    await loop.aclose()


async def test_compact_now_is_refused_while_a_turn_runs(tmp_path) -> None:
    gate = asyncio.Event()
    loop = NativeLoop(provider=_ScriptedProvider(gate=gate), store=JsonlStore(tmp_path),
                      model="m", compaction=_CONFIG)
    await loop.submit(UserTurn(content="long work"))
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError, match="already running"):
        await loop.submit(UserTurn(content="/compact"))
    gate.set()
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.aclose()


async def test_compact_with_arguments_is_an_ordinary_message(tmp_path) -> None:
    provider = _ScriptedProvider()
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="m", compaction=_CONFIG)
    await _turn(loop, "/compact keep the API notes")
    assert provider.calls == 1
    assert not [e for e in loop.bus.log(loop.session_id)
                if isinstance(e, (ContextCompaction, DelegatedDiagnostic))]
    await loop.aclose()
