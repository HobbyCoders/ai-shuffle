"""Voice engines and their distinct transport/delegation contracts."""

LIVE_MODEL = "gpt-live-1"
# API successor of gpt-live-1: same Live socket, voices and options. Offered
# only when the selected OpenAI API key can see it (app.core.voice_availability).
LIVE_11_MODEL = "gpt-live-1.1"
LIVE_MODELS = (LIVE_MODEL, LIVE_11_MODEL)
# ChatGPT subscription path; explicit opt-in only. ChatGPT and Codex run
# GPT-Live-1.1 here since 2026-10-15, but the wire model id is unchanged.
CODEX_MODEL = "gpt-live-1-codex"
GROK_MODEL = "grok-voice-latest"
# SuperGrok subscription path over the same xAI realtime socket and upstream
# model. Explicit selection only; never funded by an xAI API key.
SUPERGROK_MODEL = "grok-voice-supergrok"
SUPERGROK_LABEL = "Grok voice (SuperGrok subscription)"
GROK_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
# Built-ins shared by xAI speech-to-speech and TTS, checked 2026-10-18.
GROK_VOICES = ("eve", "carina", "zagan", "helix", "orion", "luna", "iris", "altair", "zenith", "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste", "ursa", "sirius", "lumen", "castor", "naksh", "atlas", "aurora", "liora", "ara", "leo", "rex", "sal")
PERSONAPLEX_MODEL = "fal-ai/personaplex/realtime"
FAL_PROVIDER_ID = "__fal_voice__"
REALTIME_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
LIVE_VOICES = REALTIME_VOICES + ("quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo", "beacon", "delta", "cinder", "nuri", "juni", "flitz", "brise", "sillage", "nira", "harema", "shida", "noeul")
FAL_VOICES = tuple(f"{prefix}{i}" for prefix, count in (("NATF", 4), ("NATM", 4), ("VARF", 5), ("VARM", 5)) for i in range(count))
# ChatGPT subscription voices: openai/codex codex-rs/protocol/src/protocol.rs,
# RealtimeVoicesList::builtin().v3 (the gpt-live-1-codex list), checked
# 2026-10-23. Wire names are lowercase; cove is the Codex default.
CODEX_VOICES = ("juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove", "aube", "bubbie", "haetsal", "hanul", "himari", "leher", "miko", "neer", "porto", "rindo", "rio", "rivage", "selva", "sonna", "tinta", "viola")

# Engines a new call may choose, in Settings order. ``funding`` is the
# provider kind that pays for it (see app.api.voice.provider_matches_model);
# ``gated`` engines appear only when app.core.voice_availability says so.
# Add an engine here and in voices_for/transport_for/validate_options.
ENGINES = (
    {"id": LIVE_MODEL, "label": "GPT Live 1", "funding": "openai", "default_voice": "marin"},
    {"id": LIVE_11_MODEL, "label": "GPT-Live-1.1", "funding": "openai", "default_voice": "marin", "gated": True},
    {"id": GROK_MODEL, "label": "Grok Voice", "funding": "xai", "default_voice": "eve"},
    {"id": SUPERGROK_MODEL, "label": SUPERGROK_LABEL, "funding": "grok", "default_voice": "eve"},
    {"id": CODEX_MODEL, "label": "GPT-Live-1.1 · ChatGPT subscription", "funding": "chatgpt", "default_voice": "cove"},
)
ENGINE_IDS = tuple(engine["id"] for engine in ENGINES)
_ENGINE_BY_ID = {engine["id"]: engine for engine in ENGINES}


def engine_label(model: str) -> str:
    return _ENGINE_BY_ID.get(model, {}).get("label", model)


def default_voice_for(model: str) -> str:
    """The voice a call uses when the stored one is not valid for ``model``."""
    engine = _ENGINE_BY_ID.get(model)
    if engine:
        return engine["default_voice"]
    voices = voices_for(model)
    return "marin" if "marin" in voices else voices[0]


def voices_for(model: str) -> tuple[str, ...]:
    if model == CODEX_MODEL:
        return CODEX_VOICES
    if model in (GROK_MODEL, SUPERGROK_MODEL):
        return GROK_VOICES
    if model == PERSONAPLEX_MODEL:
        return FAL_VOICES
    return LIVE_VOICES if model in LIVE_MODELS else REALTIME_VOICES


