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
from fluid.events.model import ErrorEvent, FastModeOutcome, ProviderRetry, Usage
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
#: OpenAI's structured refusal of a request field.
TIER_REFUSAL = {"error": {"message": REFUSAL, "type": "invalid_request_error",
                          "param": "service_tier", "code": "unsupported_value"}}


def _refused():
    return fps.error_reply(400, body=TIER_REFUSAL)


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


async def _turn(server, tier: str | None, picks: dict | None = None, *,
                observe: bool = False, **loop_kwargs) -> tuple[list, NativeLoop]:
    """One turn starting at ``tier``; ``picks`` maps a call number (1-based)
    to the tier the host picks just before that call, as a mid-turn pick.
    ``observe`` turns on the service-tier observation, as the host does."""
    loop = NativeLoop(
        provider=OpenAIResponsesClient(
            api_key="k", base_url=server.url + "/v1/responses",
            retry=RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)),
        store=MemoryStore(), model="gpt-6.1-sol", registry=ToolRegistry(),
        permissions=PermissionEngine("never_ask"), **loop_kwargs,
    )
    loop.observe_service_tier = observe
    if tier:
        loop.request_extra["service_tier"] = tier
    if picks:
        stream_once, calls = loop._stream_once, [0]

        async def picking(*args, **kwargs):
            calls[0] += 1
            if calls[0] in picks:
                loop.request_extra.pop("service_tier", None)
                if picks[calls[0]]:
                    loop.request_extra["service_tier"] = picks[calls[0]]
            return await stream_once(*args, **kwargs)

        loop._stream_once = picking
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


async def test_a_refused_ultrafast_runs_the_turn_on_standard_outside_the_call_cap(server):
    """The one deliberate exception to the per-turn model-call cap: an
    Ultrafast request the provider refused did no model work, so it is not
    counted and the Standard request that follows may go out even on a turn
    capped at one call."""
    server.script(_refused(), _reply("still answered", "default"))
    # A budget of one call: the refused attempt ran nothing and is not counted.
    events, _ = await _turn(server, "ultrafast", max_model_calls_per_turn=1)
    assert [r.body.get("service_tier") for r in server.requests] == ["ultrafast", None]
    [retry] = _of(events, ProviderRetry)
    assert "Ultrafast isn't available" in retry.reason
    [outcome] = _of(events, FastModeOutcome)
    assert outcome.disabled_reason == "ultrafast_unavailable"
    assert (outcome.standard_calls, outcome.ultrafast_calls) == (1, 0)
    [done] = _of(events, TurnComplete)
    assert done.num_model_calls == 1


async def test_a_refusal_pins_the_turn_to_standard_and_always_reports(server):
    # Standard at the start, Ultrafast picked before the first call (refused),
    # then Fast before the retry: the retry still runs Standard, and the note
    # comes although neither end of the turn is Ultrafast.
    server.script(_refused(), _reply("answered", "default"))
    events, _ = await _turn(server, None, picks={1: "ultrafast", 2: "priority"})
    assert [r.body.get("service_tier") for r in server.requests] == ["ultrafast", None]
    [outcome] = _of(events, FastModeOutcome)
    assert (outcome.speed, outcome.speed_at_end) == ("standard", "fast")
    assert outcome.disabled_reason == "ultrafast_unavailable"
    assert _of(events, TurnComplete)


async def test_only_the_tier_the_failed_request_sent_counts(server):
    # A Fast request refused for its tier is not an Ultrafast refusal, even
    # when Ultrafast is picked while it fails: no Standard replay.
    server.script(_refused())
    events, _ = await _turn(server, "priority", picks={1: "priority"})
    assert [r.body.get("service_tier") for r in server.requests] == ["priority"]
    assert not _of(events, ProviderRetry)
    assert _of(events, ErrorEvent)


async def test_an_error_that_only_mentions_the_tier_fails_the_turn(server):
    # A tool-schema error naming a tool called ultrafast_search is not a tier
    # refusal: the turn fails instead of replaying on Standard.
    body = {"error": {"message": "Invalid schema for function 'ultrafast_search' (service_tier)",
                      "type": "invalid_request_error", "param": "tools[0].parameters",
                      "code": "invalid_function_parameters"}}
    server.script(fps.error_reply(400, body=body))
    events, _ = await _turn(server, "ultrafast")
    assert len(server.requests) == 1
    assert not _of(events, ProviderRetry)
    assert _of(events, ErrorEvent)


