"""Live smoke test against the real Anthropic API.

Skipped unless ANTHROPIC_API_KEY is set. Asserts the M0 exit criteria:
streamed TextDelta…TurnComplete on turn 1, and cache_read_tokens > 0 on
turn 2 of the same session (the cache-economics invariant).
"""

import os

import pytest

from omni.engine.native import NativeLoop
from omni.events import TextDelta, TurnComplete, Usage, UserTurn
from omni.providers.anthropic.client import AnthropicClient
from omni.store.base import MemoryStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)

# A system prompt comfortably above the 1024-token cache minimum.
SYSTEM = "You are a terse assistant. Answer in one short sentence.\n" + (
    "Context filler line for cache-minimum sizing. " * 200
)


async def test_two_turn_cache_hit() -> None:
    loop = NativeLoop(
        provider=AnthropicClient(),
        store=MemoryStore(),
        model=os.environ.get("OMNI_SMOKE_MODEL", "claude-haiku-4-5-20251001"),
        system=SYSTEM,
        max_tokens=256,
    )
    await loop.submit(UserTurn(content="Say the word 'ready'."))
    await loop.wait_turn()
    events = loop.bus.log(loop.session_id)
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], TurnComplete)

    await loop.submit(UserTurn(content="Now say 'again'."))
    await loop.wait_turn()
    events = loop.bus.log(loop.session_id)
    assert isinstance(events[-1], TurnComplete)
    turn2_usage = [e for e in events if isinstance(e, Usage)][-1]
    assert turn2_usage.cache_read_tokens > 0, (
        f"expected a prefix cache hit on turn 2, got usage={turn2_usage.raw}"
    )
