"""count_tokens determinism + monotonicity tests (§D5, case 8).

Also covers the tiktoken (o200k_base) counting path added to fix the
"Prompt is too long" under-count, plus the heuristic fallback and its
failure-caching behavior (see token_estimate module docstring)."""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from app.core.translator_gateway import token_estimate
from app.core.translator_gateway.token_estimate import count_tokens


@pytest.fixture(autouse=True)
def _reset_encoder_cache():
    """Each test starts from a clean lazy-encoder cache so monkeypatching the
    getter's inputs actually takes effect (the cache is module-global)."""
    saved = (token_estimate._ENCODER, token_estimate._ENCODER_INIT_DONE)
    token_estimate._ENCODER = None
    token_estimate._ENCODER_INIT_DONE = False
    try:
        yield
    finally:
        token_estimate._ENCODER, token_estimate._ENCODER_INIT_DONE = saved


def _force_heuristic(monkeypatch):
    """Pin the encoder getter into its cached-failure (heuristic) state."""
    monkeypatch.setattr(token_estimate, "_get_encoder", lambda: None)


# ── tiny programmatic image-header fixtures (no checked-in binaries) ──────────

def _png_b64(width: int, height: int, tail_bytes: int = 0) -> str:
    """Minimal valid PNG: signature + IHDR carrying width/height. ``tail_bytes``
    appends junk so we can simulate a large payload without a real image."""
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    chunk = (
        struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    )
    raw = b"\x89PNG\r\n\x1a\n" + chunk + (b"\x00" * tail_bytes)
    return base64.b64encode(raw).decode()


def _jpeg_b64(width: int, height: int, pad_before_sof: int = 0) -> str:
    """Minimal JPEG: SOI, an APP0/filler segment, then an SOF0 frame header.
    ``pad_before_sof`` inflates a comment segment so the SOF sits deep, exercising
    the bounded multi-kilobyte scan."""
    soi = b"\xff\xd8"
    filler = b""
    if pad_before_sof:
        body = b"\x00" * pad_before_sof
        filler = b"\xff\xfe" + struct.pack(">H", len(body) + 2) + body  # COM segment
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1])
    )
    return base64.b64encode(soi + filler + sof + b"\xff\xda").decode()


def _gif_b64(width: int, height: int) -> str:
    raw = b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"
    return base64.b64encode(raw).decode()


def _image_message(data: str, media_type: str = "image/png") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": data}},
        ],
    }


def test_deterministic():
    body = {
        "model": "m",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello there, how are you today?"}],
    }
    assert count_tokens(body) == count_tokens(body)
    assert count_tokens(dict(body)) == count_tokens(dict(body))


def test_empty_request_is_small_nonnegative():
    n = count_tokens({"model": "m", "messages": []})
    assert n >= 0
    assert n < 20


def test_monotonic_in_message_text():
    short = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    long = {"model": "m", "messages": [{"role": "user", "content": "hi " * 500}]}
    assert count_tokens(long) > count_tokens(short)


def test_monotonic_in_message_count():
    one = {"model": "m", "messages": [{"role": "user", "content": "same text"}]}
    two = {"model": "m", "messages": [
        {"role": "user", "content": "same text"},
        {"role": "assistant", "content": "same text"},
    ]}
    assert count_tokens(two) > count_tokens(one)


def test_monotonic_in_tools():
    no_tools = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    with_tools = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "Gets the weather for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
    }
    assert count_tokens(with_tools) > count_tokens(no_tools)


def test_system_prompt_increases_count():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    with_sys = {"model": "m", "system": "A long system prompt here.", "messages": [{"role": "user", "content": "x"}]}
    assert count_tokens(with_sys) > count_tokens(base)


def test_string_and_block_content_equivalent_size_order():
    # richer content (tool_use with big input) counts more than a tiny string
    tiny = {"model": "m", "messages": [{"role": "assistant", "content": "ok"}]}
    rich = {"model": "m", "messages": [{"role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "c1", "name": "do", "input": {"payload": "x" * 200}},
    ]}]}
    assert count_tokens(rich) > count_tokens(tiny)


# ── tiktoken (o200k_base) counting path ──────────────────────────────────────

_CODE = (
    "def transform(records):\n"
    "    return {r['id']: json.dumps(r, separators=(',', ':')) for r in records}\n"
    "# handles nested {\"a\": [1, 2, 3], \"b\": {\"c\": true}} payloads\n"
) * 4


