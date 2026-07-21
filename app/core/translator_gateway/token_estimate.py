"""
Token estimator for ``POST /v1/messages/count_tokens`` (§A.3 / §D5).

Claude Code preflights a token count before every request; if that count is wildly
wrong it misroutes auto-compaction and the prompt-too-long preflight (the LiteLLM
#30043 failure class). Code/JSON tokenizes far hotter than the classic ~4 chars/token
rule-of-thumb, so a heuristic under-count let real context overflow the upstream
window before compaction fired — a real "Prompt is too long" production failure.

We therefore count with a real BPE tokenizer (tiktoken ``o200k_base``, the encoding
used by current GPT/codex models) when it is available, and fall back to the
~4-chars/token heuristic on ANY failure. The fallback matters because tiktoken
fetches its BPE vocabulary file on first use, and that fetch can fail in an offline
container (no network, cold cache); the failure is cached so we do not re-attempt it
on every call. The heuristic path is otherwise identical to the pre-change behavior.

``image`` blocks are NOT counted as text: their base64 payload is not language and
tokenizing it inflates the estimate by hundreds of thousands of tokens (a 1MB
screenshot would trip the oversize guard on its own). Instead we bill each image
with OpenAI's tile math (85 + 170×tiles for high/auto detail), deriving pixel
dimensions from a dependency-free header parse of the first bytes of the base64
(PNG/JPEG/GIF/WEBP). On any parse failure — or a URL-source image with no bytes to
measure — we fall back to a conservative flat ``_IMAGE_TOKENS_FALLBACK`` (1105 =
85 + 170×6, a typical full-screen screenshot).

Guarantees:
  * DETERMINISTIC — same request body → same count, always (both paths); the header
    parse and tile math are pure integer arithmetic over the request bytes.
  * MONOTONIC — adding any text/message/tool/image never decreases the count (image
    cost is always a positive constant ≥ 85).

No mandatory I/O or network on the hot path: the encoder is initialized lazily and
cached (including a cached failure), and counting itself never touches the network.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any, Dict, List, Optional, Tuple

# ── Estimation constants (documented; tune, don't randomize) ─────────────────
_CHARS_PER_TOKEN = 4          # classic BPE rule-of-thumb (heuristic fallback)
_BASE_OVERHEAD = 3            # fixed request framing (BOS / role scaffolding)
_PER_MESSAGE_OVERHEAD = 4     # per message: role + delimiters
_PER_TOOL_OVERHEAD = 8        # per tool definition: schema framing
_PER_SYSTEM_OVERHEAD = 3      # system prompt framing when present

# ── Image tile-cost constants (OpenAI vision billing) ────────────────────────
_IMAGE_BASE_TOKENS = 85       # flat per-image base (also the whole cost at detail:"low")
_IMAGE_TILE_TOKENS = 170      # per 512px tile at high/auto detail
_IMAGE_MAX_SQUARE = 2048      # step 1: fit within a 2048×2048 box (downscale only)
_IMAGE_TARGET_SHORT = 768     # step 2: scale the shortest side to 768px (downscale only)
_IMAGE_TILE_PX = 512          # step 3: tile size
# Conservative flat cost when dimensions are unknown (URL source or a header we
# cannot parse): 85 + 170×6, matching a typical full-screen screenshot.
_IMAGE_TOKENS_FALLBACK = _IMAGE_BASE_TOKENS + _IMAGE_TILE_TOKENS * 6

# Header-parse scan budgets. PNG/GIF/WEBP dimensions live in the first few dozen
# bytes; JPEG SOF markers can sit much deeper, so we scan a bounded prefix.
_HEADER_SCAN_BYTES = 64
_JPEG_MAX_SCAN = 32 * 1024

# SOF (start-of-frame) markers that carry image dimensions; excludes DHT (0xC4),
# JPG (0xC8) and DAC (0xCC), which are not frame headers.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)

# Lazily-initialized, module-cached tiktoken encoder. `_ENCODER_INIT_DONE`
# guards against re-attempting a failed import/BPE-fetch on every call — a
# failure caches `_ENCODER = None` so the heuristic is used from then on.
_ENCODER: Optional[Any] = None
_ENCODER_INIT_DONE = False


def _get_encoder() -> Optional[Any]:
    """Return a cached tiktoken ``o200k_base`` encoder, or ``None`` if it could
    not be initialized. Attempted at most once per process: ``import tiktoken``
    and the first ``get_encoding`` call fetch the BPE vocabulary and may fail in
    an offline container, so any Exception caches a ``None`` fallback."""
    global _ENCODER, _ENCODER_INIT_DONE
    if _ENCODER_INIT_DONE:
        return _ENCODER
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("o200k_base")
    except Exception:
        _ENCODER = None
    finally:
        _ENCODER_INIT_DONE = True
    return _ENCODER


def _count_text_tokens(text: str) -> int:
    """Token count for a single string: real BPE via tiktoken when available,
    else the ~4-chars/token heuristic. The encode call is itself guarded so a
    per-string encode failure falls back to the heuristic for that string only."""
    if not text:
        return 0
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return _chars_to_tokens(len(text))


def _chars_to_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return math.ceil(chars / _CHARS_PER_TOKEN)


# ── Image token cost ─────────────────────────────────────────────────────────


def _high_detail_tiles(width: int, height: int) -> int:
    """Number of 512px tiles OpenAI bills for a high/auto-detail image of the
    given pixel dimensions: fit within a 2048×2048 box, scale the shortest side
    to 768px (both downscale-only), then tile at 512px. Pure integer math."""
    if width <= 0 or height <= 0:
        return 0
    # 1) fit within 2048×2048, preserving aspect ratio (never upscale)
    if width > _IMAGE_MAX_SQUARE or height > _IMAGE_MAX_SQUARE:
        if width >= height:
            height = max(1, (height * _IMAGE_MAX_SQUARE) // width)
            width = _IMAGE_MAX_SQUARE
        else:
            width = max(1, (width * _IMAGE_MAX_SQUARE) // height)
            height = _IMAGE_MAX_SQUARE
    # 2) scale the shortest side to 768px, preserving aspect ratio (never upscale)
    if min(width, height) > _IMAGE_TARGET_SHORT:
        if width <= height:
            height = max(1, (height * _IMAGE_TARGET_SHORT) // width)
            width = _IMAGE_TARGET_SHORT
        else:
            width = max(1, (width * _IMAGE_TARGET_SHORT) // height)
            height = _IMAGE_TARGET_SHORT
    # 3) count 512px tiles
    return math.ceil(width / _IMAGE_TILE_PX) * math.ceil(height / _IMAGE_TILE_PX)


def image_tokens(width: int, height: int, detail: str = "high") -> int:
    """Token cost of one image at the given pixel dimensions and OpenAI ``detail``
    level. ``"low"`` is a flat 85; ``"high"``/``"auto"`` (the estimate-time
    assumption, since detail is unknown until translation) is 85 + 170×tiles.
    Non-positive dimensions fall back to the conservative flat constant."""
    if detail == "low":
        return _IMAGE_BASE_TOKENS
    tiles = _high_detail_tiles(width, height)
    if tiles <= 0:
        return _IMAGE_TOKENS_FALLBACK
    return _IMAGE_BASE_TOKENS + _IMAGE_TILE_TOKENS * tiles


def _decode_b64_prefix(b64_data: str, max_bytes: int) -> bytes:
    """Decode at most ``max_bytes`` of a base64 string by slicing a whole-quantum
    prefix (a multiple of 4 chars) so we never decode a megabyte to read a header.
    Returns ``b""`` on any decode failure."""
    n_chars = ((max_bytes + 2) // 3) * 4
    prefix = b64_data[:n_chars]
    prefix = prefix[: len(prefix) - (len(prefix) % 4)]
    if not prefix:
        return b""
    try:
        return base64.b64decode(prefix)
    except Exception:
        return b""


def _png_dims(header: bytes) -> Optional[Tuple[int, int]]:
    """PNG IHDR: 8-byte signature, 4-byte length, ``IHDR``, then width/height as
    big-endian uint32 at offsets 16 and 20."""
    if len(header) < 24 or header[12:16] != b"IHDR":
        return None
    w = int.from_bytes(header[16:20], "big")
    h = int.from_bytes(header[20:24], "big")
    return (w, h) if w and h else None


def _gif_dims(header: bytes) -> Optional[Tuple[int, int]]:
    """GIF logical screen descriptor: width/height as little-endian uint16 at
    offsets 6 and 8, right after the 6-byte ``GIF87a``/``GIF89a`` signature."""
    if len(header) < 10:
        return None
    w = int.from_bytes(header[6:8], "little")
    h = int.from_bytes(header[8:10], "little")
    return (w, h) if w and h else None


def _webp_dims(header: bytes) -> Optional[Tuple[int, int]]:
    """WEBP dimensions, dispatched by the chunk fourCC at offset 12 (VP8 lossy,
    VP8L lossless, VP8X extended). Fields are little-endian; VP8/VP8L pack the
    dimensions into 14-bit fields, VP8X stores canvas size minus one."""
    if len(header) < 16:
        return None
    fmt = header[12:16]
    if fmt == b"VP8 ":  # lossy: 3-byte frame tag + 3-byte start code, then dims
        if len(header) < 30:
            return None
        w = int.from_bytes(header[26:28], "little") & 0x3FFF
        h = int.from_bytes(header[28:30], "little") & 0x3FFF
        return (w, h) if w and h else None
    if fmt == b"VP8L":  # lossless: signature byte 0x2F, then 14+14 bit dims minus one
        if len(header) < 25:
            return None
        bits = int.from_bytes(header[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return (w, h)
    if fmt == b"VP8X":  # extended: 3-byte canvas width-1 and height-1
        if len(header) < 30:
            return None
        w = int.from_bytes(header[24:27], "little") + 1
        h = int.from_bytes(header[27:30], "little") + 1
        return (w, h)
    return None


def _jpeg_dims(data: bytes) -> Optional[Tuple[int, int]]:
    """JPEG dimensions by walking marker segments from SOI until an SOF frame
    header (height/width as big-endian uint16 after the 1-byte precision). Skips
    each segment by its length; bails at SOS (0xDA) or once the bounded prefix is
    exhausted, returning ``None`` so the conservative fallback applies."""
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # 0xFF fill bytes and standalone markers (SOI/EOI/RSTn/TEM) carry no length
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:  # start of scan: SOF must have appeared before this
            break
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            break
        if marker in _JPEG_SOF_MARKERS:
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def sniff_image_dimensions(b64_data: str) -> Optional[Tuple[int, int]]:
    """Best-effort ``(width, height)`` for a base64 image payload, parsing only a
    bounded header prefix (dependency-free, stdlib only). Returns ``None`` on any
    unrecognized/short/corrupt header so callers apply the conservative fallback."""
    if not b64_data:
        return None
    # Tolerate an accidental data-URI wrapper (Anthropic sends raw base64).
    if b64_data.startswith("data:"):
        comma = b64_data.find(",")
        if comma != -1:
            b64_data = b64_data[comma + 1:]
    header = _decode_b64_prefix(b64_data, _HEADER_SCAN_BYTES)
    if len(header) < 12:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_dims(header)
    if header[:3] == b"GIF":
        return _gif_dims(header)
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return _webp_dims(header)
    if header[:2] == b"\xff\xd8":  # JPEG: SOF can sit deep, scan a larger prefix
        return _jpeg_dims(_decode_b64_prefix(b64_data, _JPEG_MAX_SCAN))
    return None


def image_block_tokens(block: Dict[str, Any]) -> int:
    """Token cost of one Anthropic ``image`` content block. Measures a base64
    source via :func:`sniff_image_dimensions`; URL sources (no bytes) and every
    parse failure use the conservative flat fallback. Assumes high/auto detail."""
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        data = source.get("data")
        if isinstance(data, str):
            dims = sniff_image_dimensions(data)
            if dims is not None:
                return image_tokens(dims[0], dims[1])
    return _IMAGE_TOKENS_FALLBACK


def _text_of_content(content: Any, image_sink: Optional[List[int]] = None) -> str:
    """Flatten any Anthropic content shape (string or list of blocks) into a
    single string whose *length* drives the estimate. All text-bearing fields
    contribute; unknown blocks are JSON-encoded so their size still counts.

    ``image`` blocks are the exception: their base64 payload is deliberately kept
    out of the text stream (counting it as text is the bug this module fixes).
    When ``image_sink`` is provided, each image's token cost is appended to it for
    the caller to add separately; without a sink the image contributes nothing."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "thinking":
                parts.append(str(block.get("thinking", "")))
                parts.append(str(block.get("signature", "")))
            elif btype == "tool_use":
                parts.append(str(block.get("name", "")))
                parts.append(
                    json.dumps(block.get("input", {}), separators=(",", ":"), ensure_ascii=False)
                )
            elif btype == "image":
                # Do NOT append the base64 payload to the text stream; bill it
                # separately via the tile math (see module docstring).
                if image_sink is not None:
                    image_sink.append(image_block_tokens(block))
            elif btype == "tool_result":
                # tool_result content may itself carry image blocks (e.g. a
                # browser tool returning a screenshot) — thread the sink through.
                parts.append(_text_of_content(block.get("content"), image_sink))
            else:
                parts.append(json.dumps(block, separators=(",", ":"), ensure_ascii=False))
        return "".join(parts)
    return json.dumps(content, separators=(",", ":"), ensure_ascii=False)


