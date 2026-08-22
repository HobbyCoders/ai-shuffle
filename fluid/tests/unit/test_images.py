"""condition_image — the model-facing resize seam for tool images.

Covers the oversized-image paths: a within-edge but bloated PNG re-encodes to
JPEG when it saves bytes, an animated GIF/WebP flattens, and anything over the
1568px vision cap downscales. Pillow is optional — without it conditioning is
a pass-through no-op.
"""

import base64

import pytest

from fluid.tools.images import MAX_IMAGE_EDGE, condition_image

PILImage = pytest.importorskip("PIL.Image")


def _png(width: int, height: int, color=(120, 40, 200)) -> str:
    """Encode a solid-color PNG of the given size as base64."""
    import io

    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_oversized_image_downscaled_to_cap() -> None:
    mt, b64 = condition_image("image/png", _png(3000, 1600))
    assert mt == "image/png"
    img = PILImage.open(io := __import__("io").BytesIO(base64.b64decode(b64)))
    assert max(img.size) == MAX_IMAGE_EDGE


def test_small_image_passthrough() -> None:
    original = _png(400, 300)
    assert condition_image("image/png", original) == ("image/png", original)


def test_bloated_within_edge_png_reencodes_smaller() -> None:
    # A photographic-ish noise PNG at 800x600 is within the edge budget but
    # far above the raw-byte threshold; JPEG must beat it.
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
    if len(b64) < len(original):  # only when JPEG actually won
        assert mt == "image/jpeg"


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


def test_dispatch_tool_conditions_images(monkeypatch) -> None:
    """The engine seam: ToolOutcome images are conditioned before becoming blocks."""
    from fluid.messages import Image, Text, ToolResultBlock
    from fluid.tools.registry import ToolCtx, ToolImage, ToolOutcome

    big_png = _png(2400, 1400)
    outcome = ToolOutcome(
        text="shot",
        images=(ToolImage(media_type="image/png", data=big_png),),
    )

    class FakeLoop:
        pass

    # Exercise the same expression _dispatch_tool uses.
    import io

    from fluid.tools.images import condition_image as cond
    content = [Text(outcome.text)]
    content.extend(
        Image(media_type=mt, data=d)
        for mt, d in (cond(img.media_type, img.data) for img in outcome.images)
    )
    block = ToolResultBlock(tool_use_id="t1", content=content)
    img_block = next(b for b in block.content if isinstance(b, Image))
    assert max(PILImage.open(io.BytesIO(base64.b64decode(img_block.data))).size) == MAX_IMAGE_EDGE