def test_tiktoken_path_uses_encoder_count_not_chars_over_four():
    """A code-heavy body counts with the real BPE tokenizer, not chars/4."""
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("o200k_base")

    body = {"model": "m", "messages": [{"role": "user", "content": _CODE}]}

    expected = (
        token_estimate._BASE_OVERHEAD
        + token_estimate._PER_MESSAGE_OVERHEAD
        + len(enc.encode("user"))
        + len(enc.encode(_CODE))
    )
    assert count_tokens(body) == expected

    # Code tokenizes hotter than 4 chars/token, so the encoder count must exceed
    # the old heuristic — this is exactly the under-count the fix addresses.
    heuristic = (
        token_estimate._BASE_OVERHEAD
        + token_estimate._PER_MESSAGE_OVERHEAD
        + token_estimate._chars_to_tokens(len("user") + len(_CODE))
    )
    assert count_tokens(body) > heuristic


# ── heuristic fallback path (encoder unavailable) ────────────────────────────

def test_heuristic_fallback_matches_prechange_behavior(monkeypatch):
    """With the encoder forced unavailable, the count is byte-identical to the
    pre-change sum-chars-then-single-ceil heuristic."""
    _force_heuristic(monkeypatch)

    body = {
        "model": "m",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello there, how are you today?"}],
    }
    # Pre-change algorithm, computed by hand:
    #   base 3 + system-overhead 3 + message-overhead 4 = 10
    #   chars = len("You are helpful.")=16 + len("user")=4 + len(content)=31 = 51
    #   ceil(51 / 4) = 13  →  10 + 13 = 23
    assert count_tokens(body) == 23


def test_heuristic_fallback_generic_equivalence(monkeypatch):
    """Fallback equals base + overheads + single ceil over summed char lengths,
    for an arbitrary body (matches the original implementation exactly)."""
    import json as _json

    _force_heuristic(monkeypatch)

    body = {
        "model": "m",
        "system": "sys prompt",
        "messages": [
            {"role": "user", "content": "first message body"},
            {"role": "assistant", "content": "second, a bit longer reply"},
        ],
        "tools": [
            {"name": "do_thing", "description": "does a thing",
             "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        ],
    }

    total = token_estimate._BASE_OVERHEAD
    chars = 0
    total += token_estimate._PER_SYSTEM_OVERHEAD
    chars += len("sys prompt")
    for role, content in (("user", "first message body"),
                          ("assistant", "second, a bit longer reply")):
        total += token_estimate._PER_MESSAGE_OVERHEAD
        chars += len(role) + len(content)
    total += token_estimate._PER_TOOL_OVERHEAD
    chars += len("do_thing") + len("does a thing")
    chars += len(_json.dumps(
        {"type": "object", "properties": {"q": {"type": "string"}}},
        separators=(",", ":"), ensure_ascii=False,
    ))
    expected = total + token_estimate._chars_to_tokens(chars)

    assert count_tokens(body) == expected


# ── failure caching (encoder init attempted at most once) ────────────────────

def test_encoder_init_attempted_only_once_after_failure(monkeypatch):
    """A failing tiktoken.get_encoding is attempted exactly once; subsequent
    counts reuse the cached failure and take the heuristic path."""
    tiktoken = pytest.importorskip("tiktoken")

    calls = {"n": 0}

    def _boom(name):
        calls["n"] += 1
        raise RuntimeError("offline: BPE fetch failed")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)

    body = {"model": "m", "messages": [{"role": "user", "content": "some text here"}]}
    first = count_tokens(body)
    second = count_tokens(body)
    third = count_tokens(body)

    assert calls["n"] == 1  # failure cached, not retried per call
    # Heuristic path is used after the cached failure.
    assert first == second == third
    assert token_estimate._ENCODER is None
    assert token_estimate._ENCODER_INIT_DONE is True


# ── image blocks: base64 must NOT be counted as text (the core B1 fix) ────────

def test_large_png_image_block_not_counted_as_text():
    """A ~1MB base64 PNG in a user image block bills ~1105 tokens (its tile cost),
    NOT the hundreds of thousands the old json.dumps-the-base64 path produced."""
    data = _png_b64(1280, 800, tail_bytes=768 * 1024)  # ~1MB base64 payload
    assert len(data) > 1_000_000  # the payload really is ~1MB of base64
    body = {"model": "m", "messages": [_image_message(data)]}
    n = count_tokens(body)
    assert n < 2000  # not the ~250k the base64 text would have produced
    # The image contributes exactly its high-detail tile cost (1280×800 → 1105).
    assert n > 1000


def test_large_png_image_nested_in_tool_result_not_counted_as_text():
    """Same guarantee when the image is nested inside a tool_result content list
    (e.g. a browser tool returning a screenshot)."""
    data = _png_b64(1280, 800, tail_bytes=768 * 1024)
    body = {
        "model": "m",
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [
                    {"type": "text", "text": "screenshot:"},
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png", "data": data}},
                ],
            }],
        }],
    }
    n = count_tokens(body)
    assert n < 2000
    assert n > 1000


