"""count_tokens determinism + monotonicity tests (§D5, case 8)."""

from __future__ import annotations

from app.core.translator_gateway.token_estimate import count_tokens


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
