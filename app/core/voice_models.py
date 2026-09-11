"""Voice engines and their distinct transport/delegation contracts."""

LIVE_MODEL = "gpt-live-1"
PERSONAPLEX_MODEL = "fal-ai/personaplex/realtime"
FAL_PROVIDER_ID = "__fal_voice__"
VOICE_MODELS = (LIVE_MODEL, "gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2", "gpt-realtime", PERSONAPLEX_MODEL)
REALTIME_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
LIVE_VOICES = REALTIME_VOICES + ("quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo", "beacon", "delta", "cinder")
FAL_VOICES = tuple(f"{prefix}{i}" for prefix, count in (("NATF", 4), ("NATM", 4), ("VARF", 5), ("VARM", 5)) for i in range(count))


def voices_for(model: str) -> tuple[str, ...]:
    if model == PERSONAPLEX_MODEL:
        return FAL_VOICES
    return LIVE_VOICES if model == LIVE_MODEL else REALTIME_VOICES


def transport_for(model: str) -> str:
    return "stream" if model in (LIVE_MODEL, PERSONAPLEX_MODEL) else "webrtc"


def model_details(model: str, options: dict | None = None) -> dict:
    """Describe implemented controls, not a claim of upstream account availability.

    Prompts and fixed protocol settings come from the producing paths. Never
    substitute TTS voices or invent knobs that the transport does not send.
    """
    from app.core.voice_prompts import LIVE_INSTRUCTIONS, DELEGATION_INSTRUCTIONS, PERSONAPLEX_INSTRUCTIONS

    from typing import get_args
    from app.core.voice_options import RealtimeOptions, validate_options, realtime_session_options
    options = validate_options(model, options or {})
    prompts = []
    runtime = []
    if model == LIVE_MODEL:
        prompts = [
            {"label": "Voice conversation prompt", "key": "instructions", "text": options["instructions"] or LIVE_INSTRUCTIONS, "default_text": LIVE_INSTRUCTIONS},
            {"label": "Chat agent prompt", "key": "delegation_instructions", "text": options["delegation_instructions"] or DELEGATION_INSTRUCTIONS, "default_text": DELEGATION_INSTRUCTIONS},
        ]
        runtime = [
            {"label": "Audio format", "value": "PCM16 · 24 kHz · mono"},
            {"label": "Reasoning and tools", "value": "Uses the model, effort and permissions selected in the chat composer"},
            {"label": "Opening words", "value": options["opening"] or "Wait for caller"},
            {"label": "Turn detection", "value": "Managed by GPT Live; no VAD override is sent"},
        ]
        note = "Task requests run in the existing chat session with its selected agent instructions, conversation, tools and approval rules. The voice prompt controls direct replies and when to ask the chat agent for help."
    elif model == PERSONAPLEX_MODEL:
        prompts = [{"label": "Voice agent", "text": options["instructions"] or PERSONAPLEX_INSTRUCTIONS}]
        runtime = [
            {"label": "Audio format", "value": "PCM16 · 24 kHz · mono"},
            {"label": "Conversation", "value": "Full-duplex audio"},
            {"label": "Tools and delegation", "value": "Not connected in this integration"},
            {"label": "Transcripts", "value": "Assistant text only; user transcription is not provided"},
        ]
        note = "This integration sends only the voice prompt; no agent roster, app tools or phone-mode appendix is attached."
    else:
        from app.core.voice_bridge import (
            VOICE_INSTRUCTIONS, APP_TOOLS_INSTRUCTIONS, DEFAULT_MODE_INSTRUCTIONS,
            GREETING_PROMPT, TRUNCATION_CONFIG,
        )
        prompts = [
            {"label": "Voice agent", "text": options["instructions"] or VOICE_INSTRUCTIONS},
            {"label": "App configuration tools (owner calls)", "text": APP_TOOLS_INSTRUCTIONS},
            {"label": "Browser call greeting", "text": GREETING_PROMPT},
            *[{"label": f"Phone mode appendix: {mode}", "text": text} for mode, text in DEFAULT_MODE_INSTRUCTIONS.items()],
        ]
        runtime = [
            {"label": "Transport", "value": "WebRTC audio with server-side tools"},
            {"label": "Input transcription", "value": options["transcription_model"]},
            {"label": "Max output tokens", "value": str(options["max_output_tokens"])},
            *[{"label": f"Turn detection · {key}", "value": str(value).lower()} for key, value in realtime_session_options(options)["audio"]["input"]["turn_detection"].items()],
            *[{"label": f"Context retention · {key}", "value": str(value)} for key, value in TRUNCATION_CONFIG.items()],
        ]
        note = "Calls append the available agent roster and any client-supplied device instructions. Phone appendices apply only in the named mode; app-tool instructions apply only when those tools are admitted. Conversation replay is context, not a system prompt."
    return {
        "model": model,
        "voices": [{"id": voice, "label": voice if model == PERSONAPLEX_MODEL else voice.capitalize()} for voice in voices_for(model)],
        "transport": transport_for(model),
        "runtime_settings": runtime,
        "transcription_models": list(get_args(RealtimeOptions.model_fields["transcription_model"].annotation)) if transport_for(model) == "webrtc" else [],
        "prompts": prompts,
        "prompt_note": note,
        # Preview generation uses the same Live transport and voice as new calls.
        "preview_note": "Previews use GPT Live with the selected voice. Generating a sample uses your OpenAI API account.",
    }


def session_config(row: dict, settings: dict) -> dict:
    """Old sessions use current defaults; new calls keep their creation binding."""
    import json
    config = row.get("voice_config")
    if isinstance(config, str):
        config = json.loads(config)
    # A pinned legacy binding must NOT acquire today's newly changed defaults.
    if config:
        return config
    return {
        "model": settings["voice_model"],
        "voice": settings["voice_voice"],
        "provider_id": row.get("provider_id") or settings.get("voice_provider_id"),
        "options": settings.get("voice_options", {}),
    }
