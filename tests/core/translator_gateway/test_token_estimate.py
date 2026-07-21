"""count_tokens determinism + monotonicity tests (§D5, case 8).

Also covers the tiktoken (o200k_base) counting path added to fix the
"Prompt is too long" under-count, plus the heuristic fallback and its
failure-caching behavior (see token_estimate module docstring)."""

from __future__ import annotations

import pytest

from app.core.translator_gateway import token_estimate
from app.core.translator_gateway.token_estimate import count_tokens


@pytest.fixture(autouse=True)
def _reset_encoder_cache():
    """Each test starts from a clean lazy-encoder cache so monkeypatching the
    getter's inputs actually takes effect (the cache is module-global)."""
    saved = (token_estimate._ENCODER, token_estimate._ENCODER_INIT_DONE)
    token_estimate._ENCODER = None
    token_estimate._ENCODER_INIT_DONE = False
    try:
        yield
    finally:
        token_estimate._ENCODER, token_estimate._ENCODER_INIT_DONE = saved


def _force_heuristic(monkeypatch):
    """Pin the encoder getter into its cached-failure (heuristic) state."""
    monkeypatch.setattr(token_estimate, "_get_encoder", lambda: None)


def test_deterministic():
    body = {
        "model": "m",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello there, how are you today?"}],
    }
    assert count_tokens(body) == count_tokens(body)
    assert count_tokens(dict(body)) == count_tokens(dict(body))


def test_empty_request_is_small_nonnegative():
    n = count_tokens({"model": "m", "messages": []})
    assert n >= 0
    assert n < 20


def test_monotonic_in_message_text():
    short = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    long = {"model": "m", "messages": [{"role": "user", "content": "hi " * 500}]}
    assert count_tokens(long) > count_tokens(short)


def test_monotonic_in_message_count():
    one = {"model": "m", "messages": [{"role": "user", "content": "same text"}]}
    two = {"model": "m", "messages": [
        {"role": "user", "content": "same text"},
        {"role": "assistant", "content": "same text"},
    ]}
    assert count_tokens(two) > count_tokens(one)


def test_monotonic_in_tools():
    no_tools = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    with_tools = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "Gets the weather for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
    }
    assert count_tokens(with_tools) > count_tokens(no_tools)


def test_system_prompt_increases_count():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    with_sys = {"model": "m", "system": "A long system prompt here.", "messages": [{"role": "user", "content": "x"}]}
    assert count_tokens(with_sys) > count_tokens(base)


def test_string_and_block_content_equivalent_size_order():
    # richer content (tool_use with big input) counts more than a tiny string
    tiny = {"model": "m", "messages": [{"role": "assistant", "content": "ok"}]}
    rich = {"model": "m", "messages": [{"role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "c1", "name": "do", "input": {"payload": "x" * 200}},
    ]}]}
    assert count_tokens(rich) > count_tokens(tiny)


# ── tiktoken (o200k_base) counting path ──────────────────────────────────────

_CODE = (
    "def transform(records):\n"
    "    return {r['id']: json.dumps(r, separators=(',', ':')) for r in records}\n"
    "# handles nested {\"a\": [1, 2, 3], \"b\": {\"c\": true}} payloads\n"
) * 4


def test_tiktoken_path_uses_encoder_count_not_chars_over_four():
    """A code-heavy body counts with the real BPE tokenizer, not chars/4."""
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("o200k_base")

    body = {"model": "m", "messages": [{"role": "user", "content": _CODE}]}

    expected = (
        token_estimate._BASE_OVERHEAD
        + token_estimate._PER_MESSAGE_OVERHEAD
        + len(enc.encode("user"))
        + len(enc.encode(_CODE))
    )
    assert count_tokens(body) == expected

    # Code tokenizes hotter than 4 chars/token, so the encoder count must exceed
    # the old heuristic — this is exactly the under-count the fix addresses.
    heuristic = (
        token_estimate._BASE_OVERHEAD
        + token_estimate._PER_MESSAGE_OVERHEAD
        + token_estimate._chars_to_tokens(len("user") + len(_CODE))
    )
    assert count_tokens(body) > heuristic


# ── heuristic fallback path (encoder unavailable) ────────────────────────────

def test_heuristic_fallback_matches_prechange_behavior(monkeypatch):
    """With the encoder forced unavailable, the count is byte-identical to the
    pre-change sum-chars-then-single-ceil heuristic."""
    _force_heuristic(monkeypatch)

    body = {
        "model": "m",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello there, how are you today?"}],
    }
    # Pre-change algorithm, computed by hand:
    #   base 3 + system-overhead 3 + message-overhead 4 = 10
    #   chars = len("You are helpful.")=16 + len("user")=4 + len(content)=31 = 51
    #   ceil(51 / 4) = 13  →  10 + 13 = 23
    assert count_tokens(body) == 23


def test_heuristic_fallback_generic_equivalence(monkeypatch):
    """Fallback equals base + overheads + single ceil over summed char lengths,
    for an arbitrary body (matches the original implementation exactly)."""
    import json as _json

    _force_heuristic(monkeypatch)

    body = {
        "model": "m",
        "system": "sys prompt",
        "messages": [
            {"role": "user", "content": "first message body"},
            {"role": "assistant", "content": "second, a bit longer reply"},
        ],
        "tools": [
            {"name": "do_thing", "description": "does a thing",
             "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        ],
    }

    total = token_estimate._BASE_OVERHEAD
    chars = 0
    total += token_estimate._PER_SYSTEM_OVERHEAD
    chars += len("sys prompt")
    for role, content in (("user", "first message body"),
                          ("assistant", "second, a bit longer reply")):
        total += token_estimate._PER_MESSAGE_OVERHEAD
        chars += len(role) + len(content)
    total += token_estimate._PER_TOOL_OVERHEAD
    chars += len("do_thing") + len("does a thing")
    chars += len(_json.dumps(
        {"type": "object", "properties": {"q": {"type": "string"}}},
        separators=(",", ":"), ensure_ascii=False,
    ))
    expected = total + token_estimate._chars_to_tokens(chars)

    assert count_tokens(body) == expected


# ── failure caching (encoder init attempted at most once) ────────────────────

def test_encoder_init_attempted_only_once_after_failure(monkeypatch):
    """A failing tiktoken.get_encoding is attempted exactly once; subsequent
    counts reuse the cached failure and take the heuristic path."""
    tiktoken = pytest.importorskip("tiktoken")

    calls = {"n": 0}

    def _boom(name):
        calls["n"] += 1
        raise RuntimeError("offline: BPE fetch failed")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)

    body = {"model": "m", "messages": [{"role": "user", "content": "some text here"}]}
    first = count_tokens(body)
    second = count_tokens(body)
    third = count_tokens(body)

    assert calls["n"] == 1  # failure cached, not retried per call
    # Heuristic path is used after the cached failure.
    assert first == second == third
    assert token_estimate._ENCODER is None
    assert token_estimate._ENCODER_INIT_DONE is True
