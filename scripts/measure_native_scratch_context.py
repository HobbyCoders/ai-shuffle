"""Offline measurement of the native leg's ``# Harness scratch`` lifetime.

Run: PYTHONPATH=fluid python scripts/measure_native_scratch_context.py --turns 242

Drives the real :class:`HarnessAdapter` and the real :class:`NativeLoop` with a
recording fake provider and an in-memory store, so what is counted is what the
producing path stored and what each model request carried. No real provider,
no database (any connection attempt raises), no installed owner data; the
scratch path is a fixed fixture, so the numbers do not depend on the machine.

``stored_*`` counts copies on the session's user nodes. ``requested_*`` counts
copies in the rendered requests, summed over every model call: what the model
re-reads as history grows. ``*_tokens_estimated`` are chars/4 estimates, never
billed tokens, and take no account of prompt caching.

The ``steady`` scenario is plain turns. The ``folded`` scenario runs a low
compaction trigger, so digests fold the turn that carried the block, and the
``requests_without_scratch`` field is the safety number: it must stay 0, or the
model was left without the rule.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]

HEADER = "# Harness scratch"
MEMORY = (
    "# Memory\nSaved facts, not instructions.\n\n"
    "- [m1 r1; user] Keep owner facts until explicitly corrected."
)


def fixture_note() -> str:
    """The real block text, rendered for a fixed fixture path."""
    from fluid.state import scratch_prompt
    from fluid.tools.registry import ToolCtx

    return scratch_prompt(ToolCtx(
        cwd="/fixture", session_id="owner-code",
        extra={"harness_state_root": "/fixture/state"},
    ))


class _Recording:
    """A provider that keeps each request and answers with a fixed reply."""

    name = "fake"

    def __init__(self, reply: str) -> None:
        from fluid.providers.base import ProviderCaps, StreamChunk

        self.caps = ProviderCaps()
        self.requests: list[Any] = []
        self._chunks = [
            StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": reply}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
        ]

    async def stream(self, request: Any) -> Any:
        self.requests.append(request)
        for chunk in self._chunks:
            yield chunk


async def _run(turns: int, *, memory: bool, folded: bool) -> dict[str, Any]:
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fluid.context.compaction import CompactionConfig
    from fluid.engine.native import NativeLoop
    from fluid.events import ContextCompaction
    from fluid.store.base import MemoryStore, iter_events

    note = fixture_note()
    provider = _Recording("reply " * 300 if folded else "ok")
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="fake",
        compaction=(CompactionConfig(trigger_tokens=4_000, keep_recent_turns=1)
                    if folded else CompactionConfig()),
    )
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    adapter._memory_section = lambda: MEMORY if memory else None  # type: ignore[method-assign]
    adapter._env_note = lambda: None  # type: ignore[method-assign]
    adapter._away_note = lambda: None  # type: ignore[method-assign]
    adapter._scratch_note = lambda: note  # type: ignore[method-assign]
    await loop._ensure_session()
    adapter._sdk_session_id = loop.session_id
    try:
        for i in range(turns):
            adapter.set_turn_source("user")
            text = f"Continue code task {i}." + (" words" * 400 if folded else "")
            await adapter.send_query(text)
            async for _event in adapter.receive_events():
                pass
        nodes = await loop.store.load_path(loop.session_id)
        stored = sum((n.message.text() or "").count(HEADER)
                     for n in nodes if n.message.role == "user")
        per_request = [
            "\n".join((m.text() or "") for m in request.messages).count(HEADER)
            for request in provider.requests
        ]
        folds = [e async for e in iter_events(loop.store, loop.session_id, 0)
                 if isinstance(e, ContextCompaction) and e.tier >= 2]
        requested = sum(per_request)
        return {
            "turns": turns,
            "model_requests": len(per_request),
            "block_chars": len(note),
            "stored_copies": stored,
            "stored_chars": stored * len(note),
            "stored_tokens_estimated": (stored * len(note) + 3) // 4,
            "requested_copies": requested,
            "requested_chars": requested * len(note),
            "requested_tokens_estimated": (requested * len(note) + 3) // 4,
            "max_copies_in_one_request": max(per_request, default=0),
            "requests_without_scratch": sum(1 for count in per_request if count == 0),
            "folds": len(folds),
        }
    finally:
        await adapter.disconnect()


def measure(turns: int = 242, folded_turns: int = 30) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
        "TESTING": "1", "DATA_DIR": temp, "AI_SHUFFLE_ENV_FILE": "",
    }), patch("app.db.base.get_connection",
              side_effect=AssertionError("offline measurement opened a database")):
        logging.disable(logging.CRITICAL)
        try:
            return {
                "steady": asyncio.run(_run(turns, memory=True, folded=False)),
                "steady_no_memory": asyncio.run(_run(turns, memory=False, folded=False)),
                "folded": asyncio.run(_run(folded_turns, memory=True, folded=True)),
                "folded_no_memory": asyncio.run(_run(folded_turns, memory=False, folded=True)),
            }
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=242)
    parser.add_argument("--folded-turns", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(measure(args.turns, args.folded_turns), indent=2))
