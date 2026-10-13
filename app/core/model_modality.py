"""Model-id modality classification for the provider catalog.

The provider model list (Settings > Providers) is a TEXT-GENERATION picker: its
entries land in ``models_json`` and are offered as chat models by
``app.core.model_registry._providers_payload``. Audio, voice, image and video
endpoints are managed elsewhere — Settings > Default models for media models and
the realtime voice model — and listing them among chat models only
invites someone to pick one for a job it cannot do.

DENY-LIST BY DESIGN. An id this module does not recognise is TEXT and stays
visible. Never invert this into an allow-list: OpenRouter, custom gateway,
Ollama and vLLM rows carry arbitrary ids, and an allow-list would empty their
pickers entirely. Hiding a model the user needs is a bug they cannot work around
from the UI; showing one extra is a cosmetic annoyance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Media families, keyed on a hyphen/underscore-delimited SEGMENT of the id.
# Segment matching rather than substring is what keeps this safe — see
# _NEVER_A_SIGNAL below for the ids that would break under a substring rule.
_MEDIA_SEGMENTS: Dict[str, str] = {
    "realtime": "voice",      # gpt-realtime, gpt-realtime-2.1-mini, gpt-realtime-whisper
    "whisper": "audio",       # whisper-1
    "transcribe": "audio",    # gpt-4o-transcribe, gpt-4o-mini-transcribe
    "transcription": "audio",
    "tts": "audio",           # tts-1, tts-1-hd, gpt-4o-mini-tts
    "dall": "image",          # dall-e-2, dall-e-3 -> segments ("dall", "e", "3")
    "image": "image",         # gpt-image-1, grok-2-image-1212
    "imagine": "image",       # xAI Imagine; model_catalog_presets documents these as non-chat
    "sora": "video",
    "video": "video",
}

# Tokens that must NEVER become a hide signal, each with the model it protects.
# Adding any of these to _MEDIA_SEGMENTS would hide a working TEXT model:
# they name a modality the model can *read or emit*, not the kind of endpoint
# it is. A chat model that can hear is still a chat model.
_NEVER_A_SIGNAL: Tuple[str, ...] = (
    "audio",       # gpt-4o-audio-preview, gpt-audio - Chat Completions models
    "vision",      # gpt-4-vision-preview
    "vl",          # qwen2.5-vl-*
    "multimodal",
    "omni",        # chatgpt-4o-latest and friends
    "speech",      # speechless-llama2-hermes (an OpenRouter chat model)
    "guard",       # llama-guard-3-8b - a chat-servable classifier
    "translate",   # gpt-realtime-translate is already caught by "realtime"
    "voice",
)

_SPLIT = re.compile(r"[-_/]+")


def _segments(model_id: str) -> Tuple[str, ...]:
    """Lowercased id segments with the vendor prefix dropped, ``.`` left intact.

    Dropping the vendor prefix handles OpenRouter (``openai/gpt-4o``) and this
    app's own media ids (``openai/whisper-1``). Keeping ``.`` means
    ``gpt-realtime-2.1`` yields ``("gpt", "realtime", "2.1")`` — the family
    segment survives a dotted version suffix.
    """
    ident = (model_id or "").strip().lower()
    if "/" in ident:
        ident = ident.rsplit("/", 1)[-1]
    return tuple(part for part in _SPLIT.split(ident) if part)


def media_kind(model_id: Optional[str]) -> Optional[str]:
    """``"voice" | "audio" | "image" | "video"`` for a media model, else ``None``.

    ``None`` means "treat as text", including for an id this module has never
    seen. That default is load-bearing and pinned by tests.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    for segment in _segments(model_id):
        kind = _MEDIA_SEGMENTS.get(segment)
        if kind is not None:
            return kind
    return None


def is_media_model(model_id: Optional[str]) -> bool:
    """Whether ``model_id`` names a non-chat media endpoint."""
    return media_kind(model_id) is not None


def split_media_models(
    models: Optional[Sequence[Any]],
) -> Tuple[List[Any], List[Any]]:
    """Partition catalog entries into ``(text, media)``, preserving order.

    Entries keep their original shape — this only sorts them, so the caller can
    return the text half and still report what it held back.
    """
    text: List[Any] = []
    media: List[Any] = []
    for entry in models or []:
        model_id = entry.get("id") if isinstance(entry, dict) else entry
        if is_media_model(model_id):
            media.append(entry)
        else:
            text.append(entry)
    return text, media