def transport_for(model: str) -> str:
    return "stream" if model in (*LIVE_MODELS, GROK_MODEL, SUPERGROK_MODEL, CODEX_MODEL, PERSONAPLEX_MODEL) else "webrtc"


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
    if model in (*LIVE_MODELS, GROK_MODEL, SUPERGROK_MODEL, CODEX_MODEL):
        prompts = [
            {"label": "Voice conversation prompt", "key": "instructions", "text": options["instructions"] or LIVE_INSTRUCTIONS, "default_text": LIVE_INSTRUCTIONS},
            {"label": "Chat agent prompt", "key": "delegation_instructions", "text": options["delegation_instructions"] or DELEGATION_INSTRUCTIONS, "default_text": DELEGATION_INSTRUCTIONS},
        ]
        runtime = [
            {"label": "Audio format", "value": "PCM16 · 24 kHz · mono"},
            {"label": "Reasoning and tools", "value": "Uses the model, effort and permissions selected in the chat composer"},
            {"label": "Opening words", "value": options["opening"] or "Wait for caller"},
            {"label": "Turn detection", "value": "server_vad" if model in (GROK_MODEL, SUPERGROK_MODEL) else "Managed by GPT Live; no VAD override is sent"},
            {"label": "Microphone gate", "value": f"Audio quieter than {options['mic_threshold_db']:g} dBFS is sent as silence"},
        ]
        note = ("Task requests run in the existing chat session with its selected agent instructions, conversation, tools and approval rules. "
                "The voice conversation prompt leads every call; calls on a chat then add the call's work rules and the start of the chat agent's "
                "own instructions, for persona and tone only. The chat agent prompt rides each spoken request's message.")
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
            # A call on a chat does not use the standalone voice agent prompt:
            # it speaks under the shared call contract, fixed for this engine.
            {"label": "Voice conversation prompt (calls on a chat)", "text": LIVE_INSTRUCTIONS},
            {"label": "Chat agent prompt (calls on a chat)", "text": DELEGATION_INSTRUCTIONS},
            {"label": "App configuration tools (owner calls)", "text": APP_TOOLS_INSTRUCTIONS},
            {"label": "Browser call greeting", "text": GREETING_PROMPT},
            *[{"label": f"Phone mode appendix: {mode}", "text": text} for mode, text in DEFAULT_MODE_INSTRUCTIONS.items()],
        ]
        runtime = [
            {"label": "Transport", "value": "WebRTC audio with server-side tools"},
            {"label": "Input transcription", "value": options["transcription_model"]},
            {"label": "Max output tokens", "value": str(options["max_output_tokens"])},
            *[{"label": f"Turn detection · {key}", "value": str(value).lower()} for key, value in realtime_session_options(options)["audio"]["input"]["turn_detection"].items()],
            {"label": "Input noise reduction", "value": options["noise_reduction"].replace("_", " ")},
            *[{"label": f"Context retention · {key}", "value": str(value)} for key, value in TRUNCATION_CONFIG.items()],
        ]
        note = ("Calls on a voice session append the available agent roster and any client-supplied device instructions. "
                "Calls on a chat use the voice conversation prompt first, then the call's work rules and the start of the chat agent's own "
                "instructions, for persona and tone only. Phone appendices apply only in the named mode; app-tool instructions apply "
                "only when those tools are admitted. Conversation replay is context, not a system prompt.")
    available, unavailable_reason = True, None
    if model == CODEX_MODEL:
        from app.core.voice_dependencies import availability
        available, unavailable_reason = availability()
    return {
        "model": model,
        "available": available,
        "unavailable_reason": unavailable_reason,
        "label": engine_label(model),
        "voices": [{"id": voice, "label": voice if model == PERSONAPLEX_MODEL else voice.capitalize()} for voice in voices_for(model)],
        "transport": transport_for(model),
        "runtime_settings": runtime,
        "transcription_models": list(get_args(RealtimeOptions.model_fields["transcription_model"].annotation)) if transport_for(model) == "webrtc" else [],
        "prompts": prompts,
        "prompt_note": note,
        # Preview generation uses the same Live transport and voice as new calls.
        "preview_note": ("Uses your ChatGPT subscription login. Previews aren't available for subscription voice."
                         + (" " + unavailable_reason if unavailable_reason else "")
                         if model == CODEX_MODEL
                         else "Calls use your connected SuperGrok login. Previews are not available for this engine."
                         if model == SUPERGROK_MODEL
                         else "Previews use the selected engine and voice with your API key."),
    }


def session_config(row: dict, settings: dict) -> dict:
    """The binding of the call in progress; current settings when there is none.

    Each call start (POST /sessions, /attach, or /secret with no call running)
    stores the current Settings → Voice choices as the binding, so everything
    read during the call (audio socket, sideband, reconnects) stays on the
    engine and voice the call began with even if Settings change mid-call.
    """
    import json
    config = row.get("voice_config")
    if isinstance(config, str):
        config = json.loads(config)
    if config:
        return config
    return {
        "model": settings["voice_model"],
        "voice": settings["voice_voice"],
        "provider_id": row.get("provider_id") or settings.get("voice_provider_id"),
        "options": settings.get("voice_options", {}),
    }
