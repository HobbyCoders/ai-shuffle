"""
API endpoint for serving generated audio.

This endpoint serves audio created by the AI audio-generation tools
(generateAudio, audioToAudio) and the text-to-speech feature, allowing
them to be played inline in the chat UI.

Audio is stored in 'generated-audio' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.

This module is a thin shim over ``app.api.media_serve``; the shared serve
logic lives there. The symbols below (``settings``, ``is_path_safe``,
``ALLOWED_EXTENSIONS``, ``MEDIA_TYPES``, ``router`` and the named handlers)
are preserved for backwards compatibility with importers and tests.
"""

from app.core.config import settings  # noqa: F401  (re-exported; tests patch this)
from app.api.common import is_path_safe  # noqa: F401  (re-exported for tests)
from app.api.media_serve import MediaConfig, build_media_module

# Allowed audio extensions
ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.m4a', '.aac', '.pcm'}

# Media type mapping
MEDIA_TYPES = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg',
    '.opus': 'audio/opus',
    '.flac': 'audio/flac',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.pcm': 'audio/L16',
}

_config = MediaConfig(
    prefix="/api/generated-audio",
    tag="generated-audio",
    directory="generated-audio",
    allowed_extensions=ALLOWED_EXTENSIONS,
    media_types=MEDIA_TYPES,
    default_media_type='audio/mpeg',
    list_key="audio",
    noun="Audio",
    noun_lower="audio file",
)

# Read this module's own ``settings`` so tests patching
# ``app.api.generated_audio.settings`` take effect.
_module = build_media_module(_config, lambda: settings.effective_workspace_dir)

router = _module["router"]
get_audio_by_path = _module["by_path"]
get_generated_audio = _module["by_filename"]
list_generated_audio = _module["list_media"]
