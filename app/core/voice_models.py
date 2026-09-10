"""Voice engines and their distinct transport/delegation contracts."""

LIVE_MODEL = "gpt-live-1"
PERSONAPLEX_MODEL = "fal-ai/personaplex/realtime"
FAL_PROVIDER_ID = "__fal_voice__"
VOICE_MODELS = (LIVE_MODEL, "gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime", PERSONAPLEX_MODEL)
REALTIME_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
LIVE_VOICES = REALTIME_VOICES + ("quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo", "beacon", "delta", "cinder")
FAL_VOICES = tuple(f"{prefix}{i}" for prefix, count in (("NATF", 4), ("NATM", 4), ("VARF", 5), ("VARM", 5)) for i in range(count))


def voices_for(model: str) -> tuple[str, ...]:
    if model == PERSONAPLEX_MODEL:
        return FAL_VOICES
    return LIVE_VOICES if model == LIVE_MODEL else REALTIME_VOICES


def transport_for(model: str) -> str:
    return "stream" if model in (LIVE_MODEL, PERSONAPLEX_MODEL) else "webrtc"


def session_config(row: dict, settings: dict) -> dict:
    """Old sessions use current defaults; new calls keep their creation binding."""
    import json
    config = row.get("voice_config")
    if isinstance(config, str):
        config = json.loads(config)
    return config or {
        "model": settings["voice_model"],
        "voice": settings["voice_voice"],
        "provider_id": row.get("provider_id") or settings.get("voice_provider_id"),
    }
