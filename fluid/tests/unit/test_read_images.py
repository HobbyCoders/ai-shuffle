"""Read tool image/binary/truncation paths + image plumbing through the stack."""

import base64
from io import BytesIO
import sys

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.read import DEFAULT_LINE_LIMIT, MAX_LINE_CHARS
from fluid.tools.mcp.client import _map_call_result
from fluid.tools.registry import ToolCtx, ToolRegistry

# A complete 1x1 PNG, including checksums, so Pillow can verify it.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtins(registry)
    return registry


async def test_read_image_returns_base64_not_text(tmp_path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(PNG_BYTES)
    ctx = ToolCtx(cwd=str(tmp_path))

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ctx)

    assert not out.is_error
    assert len(out.images) == 1
    img = out.images[0]
    assert img.media_type == "image/png"
    assert base64.b64decode(img.data) == PNG_BYTES
    # The model-facing text is a short label, never decoded binary.
    assert "shot.png" in out.text and len(out.text) < 200
    assert str(f) in ctx.read_files


@pytest.mark.parametrize("payload", [b"", b"not an image", PNG_BYTES[:-16]])
async def test_read_invalid_image_is_error(tmp_path, payload) -> None:
    if payload:
        pytest.importorskip("PIL.Image")
    f = tmp_path / "bad.jpg"
    f.write_bytes(payload)
    ctx = ToolCtx(cwd=str(tmp_path))

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ctx)

    assert out.is_error
    assert not out.images
    assert "not a readable image" in out.text
    assert str(f) in out.text
    assert f"{len(payload)} bytes" in out.text
    assert str(f) not in ctx.read_files


@pytest.mark.parametrize("truncated", [False, True])
async def test_read_jpeg_requires_pixel_decode(tmp_path, truncated) -> None:
    pil_image = pytest.importorskip("PIL.Image")
    buffer = BytesIO()
    with pil_image.new("RGB", (32, 32), "red") as image:
        image.save(buffer, format="JPEG")
    payload = buffer.getvalue()
    if truncated:
        payload = payload[:-10]
        # Header verification alone does not catch this truncated JPEG.
        with pil_image.open(BytesIO(payload)) as image:
            image.verify()
    f = tmp_path / "shot.jpg"
    f.write_bytes(payload)
    ctx = ToolCtx(cwd=str(tmp_path))

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ctx)

    assert str(f) in out.text
    assert f"{len(payload)} bytes" in out.text
    if truncated:
        assert out.is_error
        assert not out.images
        assert "not a readable image" in out.text
        assert str(f) not in ctx.read_files
    else:
        assert not out.is_error
        assert base64.b64decode(out.images[0].data) == payload


@pytest.mark.parametrize("payload", [b"", b"not an image"])
async def test_read_image_without_pillow(tmp_path, monkeypatch, payload) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    f = tmp_path / "shot.png"
    f.write_bytes(payload)
    out = await _registry().dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))

    if not payload:
        assert out.is_error and not out.images
    else:
        assert not out.is_error
        assert base64.b64decode(out.images[0].data) == payload


async def test_read_image_too_large_is_error(tmp_path, monkeypatch) -> None:
    import fluid.tools.builtins.read as read_mod

    monkeypatch.setattr(read_mod, "MAX_IMAGE_BYTES", 16)
    f = tmp_path / "big.png"
    f.write_bytes(PNG_BYTES)

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    assert out.is_error and "too large" in out.text and not out.images


