"""Effort and thinking on the native Anthropic lane.

Three defects, verified at 88292d124: ``_build_request`` never sent
``thinking``, so a user's explicit thinking setting was dropped on this lane;
effort passed through verbatim, so ``xhigh`` reached Opus 4.6 and Sonnet 4.6,
which reject it; and nothing stopped a thinking setting from being switched on
under a session whose history ran without it. Thinking stays off by default:
turning it on per model waits on experiment E5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from fluid.engine.native import NativeLoop
from fluid.events import TurnStarted, UserTurn
from fluid.events.model import event_from_dict, event_to_dict
from fluid.providers.anthropic.serialize import build_request_body
from fluid.providers.anthropic.thinking import clamp_effort, resolve_thinking
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore

ADAPTIVE_SUMMARIZED = {"type": "adaptive", "display": "summarized"}


# ─── effort clamp ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "effort", "sent"),
    [
        # 4.6 generation: no xhigh. It becomes the highest level below it.
        ("claude-opus-4-6", "xhigh", "high"),
        ("claude-sonnet-4-6", "xhigh", "high"),
        ("claude-opus-4-6", "max", "max"),
        ("claude-sonnet-4-6", "medium", "medium"),
        # Opus 4.5: low/medium/high only.
        ("claude-opus-4-5-20251101", "max", "high"),
        ("claude-opus-4-5-20251101", "xhigh", "high"),
        # Models that reject effort get none.
        ("claude-haiku-4-5", "high", None),
        ("claude-sonnet-4-5", "high", None),
        ("claude-3-5-sonnet-20241022", "low", None),
        # Models that take the whole ladder keep the value.
        ("claude-opus-4-7", "xhigh", "xhigh"),
        ("claude-opus-4-8", "xhigh", "xhigh"),
        ("claude-sonnet-5", "xhigh", "xhigh"),
        ("claude-opus-5", "max", "max"),
        ("claude-opus-5-5", "xhigh", "xhigh"),
        ("claude-sonnet-5-5", "max", "max"),
        ("claude-haiku-5-5", "xhigh", "xhigh"),
        ("claude-haiku-5-5", "max", "max"),
        ("claude-fable-5-1", "xhigh", "xhigh"),
        # Non-Claude ids and unset effort pass through.
        ("glm-5.3", "xhigh", "xhigh"),
        ("claude-opus-4-6", None, None),
    ],
)
def test_effort_clamp_table(model, effort, sent) -> None:
    assert clamp_effort(model, effort) == sent


def test_xhigh_never_reaches_a_4_6_model_on_the_wire() -> None:
    for model in ("claude-opus-4-6", "claude-sonnet-4-6"):
        body = build_request_body(ProviderRequest(model=model, messages=[], effort="xhigh"))
        assert body["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_haiku_5_5_wire_accepts_effort_and_uses_native_limits(effort) -> None:
    body = build_request_body(ProviderRequest(
        model="claude-haiku-5-5", messages=[], effort=effort,
        thinking={"type": "disabled"},
    ))
    assert body["output_config"]["effort"] == effort
    assert body["max_tokens"] == 128_000
    assert "thinking" not in body
    adaptive = build_request_body(ProviderRequest(
        model="claude-haiku-5-5", messages=[], effort=effort,
        thinking={"type": "enabled", "budget_tokens": 8000},
    ))
    assert adaptive["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in adaptive["thinking"]


@pytest.mark.parametrize("model", [
    "claude-sonnet-5-5",
    "claude-sonnet-5-5[1m]",
    "claude-sonnet-5-5-latest",
    "claude-sonnet-5-5-20260601",
    "anthropic.claude-sonnet-5-5-v1:0",
])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
def test_sonnet_5_5_never_sends_disabled_thinking(model, effort) -> None:
    body = build_request_body(ProviderRequest(
        model=model, messages=[], effort=effort, thinking={"type": "disabled"},
    ))
    assert "thinking" not in body
    if effort is not None:
        assert body["output_config"]["effort"] == effort


def test_a_model_that_rejects_effort_gets_no_output_config() -> None:
    body = build_request_body(
        ProviderRequest(model="claude-haiku-4-5", messages=[], effort="high")
    )
    assert "output_config" not in body


# ─── thinking fitted per model ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "thinking", "effort", "sent"),
    [
        # Explicit settings pass through where the model takes them.
        ("claude-opus-4-8", ADAPTIVE_SUMMARIZED, "high", ADAPTIVE_SUMMARIZED),
        ("claude-opus-4-8", {"type": "disabled"}, "high", {"type": "disabled"}),
        ("claude-sonnet-4-6", {"type": "enabled", "budget_tokens": 8000}, None,
         {"type": "enabled", "budget_tokens": 8000}),
        # A budget is removed from 4.7 on: the user asked for thinking, so adaptive.
        ("claude-opus-4-8", {"type": "enabled", "budget_tokens": 8000}, None,
         {"type": "adaptive"}),
        # Display alone keeps the model's default mode: it never turns thinking
        # on for a model that runs without it, and shows it where it is on.
        ("claude-opus-4-8", {"display": "summarized"}, None, None),
        ("claude-opus-5", {"display": "summarized"}, None, ADAPTIVE_SUMMARIZED),
        # Always-thinking models reject "disabled"; Opus 5 only at xhigh/max.
        ("claude-opus-5-5", {"type": "disabled"}, "low", None),
        ("claude-haiku-5-5", {"type": "disabled"}, "low", None),
        ("claude-haiku-5-5", {"type": "disabled"}, "max", None),
        ("claude-haiku-5-5", {"type": "adaptive"}, "medium", {"type": "adaptive"}),
        ("claude-haiku-5-5", {"type": "enabled", "budget_tokens": 8000}, None, {"type": "adaptive"}),
        ("claude-haiku-5-5", {"display": "summarized"}, None, ADAPTIVE_SUMMARIZED),
        ("claude-fable-5-1", {"type": "disabled"}, None, None),
        ("claude-opus-5", {"type": "disabled"}, "xhigh", None),
        ("claude-opus-5", {"type": "disabled"}, "high", {"type": "disabled"}),
        # No adaptive mode before 4.6.
        ("claude-haiku-4-5", {"type": "adaptive"}, None, None),
        # Unset stays unset. A Claude id this module cannot parse still gets
        # the host's value; other Anthropic-dialect models get nothing.
        ("claude-opus-4-8", None, None, None),
        ("claude-next-preview", {"type": "adaptive"}, None, {"type": "adaptive"}),
        ("some-proxy-model", {"type": "adaptive"}, None, None),
        ("glm-5.1", {"type": "enabled", "budget_tokens": 8000}, None, None),
        ("kimi-k2.5", {"type": "adaptive"}, None, None),
        ("MiniMax-M2", {"type": "disabled"}, None, None),
    ],
)
def test_thinking_fitted_per_model(model, thinking, effort, sent) -> None:
    assert resolve_thinking(model, thinking, effort) == sent


def test_a_budget_stays_below_max_tokens() -> None:
    body = build_request_body(ProviderRequest(
        model="claude-sonnet-4-6", messages=[], max_tokens=4000,
        thinking={"type": "enabled", "budget_tokens": 64000},
    ))
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 3999}


# ─── forwarding through NativeLoop ───────────────────────────────────────────


class _Provider:
    """Records each request and answers with one short text reply."""

    name = "anthropic"
    caps = ProviderCaps(
        explicit_cache_breakpoints=True, max_cache_breakpoints=4, signed_thinking=True,
    )

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"output_tokens": 1, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {"stop_reason": "end_turn"})


async def _one_turn(loop: NativeLoop) -> None:
    await loop.submit(UserTurn(content="hi"))
    await loop.wait_turn()


def _body(provider: _Provider) -> dict:
    return build_request_body(provider.requests[-1])


async def test_thinking_is_not_on_by_default() -> None:
    provider = _Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-opus-4-8", effort="high",
    )
    await _one_turn(loop)

    body = _body(provider)
    assert "thinking" not in body
    assert body["output_config"] == {"effort": "high"}


async def test_explicit_thinking_reaches_a_new_session() -> None:
    provider = _Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-opus-4-8",
        effort="high", thinking=ADAPTIVE_SUMMARIZED,
    )
    await _one_turn(loop)

    assert provider.requests[-1].thinking == ADAPTIVE_SUMMARIZED
    assert _body(provider)["thinking"] == ADAPTIVE_SUMMARIZED
    started = [e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnStarted)]
    assert started[-1].thinking == ADAPTIVE_SUMMARIZED


async def test_a_resumed_session_without_thinking_keeps_none() -> None:
    """A history that ran without thinking never has it switched on: the
    setting applies to new sessions only."""
    store = MemoryStore()
    first = _Provider()
    loop = NativeLoop(provider=first, store=store, model="claude-opus-4-8")
    await _one_turn(loop)
    sid = loop.session_id

    second = _Provider()
    resumed = NativeLoop(
        provider=second, store=store, model="claude-opus-4-8",
        session_id=sid, thinking=ADAPTIVE_SUMMARIZED,
    )
    await _one_turn(resumed)

    assert second.requests[-1].thinking is None
    assert "thinking" not in _body(second)


async def test_a_resumed_session_with_thinking_keeps_it() -> None:
    """The other direction: an engine rebuilt without the setting (or with a
    changed one) does not take thinking away from a session that has it."""
    store = MemoryStore()
    first = _Provider()
    loop = NativeLoop(
        provider=first, store=store, model="claude-opus-4-8", thinking=ADAPTIVE_SUMMARIZED,
    )
    await _one_turn(loop)
    sid = loop.session_id

    second = _Provider()
    resumed = NativeLoop(
        provider=second, store=store, model="claude-opus-4-8", session_id=sid,
    )
    await _one_turn(resumed)

    assert _body(second)["thinking"] == ADAPTIVE_SUMMARIZED


def test_turn_started_rows_from_before_the_field_load_as_none() -> None:
    row = event_to_dict(TurnStarted(session_id="s", seq=1, model="m", provider="p"))
    row.pop("thinking")
    event = event_from_dict(row)
    assert isinstance(event, TurnStarted)
    assert event.thinking is None


@pytest.mark.parametrize("model", ["glm-5.1", "kimi-k2.5", "MiniMax-M2"])
async def test_thinking_is_not_forwarded_to_other_anthropic_dialect_models(
    model: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """GLM, Kimi and MiniMax take the Anthropic dialect but not its thinking
    field: the request goes out exactly as it would with no setting."""
    import logging

    plain, explicit = _Provider(), _Provider()
    await _one_turn(NativeLoop(provider=plain, store=MemoryStore(), model=model))
    with caplog.at_level(logging.DEBUG, logger="fluid.providers.anthropic.thinking"):
        await _one_turn(NativeLoop(
            provider=explicit, store=MemoryStore(), model=model,
            thinking=ADAPTIVE_SUMMARIZED,
        ))
        body = _body(explicit)

    assert "thinking" not in body
    assert body == _body(plain)
    assert any(
        "not forwarded" in r.getMessage() and r.levelno == logging.DEBUG
        for r in caplog.records
    )
