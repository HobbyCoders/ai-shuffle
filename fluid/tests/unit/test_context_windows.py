"""Model → context-window table (fluid.providers.windows)."""

import pytest

from fluid.providers.windows import bare_model_id, context_window_for


@pytest.mark.parametrize(
    ("model", "window"),
    [
        ("claude-opus-5", 1_000_000),
        ("claude-fable-5-1", 1_000_000),
        ("claude-sonnet-5", 1_000_000),
        ("claude-sonnet-4-6", 1_000_000),
        ("claude-opus-4-6", 1_000_000),
        ("claude-haiku-4-5-20251001", 200_000),
        ("claude-haiku-5-5", 1_000_000),
        ("anthropic/claude-haiku-5-5", 1_000_000),
        ("claude-opus-5-5", 1_000_000),
        ("claude-sonnet-5-5", 1_000_000),
        ("claude-sonnet-4-5", 200_000),
        ("claude-opus-4-5", 200_000),
        ("claude-opus-4-1-20250805", 200_000),
        ("claude-3-5-sonnet-20241022", 200_000),
        ("claude-sonnet-4-5[1m]", 1_000_000),
        ("gpt-5.6-sol", 400_000),
        ("gpt-5-codex", 400_000),
        ("gpt-4.1-mini", 1_047_576),
        ("gpt-4o", 128_000),
        ("o3-mini", 200_000),
        ("o4-mini", 200_000),
        ("anthropic/claude-opus-5", 1_000_000),
        ("anthropic/claude-sonnet-4-5", 200_000),
        ("openai/gpt-5-mini", 400_000),
        ("anthropic.claude-haiku-4-5", 200_000),
    ],
)
def test_known_ids_resolve_by_longest_prefix(model: str, window: int) -> None:
    assert context_window_for(model) == window


@pytest.mark.parametrize("model", ["", None, "qwen2.5-coder", "local/llama-3", "gpt-oss-120b"])
def test_local_and_unknown_ids_stay_unknown(model) -> None:
    assert context_window_for(model) is None


def test_lookup_ignores_case_and_whitespace() -> None:
    assert context_window_for("  Claude-Opus-5 ") == 1_000_000


def test_vendor_prefixes_are_stripped_before_the_lookup() -> None:
    assert bare_model_id("anthropic/claude-opus-5") == "claude-opus-5"
    assert bare_model_id("anthropic.claude-opus-5") == "claude-opus-5"
    assert bare_model_id("openai/gpt-5-mini:free") == "gpt-5-mini:free"
    assert bare_model_id(None) == ""
