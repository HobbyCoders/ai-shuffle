"""OpenAI Ultrafast on the native Responses leg: wire tier, refusal fallback, outcome.

Runs the real ``NativeLoop`` and Responses client against the scripted
loopback provider, and asserts both on what was sent and on the turn's
:class:`FastModeOutcome`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from fluid.engine.native import NativeLoop, _is_ultrafast_rejection, _reported_speed
from fluid.events import TurnComplete, UserTurn
from fluid.events.model import FastModeOutcome, ProviderRetry
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolRegistry


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "fixtures" / "fake_provider_server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
Responses = fps.Responses
REFUSAL = "The requested service_tier 'ultrafast' is not available for this model or account."


def _reply(text: str, tier: str | None) -> list:
    extra = {"service_tier": tier} if tier else {}
    return [Responses.created(), *Responses.message("msg_1", text), Responses.completed(**extra)]


@pytest.fixture
def server():
    srv = fps.FakeProviderServer()
    try:
        yield srv
    finally:
        srv.close()


async def _turn(server, tier: str | None) -> tuple[list, NativeLoop]:
    loop = NativeLoop(
        provider=OpenAIResponsesClient(
            api_key="k", base_url=server.url + "/v1/responses",
            retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)),
        store=MemoryStore(), model="gpt-6.1-sol", registry=ToolRegistry(),
        permissions=PermissionEngine("never_ask"),
    )
    if tier:
        loop.request_extra["service_tier"] = tier
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), 20.0)
    events = loop.bus.log(loop.session_id)
    await loop.aclose()
    return events, loop


def _of(events, cls):
    return [e for e in events if isinstance(e, cls)]


async def test_ultrafast_is_sent_and_reported(server):
    server.script(_reply("fast answer", "ultrafast"))
    events, _ = await _turn(server, "ultrafast")
    assert server.requests[0].body["service_tier"] == "ultrafast"
    [outcome] = _of(events, FastModeOutcome)
    assert (outcome.ultrafast_calls, outcome.speed, outcome.disabled_reason) == (1, "ultrafast", None)
    assert _of(events, TurnComplete)


async def test_a_refused_ultrafast_runs_the_turn_on_standard(server):
    server.script(fps.error_reply(400, REFUSAL), _reply("still answered", "default"))
    events, _ = await _turn(server, "ultrafast")
    assert [r.body.get("service_tier") for r in server.requests] == ["ultrafast", None]
    [retry] = _of(events, ProviderRetry)
    assert "Ultrafast isn't available" in retry.reason
    [outcome] = _of(events, FastModeOutcome)
    assert outcome.disabled_reason == "ultrafast_unavailable"
    assert (outcome.standard_calls, outcome.ultrafast_calls) == (1, 0)
    assert _of(events, TurnComplete)


async def test_fast_turns_report_nothing_new(server):
    server.script(_reply("ok", "priority"))
    events, _ = await _turn(server, "priority")
    assert server.requests[0].body["service_tier"] == "priority"
    assert not _of(events, FastModeOutcome)


def test_the_refusal_rule_skips_rate_limits_and_server_errors():
    assert _is_ultrafast_rejection(REFUSAL, 400)
    assert _is_ultrafast_rejection("Unsupported value for service_tier", 403)
    assert not _is_ultrafast_rejection("Rate limit for service tier ultrafast reached", 429)
    assert not _is_ultrafast_rejection(REFUSAL, 503)
    assert not _is_ultrafast_rejection("tools.0.name: invalid", 400)
    assert not _is_ultrafast_rejection("ultrafast rate_limit exceeded", None)


def test_reported_tier_classifies_the_call():
    assert _reported_speed("ultrafast", "ultrafast") == "ultrafast"
    assert _reported_speed("ultrafast", "default") == "standard"
    assert _reported_speed("priority", "priority") == "fast"
    assert _reported_speed("ultrafast", None) == "unknown"
