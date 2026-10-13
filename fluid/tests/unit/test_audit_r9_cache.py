"""Round-9 audit: cache-miss diagnostics that name the varying fragment.

21 misses were ``prefix_changed changed=system`` on the first call after a
park, and 555 calls read zero cache on prefixes the probe judged identical.
The hash alone could not say what varied. The fingerprint now carries
per-paragraph hashes of the system text, a system change is diffed down to
its first differing paragraph (with section name and text head), and every
Usage event carries the provider's own cached-token count next to the
probe's verdict.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.context.cache_probe import (
    SYSTEM_PARAGRAPHS_KEY,
    cache_read_report,
    diagnose_cache_miss,
    fingerprint_prefix,
    system_change_detail,
    system_paragraph_hashes,
    system_text_of,
)
from fluid.context.compiler import CompiledPrompt, PromptSection
from fluid.engine.native import NativeLoop
from fluid.events import Usage, UserTurn
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore


def _messages(n: int) -> list[Message]:
    return [Message(role="user" if i % 2 == 0 else "assistant", content=[Text(f"m{i}")]) for i in range(n)]


SYSTEM_A = "# Base\nyou are a harness\n\n# Environment\nWorking directory: /x\n\n# Tools\nRead, Edit"
SYSTEM_B = "# Base\nyou are a harness\n\n# Environment\nWorking directory: /y\n\n# Tools\nRead, Edit"


def test_fingerprint_carries_paragraph_hashes() -> None:
    fp = fingerprint_prefix(system=SYSTEM_A, tools=[], messages=_messages(4))
    hashes = fp[SYSTEM_PARAGRAPHS_KEY].split()
    assert len(hashes) == 3 and all(len(h) == 4 for h in hashes)
    assert fp[SYSTEM_PARAGRAPHS_KEY] == system_paragraph_hashes(SYSTEM_A)
    # Small: a 20k-token prompt of ~150 paragraphs is under a kilobyte.
    assert len(system_paragraph_hashes("p\n\n" * 150)) < 1_000


def test_system_text_of_joins_block_lists() -> None:
    assert system_text_of("abc") == "abc"
    assert system_text_of([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\n\nb"
    assert system_text_of(None) == ""


def test_system_change_detail_names_the_paragraph_and_section() -> None:
    prev = fingerprint_prefix(system=SYSTEM_A, tools=[], messages=_messages(2))
    cur = fingerprint_prefix(system=SYSTEM_B, tools=[], messages=_messages(2))
    sections = [("base", "# Base\nyou are a harness"),
                ("environment", "# Environment\nWorking directory: /y"),
                ("extra", "# Tools\nRead, Edit")]
    detail = system_change_detail(
        prev, cur, current_text=SYSTEM_B, previous_text=SYSTEM_A, sections=sections,
    )
    assert detail["paragraph_index"] == 1
    assert detail["section"] == "environment"
    assert detail["current_head"].startswith("# Environment\nWorking directory: /y")
    assert detail["previous_head"].startswith("# Environment\nWorking directory: /x")
    # No previous text (across a park): position and current text still land.
    across = system_change_detail(prev, cur, current_text=SYSTEM_B)
    assert across["paragraph_index"] == 1 and "previous_head" not in across
    assert "section" not in across
    # Same system: nothing to report.
    assert system_change_detail(prev, prev, current_text=SYSTEM_A) is None


def test_diagnosis_attaches_the_system_diff() -> None:
    prev = fingerprint_prefix(system=SYSTEM_A, tools=[], messages=_messages(2))
    cur = fingerprint_prefix(system=SYSTEM_B, tools=[], messages=_messages(2))
    detail = system_change_detail(prev, cur, current_text=SYSTEM_B, previous_text=SYSTEM_A)
    diagnosis = diagnose_cache_miss(
        previous=prev, current=cur, input_tokens=50_000, cache_read_tokens=0,
        system_detail=detail,
    )
    assert diagnosis["reason"] == "prefix_changed" and diagnosis["changed"] == "system"
    assert diagnosis["system_diff"]["paragraph_index"] == 1
    assert "paragraph #1" in diagnosis["note"]
    assert "Working directory: /y" in diagnosis["note"]


def test_cache_read_report_distinguishes_partial_and_full() -> None:
    full = cache_read_report({"input_tokens": 1_000, "cache_read_input_tokens": 90_000},
                             input_includes_cached=False)
    assert full["verdict"] == "full_hit" and full["provider_cached_tokens"] == 90_000
    partial = cache_read_report({"input_tokens": 60_000, "cache_read_input_tokens": 20_000},
                                input_includes_cached=False)
    assert partial["verdict"] == "partial_hit" and 0.2 < partial["cached_fraction"] < 0.3
    none = cache_read_report({"input_tokens": 60_000, "cache_read_input_tokens": 0},
                             input_includes_cached=False)
    assert none["verdict"] == "no_cache_read"
    # OpenAI-shaped accounting: cached tokens are inside input_tokens.
    openai = cache_read_report({"input_tokens": 100_000, "cache_read_input_tokens": 95_000},
                               input_includes_cached=True)
    assert openai["verdict"] == "full_hit" and openai["prompt_tokens_total"] == 100_000
    assert cache_read_report({"input_tokens": 10}, input_includes_cached=True)["verdict"] == "small_prompt"


@pytest.mark.parametrize("provider_name", ["openai_responses", "openrouter", "anthropic"])
@pytest.mark.parametrize("created", [0, 4_000])
async def test_normalized_provider_usage_reaches_engine_cache_report(provider_name, created):
    from fluid.providers.openai_responses.serialize import ResponsesStreamNormalizer
    from fluid.providers.openrouter.serialize import ChatCompletionsNormalizer
    from fluid.providers.anthropic.serialize import sse_to_chunk

    class WireProvider:
        name = provider_name
        caps = ProviderCaps(explicit_cache_breakpoints=provider_name == "anthropic")

        async def stream(self, request):
            total = 9_896 + 34_560 + created
            if self.name == "openai_responses":
                normalizer = ResponsesStreamNormalizer()
                events = [
                    ("response.created", {"response": {}}),
                    ("response.output_text.delta", {"item_id": "msg", "delta": "ok"}),
                    ("response.completed", {"response": {"status": "completed", "usage": {
                        "input_tokens": total, "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 34_560, "cache_write_tokens": created},
                    }}}),
                ]
                for name, data in events:
                    for chunk in normalizer.consume(name, data):
                        yield chunk
            elif self.name == "openrouter":
                normalizer = ChatCompletionsNormalizer()
                for data in [
                    {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {
                        "prompt_tokens": total, "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 34_560, "cache_write_tokens": created},
                    }},
                ]:
                    for chunk in normalizer.consume("", data):
                        yield chunk
            else:
                for name, data in [
                    ("message_start", {"message": {"usage": {
                        "input_tokens": 9_896, "cache_read_input_tokens": 34_560,
                        "cache_creation_input_tokens": created,
                    }}}),
                    ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
                    ("content_block_stop", {"index": 0}),
                    ("message_delta", {"usage": {"output_tokens": 5}, "delta": {"stop_reason": "end_turn"}}),
                    ("message_stop", {}),
                ]:
                    chunk = sse_to_chunk(name, data)
                    assert chunk is not None
                    yield chunk

    loop = NativeLoop(provider=WireProvider(), store=MemoryStore(), model="m")
    try:
        await loop.submit(UserTurn(content="test"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
        assert len(usages) == 1
        usage = usages[0]
        assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (9_896, 34_560, created)
        report = usage.raw["cache_read_report"]
        assert report["prompt_tokens_total"] == 44_456 + created
        assert report["cached_fraction"] == round(34_560 / (44_456 + created), 3)
        assert report["verdict"] == "partial_hit"
        assert report["input_includes_cached"] is False
    finally:
        await loop.aclose()


@pytest.mark.parametrize("includes_cached", [False, True])
@pytest.mark.parametrize("value", [-5, "bad", None, float("nan"), float("inf"), -float("inf"), {}, 10**400])
@pytest.mark.parametrize("key", ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
def test_cache_read_report_bounds_malformed_counts(key, value, includes_cached):
    usage = {"input_tokens": 9_896, "cache_read_input_tokens": 34_560, "cache_creation_input_tokens": 0}
    usage[key] = value
    report = cache_read_report(usage, input_includes_cached=includes_cached)
    assert 0 <= report["cached_fraction"] <= 1
    assert report["prompt_tokens_total"] >= 0
    assert report["provider_cached_tokens"] >= 0
    assert report["provider_cache_creation_tokens"] >= 0


@pytest.mark.parametrize("fresh,cached,old_fraction", [
    (9_896, 34_560, 3.492), (50_000, 45_000, 0.9),
    (9_896, 34_560, float("nan")), (9_896, 34_560, -1),
])
async def test_stored_cache_reports_are_recomputed_on_replay(tmp_path, fresh, cached, old_fraction):
    from fluid.store.jsonl import JsonlStore

    store = JsonlStore(tmp_path)
    session = await store.create_session()
    historical = {
        "provider_cached_tokens": cached, "provider_cache_creation_tokens": 0,
        "prompt_tokens_total": fresh, "cached_fraction": old_fraction,
        "verdict": "full_hit", "input_includes_cached": True,
    }
    original = Usage(session_id=session.session_id, seq=0, input_tokens=fresh,
                     cache_read_tokens=cached, raw={"cache_read_report": historical, "other": "preserved"})
    await store.append_event(session.session_id, original)
    replayed = (await JsonlStore(tmp_path).events_since(session.session_id, -1))[0]
    report = replayed.raw["cache_read_report"]
    assert report["verdict"] == "partial_hit"
    assert report["cached_fraction"] == round(cached / (fresh + cached), 3)
    assert report["prompt_tokens_total"] == fresh + cached
    assert report["input_includes_cached"] is False
    assert replayed.raw["other"] == "preserved"
    assert original.raw["cache_read_report"] == historical
    assert original.raw["cache_read_report"]["verdict"] == "full_hit"


class _ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, usages: list[dict]) -> None:
        self.usages = usages
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        usage = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        yield StreamChunk("message_start", {"usage": usage})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def test_usage_carries_cache_read_report_and_a_system_diff_across_engines() -> None:
    """Turn 1 on one engine, turn 2 on a fresh engine over the same store with
    a different system text (the park/wake shape). The second Usage names
    the paragraph that changed even though the first engine is gone."""
    big = "sys " * 20_000
    system_a = f"{big}\n\n# Environment\nWorking directory: /x\n\n# Tools\nRead"
    system_b = f"{big}\n\n# Environment\nWorking directory: /y\n\n# Tools\nRead"
    store = MemoryStore()
    provider = _ScriptedProvider([{"input_tokens": 50_000, "cache_read_input_tokens": 0}])
    first = NativeLoop(provider=provider, store=store, model="m", system=system_a)
    await first.submit(UserTurn(content="turn 0"))
    await asyncio.wait_for(first.wait_turn(), timeout=5)
    sid = first.session_id
    usage0 = [e for e in first.bus.log(sid) if isinstance(e, Usage)][0]
    report = usage0.raw["cache_read_report"]
    assert report["verdict"] == "no_cache_read" and report["provider_cached_tokens"] == 0
    assert SYSTEM_PARAGRAPHS_KEY in usage0.raw["prefix_hashes"]
    await first.aclose()

    sections = CompiledPrompt(sections=(
        PromptSection(kind="base", text=big),
        PromptSection(kind="environment", text="# Environment\nWorking directory: /y"),
        PromptSection(kind="extra", text="# Tools\nRead"),
    ), text=system_b)
    provider2 = _ScriptedProvider([{"input_tokens": 52_000, "cache_read_input_tokens": 0}])
    second = NativeLoop(
        provider=provider2, store=store, model="m", system=system_b,
        system_sections=sections, session_id=sid,
    )
    await second.submit(UserTurn(content="turn 1"))
    await asyncio.wait_for(second.wait_turn(), timeout=5)
    usages = [e for e in second.bus.log(sid) if isinstance(e, Usage)]
    miss = usages[-1].raw["cache_miss"]
    assert miss["reason"] == "prefix_changed" and miss["changed"] == "system"
    diff = miss["system_diff"]
    assert diff["paragraph_index"] == 1
    assert diff["section"] == "environment"
    assert "Working directory: /y" in diff["current_head"]
    assert "previous_head" not in diff  # the old text died with the old process
    await second.aclose()