# ── OpenAI tile math ─────────────────────────────────────────────────────────

def test_tile_math_small_image_is_base_plus_one_tile():
    # A tiny image needs no downscale and fits a single 512px tile: 85 + 170.
    assert token_estimate.image_tokens(100, 100) == 85 + 170
    assert token_estimate.image_tokens(1, 1) == 85 + 170


def test_tile_math_1280x800_is_six_tiles():
    # shortest side 800 → 768 gives ~1229×768 → ceil(1229/512)*ceil(768/512)=3*2=6.
    assert token_estimate.image_tokens(1280, 800) == 85 + 170 * 6  # 1105


def test_tile_math_very_large_image_is_bounded():
    # 4096×4096 → fit 2048² → shortest side to 768 → 768×768 → 2*2=4 tiles.
    assert token_estimate.image_tokens(4096, 4096) == 85 + 170 * 4  # 765


def test_tile_math_low_detail_is_flat_85():
    assert token_estimate.image_tokens(1280, 800, detail="low") == 85


def test_tile_math_nonpositive_dims_fall_back():
    assert token_estimate.image_tokens(0, 0) == token_estimate._IMAGE_TOKENS_FALLBACK
    assert token_estimate.image_tokens(-5, 10) == token_estimate._IMAGE_TOKENS_FALLBACK


# ── header parsing across formats + fallbacks ────────────────────────────────

def test_sniff_png_gif_jpeg_dimensions():
    assert token_estimate.sniff_image_dimensions(_png_b64(320, 240)) == (320, 240)
    assert token_estimate.sniff_image_dimensions(_gif_b64(640, 480)) == (640, 480)
    assert token_estimate.sniff_image_dimensions(_jpeg_b64(1024, 768)) == (1024, 768)


def test_sniff_jpeg_with_deep_sof_within_scan_budget():
    # SOF pushed ~8KB in still parses (bounded multi-kilobyte JPEG scan).
    data = _jpeg_b64(800, 600, pad_before_sof=8 * 1024)
    assert token_estimate.sniff_image_dimensions(data) == (800, 600)


def test_sniff_unparseable_header_returns_none():
    junk = base64.b64encode(b"not really an image, just some bytes here").decode()
    assert token_estimate.sniff_image_dimensions(junk) is None


def test_image_block_fallback_on_parse_failure():
    """A base64 block whose header we cannot parse bills the conservative flat
    constant, never zero and never a text-length count."""
    junk = base64.b64encode(b"\x00\x01\x02\x03 corrupt header \x04\x05").decode()
    block = {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": junk}}
    assert token_estimate.image_block_tokens(block) == token_estimate._IMAGE_TOKENS_FALLBACK


def test_url_source_image_uses_fallback():
    """URL-source images have no bytes to measure → conservative flat fallback."""
    block = {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
    assert token_estimate.image_block_tokens(block) == token_estimate._IMAGE_TOKENS_FALLBACK


# ── determinism + monotonicity with images ───────────────────────────────────

def test_image_body_deterministic():
    body = {"model": "m", "messages": [_image_message(_png_b64(1280, 800))]}
    assert count_tokens(body) == count_tokens(body)


def test_adding_image_increases_count():
    base = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "look at this"}]}]}
    with_image = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": _png_b64(1280, 800)}},
    ]}]}
    assert count_tokens(with_image) > count_tokens(base)


def test_image_body_deterministic_under_heuristic(monkeypatch):
    """Image accounting is independent of the text tokenizer path: it stays
    deterministic (and present) even when the encoder is forced unavailable."""
    _force_heuristic(monkeypatch)
    body = {"model": "m", "messages": [_image_message(_png_b64(1280, 800))]}
    n = count_tokens(body)
    assert n == count_tokens(body)
    assert n > 1000  # tile cost is added regardless of heuristic vs. tiktoken
