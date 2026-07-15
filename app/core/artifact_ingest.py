"""V2 Q5 — screenshot artifact ingest at the SDK-output pump.

The :class:`app.core.turn_recorder.TurnRecorder` taps every yielded pump event.
When a top-level ``tool_result`` carries a decoded screenshot data URI (the
Claude-in-Chrome / computer-use image results surfaced by
``extract_mcp_image``), the recorder content-addresses the bytes into the
artifact store (:mod:`app.core.artifact_store`) so they can be served
location-independently (``GET /api/v1/artifacts/{hash}``) from any install, and
stamps the hash onto the ``tool.result`` event + emits ``artifact.created``.

This module holds only the pure, synchronous helpers (data-URI decode + content
hash). The actual disk/DB store is a fire-and-forget task owned by the recorder,
failure-isolated so an artifact-store error never affects the streaming pump or
the turn. The whole seam is gated on ``settings.v2_placement`` (Phase-4 placement
cutover); OFF = byte-identical legacy behavior (no store, no ``artifact_hashes``,
no ``artifact.created``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Optional, Tuple

# ``data:<mime>;base64,<payload>`` — the exact shape produced by
# ``app.core.query_streaming.extract_mcp_image``. DOTALL so a payload that (very
# rarely) contains a newline still matches; a malformed URI simply misses.
_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$", re.DOTALL)

# Friendly download extensions for the common screenshot mimes; anything else
# gets no extension (filename stays the bare hash on the serving side).
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def parse_screenshot_data_uri(data_uri: object) -> Optional[Tuple[bytes, str]]:
    """Decode a ``data:<mime>;base64,<payload>`` URI into ``(bytes, mime)``.

    Returns ``None`` for anything that is not a well-formed base64 data URI (a
    non-string, a plain URL, an empty payload, or undecodable base64) so the
    ingest predicate cleanly misses. Never raises.
    """
    if not isinstance(data_uri, str):
        return None
    match = _DATA_URI_RE.match(data_uri)
    if not match:
        return None
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    return data, match.group("mime")


def content_hash(data: bytes) -> str:
    """sha256 hexdigest — the content address (matches ``artifact_store``)."""
    return hashlib.sha256(data).hexdigest()


def filename_for(digest: str, mime: str) -> str:
    """A stable, content-addressed download filename for a screenshot."""
    ext = _MIME_EXT.get((mime or "").lower())
    return f"{digest}.{ext}" if ext else digest
