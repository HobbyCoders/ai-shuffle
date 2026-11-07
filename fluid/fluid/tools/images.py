"""Model-facing image conditioning — the resize seam for tool images.

Every image that enters the conversation through a tool result (builtin file
reads, MCP screenshots, computer use) passes through :func:`condition_image`
on its way from a ``ToolImage`` to an ``Image`` content block. Provider serializers
also apply it to persisted images, so older oversized history can resume without
rewriting or deleting its original content. Providers bill
images by PIXELS, not bytes — roughly width x height / 750 tokens on
Anthropic-class vision APIs — so a screenshot downscaled to fit inside the
billing-optimal long edge costs the same tokens as its multi-megabyte
original, and every kilobyte shaved off the base64 is pure transport waste.

The cap follows Anthropic's own guidance (~1568 px long edge): beyond it the
model does not resolve more detail, so sending more pixels only risks
provider-side downscale artifacts and oversized requests.

Pillow is a runtime dependency so headless workers also enforce this budget.
If an image cannot be decoded (or the installation is incomplete), the original
bytes pass through unchanged rather than failing a tool call.
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import threading
from collections import OrderedDict

# Longest edge sent to the model. Matches Claude Code's Read behavior and
# Anthropic's max-resolution vision tier (~1.15 megapixels / ~1600 tokens).
MAX_IMAGE_EDGE = 1568
# JPEG re-encode quality when transcoding.
JPEG_QUALITY = 80
# Re-encode an UNRESIZED image as JPEG when its raw bytes exceed this — a
# within-budget-but-inefficient PNG (rare) still should not ride every later
# request at 10x the necessary size.
_REENCODE_RAW_BYTES = 512_000

# A process-wide byte-weighted LRU, shared by ingest and every serializer. Store
# the result even when it is the original: an optimized PNG can cost seconds to
# encode without saving a byte. Digest the base64 spelling (not decoded bytes),
# since its length participates in the keep-original decision below.
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_cache: OrderedDict[tuple[str, bytes], tuple[tuple[str, str], int]] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _cache_entry_bytes(key: tuple[str, bytes], result: tuple[str, str]) -> int:
    # Include strings, digest and tuples, plus a conservative allowance for the
    # LRU node/accounting. Small images must not create unlimited tiny entries.
    return (sum(sys.getsizeof(item) for item in (*key, *result))
            + sys.getsizeof(key) + sys.getsizeof(result) + 256)


def _open_image(raw: bytes):
    from PIL import Image  # imported lazily for text-only calls

    return Image.open(io.BytesIO(raw))


def condition_image(media_type: str, data_b64: str) -> tuple[str, str]:
    """Memoize conditioning, including the kept-original outcome.

    Call from a worker thread on async paths: misses (including LRU evictions
    and entries bigger than the budget) may decode/encode. The lock makes an
    overlapping miss for the same image encode only once.
    """
    global _cache_bytes
    key = (media_type, hashlib.sha256(data_b64.encode("utf-8", errors="surrogatepass")).digest())
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key][0]
        result = _condition_image(media_type, data_b64)
        if result is None:
            # A missing decoder or transient encode failure must remain
            # retryable; only successful conditioning outcomes are memoized.
            return media_type, data_b64
        size = _cache_entry_bytes(key, result)
        if size <= _CACHE_MAX_BYTES:
            while _cache_bytes + size > _CACHE_MAX_BYTES:
                _, (_, evicted_size) = _cache.popitem(last=False)
                _cache_bytes -= evicted_size
            _cache[key] = (result, size)
            _cache_bytes += size
        return result


def _condition_image(media_type: str, data_b64: str) -> tuple[str, str] | None:
    """Downscale/re-encode one base64 image to the model-facing budget.

    Returns ``(media_type, base64)`` — possibly identical to the input when the
    image is already within budget (including when a re-encode is no smaller).
    A successful resize always wins over byte size. None means processing failed
    and the caller should pass through without memoizing the failure.
    """
    try:
        raw = base64.b64decode(data_b64, validate=False)
        with _open_image(raw) as src:
            width, height = src.size
            longest = max(width, height)
            resized = False
            if longest > MAX_IMAGE_EDGE:
                scale = MAX_IMAGE_EDGE / longest
                src = src.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    _resampling(),
                )
                resized = True
            if not resized and len(raw) <= _REENCODE_RAW_BYTES:
                return media_type, data_b64


            out_type = media_type
            if out_type in ("image/gif", "image/webp") or (
                len(raw) > _REENCODE_RAW_BYTES and out_type == "image/png"
                and width * height > 1_150_000
            ):
                # Animated/odd formats and big PNGs flatten to JPEG; UI text
                # keeps acceptable fidelity at quality 80.
                out_type = "image/jpeg"
            buffer = io.BytesIO()
            if out_type == "image/jpeg":
                if src.mode not in ("RGB", "L"):
                    src = src.convert("RGB")
                src.save(buffer, format="JPEG", quality=JPEG_QUALITY)
            else:
                src.save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        # Byte size is only an optimization AFTER the pixel budget is met.
        # Compressed line art can grow when resized; restoring that oversized
        # original poisons every later request, including session resumes.
        if not resized and len(encoded) >= len(data_b64):
            return media_type, data_b64
        return out_type, encoded
    except Exception:
        # No Pillow, undecodable bytes, or any encode failure: pass through.
        return None


def _resampling():
    from PIL import Image

    return Image.Resampling.LANCZOS
