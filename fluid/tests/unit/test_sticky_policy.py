"""Per-adapter sticky policy: who drops the cache-affinity key after an empty completion.

OpenRouter's ``session_id`` pins a request to one upstream replica, and a
replica that answered empty stays dead, so the engine drops the key for the
rest of the loop and logs it. The Responses API's ``prompt_cache_key`` is its
prompt-cache affinity: dropping it re-bills the whole prefix and undoes the
routing fix, so the Responses adapter never drops it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from fluid.engine import native
from fluid.engine.native import NativeLoop
from fluid.events import ProviderRetry, UserTurn
from fluid.providers.base import ProviderRequest, StreamChunk
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.store.base import MemoryStore


def _empty() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 0}}),
        StreamChunk("usage", {"usage": {"input_tokens": 0, "output_tokens": 0},
                              "stop_reason": "end_turn"}),
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


class _Scripted:
    """Mixin: replay one scripted chunk list per call instead of doing HTTP."""

    def _script(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(len(self.requests), len(self.scripts) - 1)]
        self.requests.append(request)
        for chunk in script:
            yield chunk


class ScriptedResponses(_Scripted, OpenAIResponsesClient):
    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        OpenAIResponsesClient.__init__(self, api_key="sk-test")
        self._script(scripts)


class ScriptedOpenRouter(_Scripted, OpenRouterClient):
    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        OpenRouterClient.__init__(self, api_key="sk-test")
        self._script(scripts)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


@pytest.mark.parametrize("adapter,key,drops", [
    (ScriptedResponses, "prompt_cache_key", False),
    (ScriptedOpenRouter, "session_id", True),
])
async def test_sticky_policy_per_adapter(
    adapter, key: str, drops: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    # Turn 1: an empty completion, then its retry answers. Turn 2: one call.
    provider = adapter([_empty(), _text("done"), _text("again")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    with caplog.at_level(logging.WARNING, logger="fluid.engine.native"):
        await loop.submit(UserTurn(content="go"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        await loop.submit(UserTurn(content="and again"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
    await loop.aclose()

    assert len(provider.requests) == 3
    assert [r.attempt for r in loop.bus.log(sid) if isinstance(r, ProviderRetry)] == [1]
    sent = [r.extra.get(key) for r in provider.requests]
    dropped_logs = [r for r in caplog.records if "dropping the sticky routing key" in r.getMessage()]
    if drops:
        # The retry and every later call, across the turn boundary, go keyless.
        assert sent == [sid, None, None]
        assert len(dropped_logs) == 1
        assert "openrouter" in dropped_logs[0].getMessage()
    else:
        # The retry and the next turn keep the affinity key.
        assert sent == [sid, sid, sid]
        assert dropped_logs == []
