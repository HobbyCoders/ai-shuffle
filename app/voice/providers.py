"""
Realtime voice — provider resolution.

Reads `system_settings` to pick STT/TTS providers and instantiates the
corresponding Pipecat services. Each helper raises a structured
VoiceProviderError if the required API key is missing so the voice
endpoint can return an actionable HTTP 400.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.db import database
from app.api.settings_crypto import get_decrypted_api_key

logger = logging.getLogger(__name__)


# Settings keys (system_settings table)
STT_PROVIDER_KEY = "voice_stt_provider"
TTS_PROVIDER_KEY = "voice_tts_provider"
TTS_VOICE_ID_KEY = "voice_tts_voice_id"
TURN_DETECTOR_KEY = "voice_turn_detector"  # "vad" | "semantic"
LLM_MODEL_KEY = "voice_llm_model"
INTERRUPTIONS_KEY = "voice_interruptions_enabled"
MAX_DURATION_KEY = "voice_max_duration_seconds"
USE_AGENT_SDK_KEY = "voice_use_agent_sdk"  # "true" → route through ClaudeAgentAdapter

# Defaults
DEFAULT_STT_PROVIDER = "deepgram"
DEFAULT_TTS_PROVIDER = "cartesia"
DEFAULT_TURN_DETECTOR = "vad"
DEFAULT_LLM_MODEL = "claude-sonnet-4-5"
DEFAULT_CARTESIA_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Barbershop Man
DEFAULT_ELEVENLABS_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_MAX_DURATION = 900  # 15 minutes


class VoiceProviderError(RuntimeError):
    """Raised when a required provider API key or dependency is missing."""

    def __init__(self, provider: str, message: str):
        super().__init__(message)
        self.provider = provider
        self.message = message


@dataclass
class VoiceConfig:
    """Resolved voice configuration for a single session."""
    stt_provider: str
    tts_provider: str
    tts_voice_id: str
    turn_detector: str
    llm_model: str
    interruptions_enabled: bool
    max_duration_seconds: int
    stt_api_key: Optional[str]
    tts_api_key: Optional[str]
    llm_api_key: Optional[str]
    use_agent_sdk: bool = False  # Phase 2: route LLM via ClaudeAgentAdapter


def load_voice_config() -> VoiceConfig:
    """Load and resolve the current voice configuration from system_settings."""
    stt_provider = (database.get_system_setting(STT_PROVIDER_KEY) or DEFAULT_STT_PROVIDER).lower()
    tts_provider = (database.get_system_setting(TTS_PROVIDER_KEY) or DEFAULT_TTS_PROVIDER).lower()
    tts_voice_id = database.get_system_setting(TTS_VOICE_ID_KEY) or _default_voice_for(tts_provider)
    turn_detector = (database.get_system_setting(TURN_DETECTOR_KEY) or DEFAULT_TURN_DETECTOR).lower()
    llm_model = database.get_system_setting(LLM_MODEL_KEY) or DEFAULT_LLM_MODEL

    interruptions_raw = database.get_system_setting(INTERRUPTIONS_KEY)
    interruptions_enabled = interruptions_raw != "false"  # default on

    max_duration_raw = database.get_system_setting(MAX_DURATION_KEY)
    try:
        max_duration_seconds = int(max_duration_raw) if max_duration_raw else DEFAULT_MAX_DURATION
    except (TypeError, ValueError):
        max_duration_seconds = DEFAULT_MAX_DURATION

    stt_api_key = _stt_api_key(stt_provider)
    tts_api_key = _tts_api_key(tts_provider)
    llm_api_key = _llm_api_key()

    use_agent_sdk_raw = database.get_system_setting(USE_AGENT_SDK_KEY)
    use_agent_sdk = use_agent_sdk_raw == "true"

    return VoiceConfig(
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        tts_voice_id=tts_voice_id,
        turn_detector=turn_detector,
        llm_model=llm_model,
        interruptions_enabled=interruptions_enabled,
        max_duration_seconds=max_duration_seconds,
        stt_api_key=stt_api_key,
        tts_api_key=tts_api_key,
        llm_api_key=llm_api_key,
        use_agent_sdk=use_agent_sdk,
    )


def _default_voice_for(provider: str) -> str:
    if provider == "elevenlabs":
        return DEFAULT_ELEVENLABS_VOICE
    return DEFAULT_CARTESIA_VOICE


def _stt_api_key(provider: str) -> Optional[str]:
    if provider == "deepgram":
        return get_decrypted_api_key("deepgram_api_key")
    if provider == "groq":
        return get_decrypted_api_key("groq_api_key")
    if provider == "fal":
        return get_decrypted_api_key("fal_api_key")
    return None


def _tts_api_key(provider: str) -> Optional[str]:
    if provider == "cartesia":
        return get_decrypted_api_key("cartesia_api_key")
    if provider == "elevenlabs":
        return get_decrypted_api_key("elevenlabs_api_key")
    return None


def _llm_api_key() -> Optional[str]:
    # Reuse existing Anthropic resolution — env var or DB setting
    import os
    return (
        get_decrypted_api_key("anthropic_api_key")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


# ---------------------------------------------------------------------------
# Service factories (Pipecat imports deferred — optional dependency)
# ---------------------------------------------------------------------------

def build_stt_service(cfg: VoiceConfig) -> Any:
    """Instantiate a Pipecat STT service based on cfg.stt_provider."""
    if not cfg.stt_api_key:
        raise VoiceProviderError(
            cfg.stt_provider,
            f"STT provider '{cfg.stt_provider}' requires an API key. "
            f"Configure it in Settings → Integrations.",
        )

    if cfg.stt_provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.transcriptions.language import Language
        return DeepgramSTTService(
            api_key=cfg.stt_api_key,
            live_options={
                "model": "nova-2-general",
                "language": Language.EN,
                "smart_format": True,
                "interim_results": True,
                "endpointing": 300,
            },
        )

    if cfg.stt_provider == "groq":
        from pipecat.services.groq.stt import GroqSTTService
        return GroqSTTService(
            api_key=cfg.stt_api_key,
            model="whisper-large-v3-turbo",
        )

    raise VoiceProviderError(
        cfg.stt_provider, f"Unsupported STT provider: {cfg.stt_provider}"
    )


def build_tts_service(cfg: VoiceConfig) -> Any:
    """Instantiate a Pipecat TTS service based on cfg.tts_provider."""
    if not cfg.tts_api_key:
        raise VoiceProviderError(
            cfg.tts_provider,
            f"TTS provider '{cfg.tts_provider}' requires an API key. "
            f"Configure it in Settings → Integrations.",
        )

    if cfg.tts_provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService
        return CartesiaTTSService(
            api_key=cfg.tts_api_key,
            voice_id=cfg.tts_voice_id,
            model="sonic-english",
        )

    if cfg.tts_provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
        return ElevenLabsTTSService(
            api_key=cfg.tts_api_key,
            voice_id=cfg.tts_voice_id,
            model="eleven_turbo_v2_5",
        )

    raise VoiceProviderError(
        cfg.tts_provider, f"Unsupported TTS provider: {cfg.tts_provider}"
    )


def build_llm_service(
    cfg: VoiceConfig,
    session_options: Optional[Any] = None,
    tool_observer: Optional[Any] = None,
) -> Any:
    """
    Instantiate a Pipecat LLM service.

    Phase 1 (use_agent_sdk=False): direct Anthropic via Pipecat built-in.
    Phase 2 (use_agent_sdk=True): bridges to the Claude Agent SDK, inheriting
        tools / MCP / worktree. Requires session_options.
    """
    if cfg.use_agent_sdk:
        if session_options is None:
            raise VoiceProviderError(
                "claude-agent-sdk",
                "use_agent_sdk=True requires session_options (profile / cwd / tools).",
            )
        from app.voice.claude_llm import ClaudeAgentLLMService
        return ClaudeAgentLLMService(
            session_options=session_options,
            tool_observer=tool_observer,
        )

    if not cfg.llm_api_key:
        raise VoiceProviderError(
            "anthropic",
            "Anthropic API key not found. Set ANTHROPIC_API_KEY or configure in Settings.",
        )
    from pipecat.services.anthropic.llm import AnthropicLLMService
    return AnthropicLLMService(
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
    )


def build_vad_analyzer(cfg: VoiceConfig) -> Any:
    """Silero VAD — always-on default."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    return SileroVADAnalyzer()


def build_turn_analyzer(cfg: VoiceConfig) -> Optional[Any]:
    """Semantic end-of-turn detection (Phase 3 opt-in)."""
    if cfg.turn_detector != "semantic":
        return None
    try:
        from pipecat.audio.turn.smart_turn.local_smart_turn_v2 import (
            LocalSmartTurnAnalyzerV2,
        )
        return LocalSmartTurnAnalyzerV2()
    except ImportError:
        logger.warning("SmartTurnAnalyzer not available; falling back to VAD-only")
        return None