async def test_fast_turns_report_nothing_new(server):
    server.script(_reply("ok", "priority"))
    events, _ = await _turn(server, "priority")
    assert server.requests[0].body["service_tier"] == "priority"
    assert not _of(events, FastModeOutcome)


def test_the_refusal_rule_reads_only_structured_fields():
    import json

    body = json.dumps(TIER_REFUSAL)
    assert _is_ultrafast_rejection({"status": 400, "message": body})
    assert _is_ultrafast_rejection({"status": 403, "message": json.dumps(
        {"error": {"message": "no", "code": "service_tier_unavailable"}})})
    # An in-stream error carries the fields on the chunk itself.
    assert _is_ultrafast_rejection({"message": "{...}", "param": "service_tier"})
    # The words alone never count.
    assert not _is_ultrafast_rejection({"status": 400, "message": REFUSAL})
    # Nor do rate limits, server errors, retriable or transport failures.
    assert not _is_ultrafast_rejection({"status": 429, "message": body})
    assert not _is_ultrafast_rejection({"status": 503, "message": body})
    assert not _is_ultrafast_rejection({"param": "service_tier", "retriable": True})
    assert not _is_ultrafast_rejection({"param": "service_tier", "transport_failure": {}})
    # A context or tool-schema error names another field.
    assert not _is_ultrafast_rejection({"status": 400, "message": json.dumps(
        {"error": {"param": "input", "code": "context_length_exceeded"}})})


@pytest.mark.parametrize("reported, counts", [
    # (ultrafast, fast, standard, unknown) calls for one answered request.
    ("ultrafast", (1, 0, 0, 0)),
    ("default", (0, 0, 1, 0)),
    ("priority", (0, 1, 0, 0)),
    (None, (0, 0, 0, 1)),
    # A tier the module does not know is evidence of no speed at all.
    ("auto", (0, 0, 0, 1)),
    ("ultra_fast", (0, 0, 0, 1)),
])
async def test_the_response_tier_decides_how_an_ultrafast_call_counts(server, reported, counts):
    server.script(_reply("answered", reported))
    events, _ = await _turn(server, "ultrafast")
    assert server.requests[0].body["service_tier"] == "ultrafast"
    [outcome] = _of(events, FastModeOutcome)
    assert (outcome.ultrafast_calls, outcome.fast_calls,
            outcome.standard_calls, outcome.unknown_calls) == counts
    assert outcome.disabled_reason is None


_OBSERVED = ("service_tier_requested", "service_tier_reported")


@pytest.mark.parametrize("asked, reported, observed", [
    ("ultrafast", "ultrafast", ("ultrafast", "ultrafast")),
    ("ultrafast", "default", ("ultrafast", "default")),
    ("ultrafast", None, ("ultrafast", "absent")),
    # Standard sends no tier; the response's own answer is kept verbatim.
    (None, "default", ("none", "default")),
    (None, "Auto", ("none", "auto")),
    # Anything that is not a short tier token is not kept.
    (None, "default; user=someone@example.com", ("none", "unrecognized_format")),
])
async def test_an_observed_turn_records_each_calls_tiers_on_its_usage(server, asked, reported, observed):
    server.script(_reply("answered", reported))
    events, _ = await _turn(server, asked, observe=True)
    [usage] = [u for u in _of(events, Usage) if u.raw.get("model_call_index") is not None]
    assert tuple(usage.raw.get(key) for key in _OBSERVED) == observed


async def test_an_unobserved_turn_records_no_tiers(server):
    server.script(_reply("answered", "default"))
    events, loop = await _turn(server, "ultrafast")
    assert loop.observe_service_tier is False
    usages = _of(events, Usage)
    assert usages and not any(key in u.raw for u in usages for key in _OBSERVED)


def test_reported_tier_classifies_the_call():
    assert _reported_speed("ultrafast", "ultrafast") == "ultrafast"
    assert _reported_speed("ultrafast", "Ultrafast") == "ultrafast"
    assert _reported_speed("ultrafast", "default") == "standard"
    assert _reported_speed("priority", "priority") == "fast"
    assert _reported_speed("ultrafast", None) == "unknown"
    assert _reported_speed("ultrafast", "") == "unknown"
    # Unrecognized values are never read as Standard, nor as the tier asked for.
    for value in ("auto", "flex", "scale", "ultra_fast", "standard-ish"):
        assert _reported_speed("ultrafast", value) == "unknown"