def count_tokens(anthropic_body: Dict[str, Any]) -> int:
    """Estimate the token count of an Anthropic Messages request body.

    Returns a non-negative integer. Deterministic and monotonic (see module
    docstring). Counts each text string with tiktoken when available, else the
    ~4-chars/token heuristic (see module docstring for the fallback rationale)."""
    total = _BASE_OVERHEAD

    # When the encoder is unavailable we preserve the pre-change heuristic
    # exactly: sum all character lengths, then take a single ceil at the end.
    # With the encoder we accumulate real BPE token counts per string. The
    # body-walking structure (which strings get counted) is identical either way.
    heuristic = _get_encoder() is None
    total_chars = 0
    total_text_tokens = 0
    # Image token costs are accumulated out-of-band (not as text) and summed in
    # at the end; this keeps the estimate monotonic while base64 stays uncounted.
    image_sink: List[int] = []

    def _add(text: str) -> None:
        nonlocal total_chars, total_text_tokens
        if heuristic:
            total_chars += len(text)
        else:
            total_text_tokens += _count_text_tokens(text)

    # system
    system = anthropic_body.get("system")
    if system:
        _add(_text_of_content(system, image_sink))
        total += _PER_SYSTEM_OVERHEAD

    # messages
    messages = anthropic_body.get("messages") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += _PER_MESSAGE_OVERHEAD
        _add(str(message.get("role", "")))
        _add(_text_of_content(message.get("content"), image_sink))

    # tools
    tools = anthropic_body.get("tools") or []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        total += _PER_TOOL_OVERHEAD
        _add(str(tool.get("name", "")))
        _add(str(tool.get("description", "")))
        _add(
            json.dumps(tool.get("input_schema", {}), separators=(",", ":"), ensure_ascii=False)
        )

    total += total_text_tokens if not heuristic else _chars_to_tokens(total_chars)
    total += sum(image_sink)
    return total
