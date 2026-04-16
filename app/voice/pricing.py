"""
Realtime voice — cost pricing table.

Used by the voice session to log per-call usage to the existing
usage_log infrastructure. Prices updated 2026-Q1; keep in sync with
provider billing pages.
"""

from __future__ import annotations

# STT — dollars per audio minute
STT_PRICE_PER_MIN: dict[str, float] = {
    "deepgram-nova-2": 0.0043,
    "deepgram-nova-general": 0.0043,
    "groq-whisper-large-v3-turbo": 0.0004,
    "groq-whisper-large-v3": 0.0011,
    "fal-whisper": 0.005,
}

# TTS — dollars per 1,000 characters output
TTS_PRICE_PER_1K_CHARS: dict[str, float] = {
    "cartesia-sonic-english": 0.042,
    "cartesia-sonic": 0.042,
    "elevenlabs-turbo-v2_5": 0.18,
    "elevenlabs-multilingual-v2": 0.30,
    "deepgram-aura": 0.015,
}


def stt_cost_usd(provider: str, model: str, seconds: float) -> float:
    """Cost estimate for an STT call (streaming or batch)."""
    key = f"{provider}-{model}".lower()
    rate = STT_PRICE_PER_MIN.get(key, 0.0)
    return round(rate * (seconds / 60.0), 6)


def tts_cost_usd(provider: str, model: str, chars: int) -> float:
    """Cost estimate for a TTS call."""
    key = f"{provider}-{model}".lower()
    rate = TTS_PRICE_PER_1K_CHARS.get(key, 0.0)
    return round(rate * (chars / 1000.0), 6)


def describe_stt(provider: str, model: str) -> str | None:
    """Human-readable pricing blurb for UI; None if unknown."""
    key = f"{provider}-{model}".lower()
    rate = STT_PRICE_PER_MIN.get(key)
    if rate is None:
        return None
    return f"${rate:.4f}/min"


def describe_tts(provider: str, model: str) -> str | None:
    key = f"{provider}-{model}".lower()
    rate = TTS_PRICE_PER_1K_CHARS.get(key)
    if rate is None:
        return None
    return f"${rate:.3f}/1K chars"
