"""condition_image — the model-facing resize seam for tool images.

Covers the oversized-image paths: a bloated PNG re-encodes to JPEG once it is
past the megapixel gate but stays PNG below it, an animated GIF/WebP flattens,
and anything over the 1568px vision cap downscales. Provider serialization
also bounds older persisted images without rewriting their stored blocks.
"""

import base64

import pytest

from fluid.tools.images import MAX_IMAGE_EDGE, condition_image

from PIL import Image as PILImage


def _png(width: int, height: int, color=(120, 40, 200)) -> str:
    """Encode a solid-color PNG of the given size as base64."""
    import io

    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_pillow_is_a_runtime_dependency():
    import tomllib
    from pathlib import Path

    manifest = Path(__file__).parents[2] / "pyproject.toml"
    dependencies = tomllib.loads(manifest.read_text())["project"]["dependencies"]
    assert any(dep.lower().startswith("pillow>=") for dep in dependencies)


def test_oversized_image_downscaled_to_cap() -> None:
    mt, b64 = condition_image("image/png", _png(3000, 1600))
    assert mt == "image/png"
    img = PILImage.open(__import__("io").BytesIO(base64.b64decode(b64)))
    assert max(img.size) == MAX_IMAGE_EDGE


def test_small_image_passthrough() -> None:
    original = _png(400, 300)
    assert condition_image("image/png", original) == ("image/png", original)


def test_bloated_within_edge_png_stays_png() -> None:
    # 800x600 = 480k pixels, below the 1_150_000-pixel gate: however bloated
    # the raw bytes are, condition_image never transcodes to JPEG down here.
    # The gate exists to spare small crisp screenshots from JPEG artifacting.
    # Conditioning may re-encode the PNG, but it must never grow the payload.
    import io as _io
    import random

    rng = random.Random(7)
    buf = _io.BytesIO()
    img = PILImage.new("RGB", (800, 600))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(800 * 600)])
    img.save(buf, format="PNG")
    original = base64.b64encode(buf.getvalue()).decode("ascii")
    mt, b64 = condition_image("image/png", original)
    assert mt == "image/png"
    assert len(b64) <= len(original)


def test_bloated_large_png_reencodes_to_jpeg() -> None:
    # 1200x1000 = 1.2M pixels, past BOTH the raw-byte threshold (512_000) and
    # the pixel gate. A photographic-noise PNG (~3.6MB) has to transcode to
    # JPEG (~0.8MB) here — the margin is decisive, not encoder-dependent.
    import io as _io
    import random

    rng = random.Random(7)
    buf = _io.BytesIO()
    # frombytes over putdata: same determinism, far cheaper at 1.2M pixels.
    img = PILImage.frombytes("RGB", (1200, 1000), rng.randbytes(1200 * 1000 * 3))
    img.save(buf, format="PNG")
    original = base64.b64encode(buf.getvalue()).decode("ascii")
    mt, b64 = condition_image("image/png", original)
    assert mt == "image/jpeg"
    assert len(b64) < len(original)


def test_gif_flattens_to_jpeg() -> None:
    import io as _io
    import random

    rng = random.Random(3)
    buf = _io.BytesIO()
    img = PILImage.new("P", (2000, 1000))
    img.putpalette([rng.randrange(256) for _ in range(768)])
    img.putdata([rng.randrange(256) for _ in range(2000 * 1000)])
    img.save(buf, format="GIF")
    big = base64.b64encode(buf.getvalue()).decode("ascii")
    mt, b64 = condition_image("image/gif", big)
    assert len(b64) < len(big)
    assert mt == "image/jpeg"


def test_garbage_base64_passes_through() -> None:
    assert condition_image("image/png", "!!!not-an-image") == ("image/png", "!!!not-an-image")


@pytest.mark.parametrize("dialect", ["anthropic", "responses", "chat"])
@pytest.mark.parametrize("nested", [False, True], ids=["user-image", "tool-image"])
@pytest.mark.parametrize("size", [(400, 300), (3000, 1600)])
def test_serializers_bound_persisted_images_without_mutation(dialect, nested, size):
    import io
    import json

    from fluid.messages import Image, Message, Text, ToolResultBlock
    from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
    from fluid.providers.openai_responses.serialize import build_request_body as responses_body
    from fluid.providers._chat_completions import translate_message
    from fluid.providers.base import ProviderRequest

    original = _png(*size)
    image = Image(media_type="image/png", data=original)
    content = [Text("keep this text"), image]
    if nested:
        content = [ToolResultBlock(tool_use_id="read-image", content=content)]
    # Use the stored representation too; legacy history is not re-ingested.
    stored = json.dumps(Message(role="user", content=content).to_dict())
    message = Message.from_dict(json.loads(stored))
    request = ProviderRequest(model="test-model", messages=[message])
    if dialect == "anthropic":
        parts = anthropic_body(request)["messages"][0]["content"]
        if nested:
            parts = parts[0]["content"]
        source = parts[1]["source"]
        media_type, data = source["media_type"], source["data"]
    else:
        if dialect == "responses":
            items = responses_body(request)["input"]
            parts = items[0]["output" if nested else "content"]
            url = parts[1]["image_url"]
        else:
            items = translate_message(message)
            parts = items[-1]["content"]
            url = parts[1]["image_url"]["url"]
        media_type, data = url.removeprefix("data:").split(";base64,", 1)
    assert media_type == "image/png"
    with PILImage.open(io.BytesIO(base64.b64decode(data))) as decoded:
        assert max(decoded.size) <= MAX_IMAGE_EDGE
    if max(size) <= MAX_IMAGE_EDGE:
        assert data == original  # No needless churn of an already safe prefix.
    assert json.dumps(message.to_dict()) == stored