async def test_read_non_image_binary_refused(tmp_path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02" * 100)

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    assert out.is_error and "binary file" in out.text


async def test_read_text_default_line_limit_and_continuation(tmp_path) -> None:
    f = tmp_path / "big.log"
    f.write_text("\n".join(f"line{i}" for i in range(1, DEFAULT_LINE_LIMIT + 100)))

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    body = out.text.splitlines()
    assert body[0] == "1\tline1"
    assert body[DEFAULT_LINE_LIMIT - 1] == f"{DEFAULT_LINE_LIMIT}\tline{DEFAULT_LINE_LIMIT}"
    assert f"offset={DEFAULT_LINE_LIMIT + 1}" in body[-1]  # continuation hint


async def test_read_explicit_limit_can_exceed_default(tmp_path) -> None:
    f = tmp_path / "short-lines.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 2201)))
    assert DEFAULT_LINE_LIMIT == 2_000

    out = await _registry().dispatch(
        "Read", {"file_path": str(f), "limit": 2100}, ToolCtx(cwd=str(tmp_path))
    )

    assert out.text.splitlines()[2099] == "2100\tline2100"
    assert "offset=2101" in out.text


async def test_read_wide_pages_recover_every_line_and_bound_edit_coverage(tmp_path) -> None:
    import re

    from fluid.tools import read_window
    from fluid.tools.builtins.read import MAX_TEXT_CHARS

    f = tmp_path / "wide-pages.txt"
    source = [
        f"row{i}: " + "é" * 990
        for i in range(1, 2 * MAX_TEXT_CHARS // 990 + 2)
    ]
    f.write_text("\n".join(source), encoding="utf-8")
    ctx = ToolCtx(cwd=str(tmp_path))
    registry = _registry()
    recovered = []
    offset = 1
    pages = 0
    while offset <= len(source):
        out = await registry.dispatch(
            "Read", {"file_path": str(f), "offset": offset, "limit": 2000}, ctx
        )
        assert not out.is_error
        lines = out.text.splitlines()
        body = [line for line in lines if re.match(r"^\d+\t", line)]
        assert body, "each page makes progress"
        assert len("\n".join(body)) <= MAX_TEXT_CHARS
        recovered.extend(line.split("\t", 1)[1] for line in body)
        next_offset = offset + len(body)
        if next_offset <= len(source):
            assert f"offset={next_offset}" in lines[-1]
            assert out.window_of.start == offset - 1
            assert out.window_of.stop == next_offset - 1
            if pages == 0:
                assert read_window.windows_of(ctx, str(f)) == [(1, next_offset - 1)]
                denied = await registry.dispatch(
                    "Edit", {
                        "file_path": str(f), "old_string": source[next_offset - 1],
                        "new_string": "must not write an unread line",
                    }, ctx,
                )
                assert denied.is_error
                assert f.read_text(encoding="utf-8").splitlines() == source
        else:
            assert "output limited" not in out.text
        offset = next_offset
        pages += 1
    assert pages > 1
    assert recovered == source


async def test_read_page_budget_exact_eof_and_one_more_line(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import read as read_mod

    f = tmp_path / "boundary.txt"
    f.write_text("abc\ndef", encoding="utf-8")
    monkeypatch.setattr(read_mod, "MAX_TEXT_CHARS", len("1\tabc\n2\tdef"))
    registry = _registry()
    out = await registry.dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    assert out.text == "1\tabc\n2\tdef"
    assert out.window_of is None

    f.write_text("abc\ndef\nghi", encoding="utf-8")
    out = await registry.dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    assert out.text.startswith("1\tabc\n2\tdef\n")
    assert "offset=3" in out.text
    assert out.window_of.stop == 2


async def test_read_text_long_lines_truncated(tmp_path) -> None:
    f = tmp_path / "wide.txt"
    f.write_text("x" * (MAX_LINE_CHARS * 3) + "\nshort\n")

    out = await _registry().dispatch("Read", {"file_path": str(f)}, ToolCtx(cwd=str(tmp_path)))
    first, second = out.text.splitlines()[:2]
    assert first.endswith("[line truncated]")
    assert len(first) < MAX_LINE_CHARS + 100
    assert second == "2\tshort"


async def test_read_offset_limit_window(tmp_path) -> None:
    f = tmp_path / "n.txt"
    f.write_text("\n".join(f"l{i}" for i in range(1, 51)))

    out = await _registry().dispatch(
        "Read", {"file_path": str(f), "offset": 10, "limit": 3}, ToolCtx(cwd=str(tmp_path))
    )
    lines = out.text.splitlines()
    assert lines[:3] == ["10\tl10", "11\tl11", "12\tl12"]
    assert "offset=13" in lines[-1]


async def test_normalize_keeps_sdk_envelope_images() -> None:
    from fluid.tools.registry import _normalize

    out = _normalize({
        "content": [
            {"type": "text", "text": "screenshot taken"},
            {"type": "image", "mimeType": "image/jpeg", "data": "aGk="},
        ]
    })
    assert out.text == "screenshot taken"
    assert out.images[0].media_type == "image/jpeg" and out.images[0].data == "aGk="


def test_mcp_map_call_result_keeps_images() -> None:
    result = {
        "content": [
            {"type": "text", "text": "ok"},
            {"type": "image", "mimeType": "image/png", "data": "aGk="},
        ],
        "isError": False,
    }
    mapped = _map_call_result(result)
    assert mapped["text"] == "ok"
    assert mapped["images"] == [{"media_type": "image/png", "data": "aGk="}]


def test_trimmer_counts_and_trims_image_results() -> None:
    from fluid.context.trim import ToolResultTrimmer, TrimConfig, _block_chars
    from fluid.messages import Image, Message, Text, ToolResultBlock
    from fluid.store.base import SessionNode

    block = ToolResultBlock(
        tool_use_id="t1",
        content=[Text("[image]"), Image(media_type="image/png", data="aGk=")],
    )
    assert _block_chars(block) >= 6_000  # image weight makes it trim-eligible

    nodes = [
        SessionNode(
            node_id=f"n{i}", parent_id=None,
            message=Message(role="user", content=[block]),
        )
        for i in range(3)
    ]
    trimmer = ToolResultTrimmer(config=TrimConfig(keep_recent_tool_results=1))
    assert trimmer.force_trim(nodes)
    rendered = trimmer.view(nodes)
    assert not any(
        isinstance(c, Image)
        for b in rendered[0].content for c in getattr(b, "content", [])
    )
