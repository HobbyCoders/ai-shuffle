"""Two things a large tool call needs: room to be written, and visibility while
it is being written.

fluid capped every request at 8192 output tokens, so a Write whose content ran
past roughly 32 KB had its arguments cut off mid-JSON and the call could not be
run at all. And a tool call's arguments were never forwarded as they streamed,
so a client knew *which* tool had started but saw nothing of it until the whole
call had been generated — many silent seconds for exactly the large calls that
most need progress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from fluid.engine.native import NativeLoop, _explain_truncation, _parse_tool_args
from fluid.events import ToolArgsDelta, ToolResult, ToolUse, TurnComplete, UserTurn
from fluid.messages import Message
from fluid.permissions.engine import PermissionEngine
from fluid.providers.anthropic.limits import max_output_tokens, resolve_max_tokens
from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.local.serialize import build_request_body as local_body
from fluid.providers.openrouter.serialize import build_request_body as openrouter_body
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


def _request(**kwargs) -> ProviderRequest:
    return ProviderRequest(
        model=kwargs.pop("model", "some-model"),
        messages=[Message(role="user", content=[])],
        **kwargs,
    )


# ── the wire: uncapped means "the model's own maximum" ───────────────────


def test_provider_request_is_uncapped_by_default() -> None:
    assert _request().max_tokens is None


@pytest.mark.parametrize("build", [openrouter_body, local_body])
def test_openai_shaped_bodies_omit_an_absent_cap(build) -> None:
    """Omission is how these dialects say "use the model's maximum"."""
    assert "max_tokens" not in build(_request())
    assert build(_request(max_tokens=4096))["max_tokens"] == 4096


def test_anthropic_resolves_a_cap_because_the_api_demands_one() -> None:
    # Anthropic rejects a request with no max_tokens, and rejects one above the
    # model's ceiling — so it is resolved rather than omitted or guessed high.
    body = anthropic_body(_request(model="claude-sonnet-5"))
    assert body["max_tokens"] == max_output_tokens("claude-sonnet-5")
    assert body["max_tokens"] > 8192


@pytest.mark.parametrize("model", [
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-opus-5-5[1m]", "claude-sonnet-5-5-latest",
    "claude-opus-5-5-20260601", "claude-sonnet-5-5-20260601",
    "CLAUDE-SONNET-5-5",
])
@pytest.mark.parametrize("requested", [None, 96_000, 128_000, 999_999])
def test_claude_5_5_output_ceiling_on_the_wire(model, requested) -> None:
    assert max_output_tokens(model) == 128_000
    body = anthropic_body(_request(model=model, max_tokens=requested))
    assert body["max_tokens"] == (96_000 if requested == 96_000 else 128_000)


@pytest.mark.parametrize(("model", "ceiling"), [
    ("claude-opus-5", 64_000), ("claude-sonnet-5", 64_000),
    ("claude-opus-4-8", 64_000), ("claude-opus-4-7", 64_000),
    ("claude-opus-4-6", 64_000), ("claude-opus-4-5", 64_000),
    ("claude-sonnet-4-6", 64_000), ("claude-sonnet-4-5", 64_000),
    ("claude-haiku-4-5", 64_000), ("claude-opus-4-1", 32_000),
    ("claude-opus-4-20250514", 32_000), ("claude-3-opus-20240229", 4096),
])
def test_older_claude_output_ceilings_are_unchanged(model, ceiling) -> None:
    assert max_output_tokens(model) == ceiling
    assert resolve_max_tokens(model, None) == ceiling
    assert resolve_max_tokens(model, 999_999) == ceiling


def test_anthropic_clamps_an_over_large_explicit_cap() -> None:
    body = anthropic_body(_request(model="claude-3-opus-20240229", max_tokens=999_999))
    assert body["max_tokens"] == 4096


def test_unknown_models_get_the_universally_accepted_ceiling() -> None:
    assert resolve_max_tokens("some-new-model", None) == 8192


def test_native_loop_defaults_to_uncapped() -> None:
    loop = NativeLoop(provider=_Idle(), store=MemoryStore(), model="claude-sonnet-5")
    assert loop.max_tokens is None


# ── truncation names its real cause ──────────────────────────────────────


def test_an_exhausted_output_budget_is_named_as_the_cause() -> None:
    _args, err = _parse_tool_args('{"content":"aaa')
    explained = _explain_truncation(err, "max_tokens")
    assert "output-token limit" in explained
    assert "the payload itself is not the problem" in explained
    assert "max_tokens" in explained
    # The preview stays at the end, where it reads as evidence.
    assert explained.rstrip().endswith('{"content":"aaa')


def test_a_dropped_connection_gets_no_budget_advice() -> None:
    """Blaming the output budget for a dead socket would send the model to fix
    the wrong thing."""
    _args, err = _parse_tool_args('{"content":"aaa')
    assert _explain_truncation(err, "end_turn") == err
    assert "output-token limit" not in err


# ── streaming: arguments are visible as they are written ─────────────────


class _Idle:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk("message_stop", {})


class _ScriptedProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _streamed_call(fragments: list[str], *, stop: str = "tool_use") -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk(
            "block_start",
            {"index": 0, "block_type": "tool_use", "tool_name": "Write", "tool_use_id": "w1"},
        ),
        *[
            StreamChunk("tool_args_delta", {"index": 0, "partial_json": part})
            for part in fragments
        ],
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": stop}),
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


def _write_registry() -> ToolRegistry:
    async def _wrote(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=f"wrote {len(input.get('content', ''))} bytes")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Write", description="Write a file.",
            input_schema={"type": "object", "properties": {}}, source="builtin",
        ),
        _wrote,
    )
    return registry


async def test_arguments_stream_as_the_model_writes_them() -> None:
    fragments = ['{"file_path":"/a.txt",', '"content":"hello ', 'world"}']
    provider = _ScriptedProvider([_streamed_call(fragments), _text("done")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="stealth/ox-alpha",
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="write it"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)

        deltas = [e for e in events if isinstance(e, ToolArgsDelta)]
        assert [d.partial_json for d in deltas] == fragments
        # Each fragment names its call, so a client can attribute it without
        # tracking block indices itself.
        assert {d.tool_use_id for d in deltas} == {"w1"}
        assert {d.name for d in deltas} == {"Write"}

        # The deltas are progress only — the parsed arguments still arrive with
        # ToolUse, and they are complete.
        use = next(e for e in events if isinstance(e, ToolUse))
        assert use.input == {"file_path": "/a.txt", "content": "hello world"}

        # Every delta precedes the completed call.
        order = [type(e).__name__ for e in events]
        assert order.index("ToolArgsDelta") < order.index("ToolUse")
    finally:
        await loop.aclose()


async def test_a_call_truncated_by_the_budget_still_gets_a_result() -> None:
    """stop_reason is max_tokens, not tool_use, so the dispatch loop used to
    skip straight to its continuation branch — leaving an emitted ToolUse with
    no result, which reads in the UI as a tool that never finishes."""
    provider = _ScriptedProvider([
        _streamed_call(['{"file_path":"/a.txt","content":"aaa'], stop="max_tokens"),
        _text("recovered"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="stealth/ox-alpha",
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="write it"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)

        uses = [e for e in events if isinstance(e, ToolUse)]
        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(uses) == len(results) == 1
        assert results[0].is_error
        assert "output-token limit" in results[0].output
        assert "wrote" not in results[0].output  # never executed

        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "end_turn"
    finally:
        await loop.aclose()
