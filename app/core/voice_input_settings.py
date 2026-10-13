"""Model selection for composer dictation and global voice input."""
from app.db import database

DEFAULT_VOICE_INPUT_MODEL = "fal-ai/whisper"
VOICE_INPUT_MODEL_KEY = "voice_input_model"


def get_voice_input_model() -> str:
    """Keep the old audio-to-text choice once, then use an independent setting.

    The atomic seed cannot overwrite a model saved concurrently by another client.
    The audio-to-text setter also calls this before changing its legacy setting.
    """
    return database.get_or_create_system_setting(
        VOICE_INPUT_MODEL_KEY,
        lambda: database.get_system_setting("stt_model") or DEFAULT_VOICE_INPUT_MODEL,
    )
