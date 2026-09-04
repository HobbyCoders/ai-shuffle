"""Tests for media-vs-text model classification.

The load-bearing property is the DIRECTION of the default: an id this rule does
not recognise must stay visible. Hiding a chat model the user needs is a bug
they cannot work around from the UI; showing one extra media id is cosmetic.
"""

import pytest

from app.core import model_modality
from app.core.model_modality import (
    _MEDIA_SEGMENTS,
    _NEVER_A_SIGNAL,
    is_media_model,
    media_kind,
    split_media_models,
)


# ---------------------------------------------------------------------------
# Media models — hidden from the chat picker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # Realtime voice, including the two the app's own allowlist carries.
        ("gpt-realtime", "voice"),
        ("gpt-realtime-2", "voice"),
        ("gpt-realtime-2.1", "voice"),
        ("gpt-realtime-2.1-mini", "voice"),
        ("gpt-realtime-whisper", "voice"),
        ("gpt-realtime-translate", "voice"),
        # Speech to text.
        ("whisper-1", "audio"),
        ("openai/whisper-1", "audio"),
        ("gpt-4o-transcribe", "audio"),
        ("gpt-4o-mini-transcribe", "audio"),
        # Text to speech.
        ("tts-1", "audio"),
        ("tts-1-hd", "audio"),
        ("gpt-4o-mini-tts", "audio"),
        # Image and video.
        ("dall-e-2", "image"),
        ("dall-e-3", "image"),
        ("gpt-image-1", "image"),
        ("grok-2-image-1212", "image"),
        ("sora-2", "video"),
    ],
)
def test_media_models_are_classified(model_id, expected):
    assert media_kind(model_id) == expected
    assert is_media_model(model_id) is True


# ---------------------------------------------------------------------------
# Text models — the anti-false-positive set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        # Plain chat models.
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.4-mini",
        "grok-4.6",
        "openai/gpt-4o",
        "anthropic/claude-fable-5.1",
        "z-ai/glm-5.3-flash",
        # THE TRAP: these name a modality the model can hear, see or emit — they
        # are still Chat Completions models. A substring rule on "audio" or
        # "vision" would hide every one of them.
        "gpt-4o-audio-preview",
        "gpt-4o-mini-audio-preview",
        "gpt-audio",
        "chatgpt-4o-latest",
        "gpt-4-vision-preview",
        "qwen2.5-vl-72b-instruct",
        "pixtral-large",
        "llava-v1.6",
        "llama-guard-3-8b",
        "speechless-llama2-hermes",
    ],
)
def test_text_models_are_kept(model_id):
    assert media_kind(model_id) is None
    assert is_media_model(model_id) is False


# ---------------------------------------------------------------------------
# The default direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id", ["some-future-model-9", "acme-llm-7b", "", "   ", None]
)
def test_unknown_and_empty_ids_are_treated_as_text(model_id):
    """An unrecognised id stays visible. Inverting this to an allow-list would
    empty the picker for OpenRouter, custom gateway, Ollama and vLLM rows."""
    assert media_kind(model_id) is None
    assert is_media_model(model_id) is False


def test_protected_tokens_are_not_hide_signals():
    """Guards the carve-out list itself: adding any of these to the segment map
    would silently hide a working text model, and the parametrized cases above
    would be the only thing to catch it."""
    for token in _NEVER_A_SIGNAL:
        assert token not in _MEDIA_SEGMENTS, (
            f"{token!r} must never classify a model as media"
        )


def test_segment_matching_not_substring():
    """``imagen`` contains ``image`` but is not the ``image`` segment."""
    assert media_kind("imagine-v2") == "image"  # exact segment
    assert media_kind("gpt-imagenet-tuned") is None  # substring only


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


def test_split_preserves_order_and_entry_shape():
    models = [
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "context_window": 400000},
        {"id": "whisper-1", "label": "Whisper"},
        {"id": "gpt-4o-audio-preview", "label": "Audio preview"},
        {"id": "dall-e-3", "label": "DALL-E 3"},
    ]

    text, media = split_media_models(models)

    assert [m["id"] for m in text] == ["gpt-5.6-sol", "gpt-4o-audio-preview"]
    assert [m["id"] for m in media] == ["whisper-1", "dall-e-3"]
    # Entries pass through untouched — the caller still needs their metadata.
    assert text[0]["context_window"] == 400000


def test_split_handles_empty_and_none():
    assert split_media_models([]) == ([], [])
    assert split_media_models(None) == ([], [])


def test_split_accepts_bare_string_ids():
    text, media = split_media_models(["gpt-5.6-sol", "tts-1"])

    assert text == ["gpt-5.6-sol"]
    assert media == ["tts-1"]


def test_every_media_kind_is_one_of_the_documented_buckets():
    """The frontend switches on these strings to name a destination."""
    assert set(_MEDIA_SEGMENTS.values()) <= {"voice", "audio", "image", "video"}


def test_app_audio_registry_ids_are_all_classified_as_media():
    """The ids this app already serves through its media panels must be exactly
    the ones the provider list hides — otherwise a model has no home."""
    from app.api.settings_audio import STT_MODELS, TTS_MODELS

    for model_id in list(TTS_MODELS) + list(STT_MODELS):
        if model_id.startswith("fal-ai/"):
            continue  # fal ids are never in a provider chat roster
        assert model_modality.is_media_model(model_id), model_id
