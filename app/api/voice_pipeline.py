"""Voice pipeline: STT -> Claude -> TTS for realtime voice sessions.

Provides shared helper functions for transcribing audio and generating speech
that can be called from both REST endpoints and the WebSocket voice handler.
Reuses the fal.ai infrastructure from settings_audio.py.
"""

import base64
import logging
import uuid
from typing import Optional

import httpx

from app.api.settings_audio import (
    TTS_MODELS,
    _build_tts_payload,
    _extract_audio_url,
    get_generated_audio_dir,
)
from app.api.settings_crypto import get_decrypted_api_key
from app.db import database

logger = logging.getLogger(__name__)


async def transcribe_audio(audio_base64: str, mime_type: str = "audio/webm") -> str:
    """Transcribe base64-encoded audio to text using configured STT model.

    Args:
        audio_base64: Base64-encoded audio data (without data URI prefix).
        mime_type: MIME type of the audio data.

    Returns:
        Transcribed text string.

    Raises:
        ValueError: If fal.ai API key is not configured or transcription fails.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    if not fal_key:
        raise ValueError("fal.ai API key not configured")

    # Build data URI from base64
    audio_data_uri = f"data:{mime_type};base64,{audio_base64}"

    # Get configured STT model
    stt_model = database.get_system_setting("stt_model") or "fal-ai/whisper"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "audio_url": audio_data_uri,
                "task": "transcribe",
                "chunk_level": "segment",
            }

            response = await client.post(
                f"https://fal.run/{stt_model}",
                headers={
                    "Authorization": f"Key {fal_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code != 200:
                error_detail = response.text
                logger.error(f"fal.ai STT failed: {response.status_code} - {error_detail}")
                raise ValueError(f"Transcription failed: {error_detail}")

            result = response.json()
            transcribed_text = result.get("text", "").strip()

            if not transcribed_text:
                # Try extracting from chunks/segments
                chunks = result.get("chunks", [])
                if chunks:
                    transcribed_text = " ".join(c.get("text", "") for c in chunks).strip()

            return transcribed_text

    except httpx.TimeoutException:
        raise ValueError("Transcription timed out")
    except ValueError:
        raise
    except Exception:
        logger.exception("fal.ai transcription failed")
        raise ValueError("Transcription failed")


async def generate_speech(text: str) -> Optional[str]:
    """Generate TTS audio from text using configured TTS model.

    Args:
        text: Text to synthesize into speech.

    Returns:
        URL path to the generated audio file (e.g., /api/generated-audio/...),
        or None if text is empty or generation fails.
    """
    if not text or not text.strip():
        return None

    fal_key = get_decrypted_api_key("fal_api_key")
    if not fal_key:
        logger.warning("fal.ai API key not configured, skipping TTS")
        return None

    # Get configured TTS model and settings
    tts_model = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
    model_info = TTS_MODELS.get(tts_model)
    if not model_info:
        logger.error(f"Invalid TTS model: {tts_model}")
        return None

    voice = database.get_system_setting("tts_voice") or "af_heart"
    speed_str = database.get_system_setting("tts_speed")
    speed = float(speed_str) if speed_str else 1.0

    # Truncate text for TTS (max 4096 chars)
    text = text.strip()[:4096]

    # Build model-specific payload
    payload = _build_tts_payload(tts_model, text, voice, speed, model_info)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"https://fal.run/{tts_model}",
                headers={
                    "Authorization": f"Key {fal_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code != 200:
                error_detail = response.text
                logger.error(f"fal.ai TTS failed: {response.status_code} - {error_detail}")
                return None

            result = response.json()
            audio_url_from_fal = _extract_audio_url(result)

            if not audio_url_from_fal:
                logger.error(f"No audio URL in fal.ai response for {tts_model}: {result}")
                return None

            # Download the audio and save locally
            audio_response = await client.get(audio_url_from_fal, timeout=60.0)
            if audio_response.status_code != 200:
                logger.error("Failed to download generated audio from fal.ai")
                return None

            audio_data = audio_response.content
            audio_dir = get_generated_audio_dir()
            local_filename = f"voice-{uuid.uuid4().hex[:8]}.mp3"
            file_path = audio_dir / local_filename

            with open(file_path, "wb") as f:
                f.write(audio_data)

            encoded_path = str(file_path).replace("\\", "/")
            return f"/api/generated-audio/by-path?path={encoded_path}"

    except httpx.TimeoutException:
        logger.warning("TTS generation timed out")
        return None
    except Exception:
        logger.exception("fal.ai TTS generation failed")
        return None
