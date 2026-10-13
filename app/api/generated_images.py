"""
API endpoint for serving generated images.

This endpoint serves images created by the AI image generation tool,
allowing them to be displayed in the chat UI without base64 encoding
in the response (which would exceed context limits).

Images are stored in 'generated-images' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.

This module is a thin shim over ``app.api.media_serve``; the shared serve
logic lives there. The symbols below (``settings``, ``is_path_safe``,
``ALLOWED_EXTENSIONS``, ``MEDIA_TYPES`` and ``router``) are preserved
for backwards compatibility with importers and tests.
"""

from app.core.config import settings  # noqa: F401  (re-exported; tests patch this)
from app.api.common import is_path_safe  # noqa: F401  (re-exported for tests)
from app.api.media_serve import MediaConfig, build_media_module

# Allowed image extensions
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

# Media type mapping
MEDIA_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}

_config = MediaConfig(
    prefix="/api/generated-images",
    tag="generated-images",
    directory="generated-images",
    allowed_extensions=ALLOWED_EXTENSIONS,
    media_types=MEDIA_TYPES,
    default_media_type='application/octet-stream',
    list_key="images",
    noun="Image",
    noun_lower="image",
)

# Read this module's own ``settings`` so tests patching
# ``app.api.generated_images.settings`` take effect.
_module = build_media_module(_config, lambda: settings.effective_workspace_dir)

router = _module["router"]
