"""
API endpoint for serving generated videos.

This endpoint serves videos created by the AI video generation tool,
allowing them to be displayed in the chat UI.

Videos are stored in 'generated-videos' subdirectories within project
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

# Allowed video extensions
ALLOWED_EXTENSIONS = {'.mp4', '.webm', '.mov', '.avi'}

# Media type mapping
MEDIA_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
}

_config = MediaConfig(
    prefix="/api/generated-videos",
    tag="generated-videos",
    directory="generated-videos",
    allowed_extensions=ALLOWED_EXTENSIONS,
    media_types=MEDIA_TYPES,
    default_media_type='video/mp4',
    list_key="videos",
    noun="Video",
    noun_lower="video",
)

# Read this module's own ``settings`` so tests patching
# ``app.api.generated_videos.settings`` take effect.
_module = build_media_module(_config, lambda: settings.effective_workspace_dir)

router = _module["router"]
get_video_by_path = _module["by_path"]
get_generated_video = _module["by_filename"]
list_generated_videos = _module["list_media"]
