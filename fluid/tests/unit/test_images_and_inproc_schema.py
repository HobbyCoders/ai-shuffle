"""condition_image without Pillow installed (a fake image stands in), and the
in-process tool schema normalizer's shorthand forms."""

from __future__ import annotations

import base64
import builtins
from typing import Literal, NotRequired, Required, TypedDict

import pytest

from fluid.tools import images
from fluid.tools.images import MAX_IMAGE_EDGE, condition_image
from fluid.tools.inproc import input_schema

# ─── condition_image (fake Pillow) ───────────────────────────────────────────


class _FakeImage:
    """Just enough of PIL.Image.Image for condition_image."""

    def __init__(self, size, mode="RGB", payload=b"x" * 10) -> None:
        self.size = size
        self.mode = mode
        self.payload = payload
        self.saved: list[tuple[str, dict]] = []
        self.converted: list[str] = []
        self.resized_to = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resize(self, size, _resample):
        self.resized_to = size
        clone = _FakeImage(size, self.mode, self.payload)
        clone.saved, clone.converted = self.saved, self.converted
        return clone

    def convert(self, mode):
        self.converted.append(mode)
        clone = _FakeImage(self.size, mode, self.payload)
        clone.saved, clone.converted = self.saved, self.converted
        return clone

    def save(self, buffer, format, **kw):
        self.saved.append((format, kw))
        buffer.write(self.payload)


def _b64(n: int) -> str:
    return base64.b64encode(b"\x89" * n).decode("ascii")


@pytest.fixture
def fake_pil(monkeypatch):
    state: dict = {}

    def open_image(raw: bytes):
        return state["image"]

    monkeypatch.setattr(images, "_open_image", open_image)
    monkeypatch.setattr(images, "_resampling", lambda: "lanczos")
    return state


def test_passthrough_without_pillow(monkeypatch) -> None:
    real_import = builtins.__import__

    def no_pil(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    data = _b64(100)
    assert condition_image("image/png", data) == ("image/png", data)


def test_small_image_passes_through_unchanged(fake_pil) -> None:
    fake_pil["image"] = _FakeImage((400, 300))
    data = _b64(100)
    assert condition_image("image/png", data) == ("image/png", data)
    assert fake_pil["image"].saved == []


def test_oversized_png_is_downscaled_and_kept_png(fake_pil) -> None:
    fake_pil["image"] = img = _FakeImage((3136, 1000))
    mt, out = condition_image("image/png", _b64(5000))
    assert mt == "image/png"
    assert img.resized_to == (MAX_IMAGE_EDGE, 500)
    assert img.saved[0][0] == "PNG"
    assert base64.b64decode(out) == img.payload


def test_gif_flattens_to_jpeg_via_rgb(fake_pil) -> None:
    fake_pil["image"] = img = _FakeImage((2000, 2000), mode="P")
    mt, _ = condition_image("image/gif", _b64(5000))
    assert mt == "image/jpeg"
    assert img.converted == ["RGB"]
    assert img.saved[0] == ("JPEG", {"quality": images.JPEG_QUALITY})


def test_large_png_past_megapixel_gate_becomes_jpeg(fake_pil) -> None:
    fake_pil["image"] = _FakeImage((1200, 1000), mode="RGBA")
    mt, _ = condition_image("image/png", _b64(images._REENCODE_RAW_BYTES + 10))
    assert mt == "image/jpeg"


def test_reencode_that_grows_keeps_within_budget_original(fake_pil) -> None:
    fake_pil["image"] = _FakeImage((1200, 1000), payload=b"y" * 600_000)
    data = _b64(images._REENCODE_RAW_BYTES + 10)
    assert condition_image("image/webp", data) == ("image/webp", data)


def test_resize_that_grows_still_obeys_pixel_budget(fake_pil) -> None:
    fake_pil["image"] = img = _FakeImage((3000, 3000), payload=b"y" * 50_000)
    data = _b64(100)
    media_type, conditioned = condition_image("image/webp", data)
    assert img.resized_to == (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE)
    assert media_type == "image/jpeg"
    assert base64.b64decode(conditioned) == img.payload


def test_decode_failure_passes_through(fake_pil) -> None:
    def broken(raw: bytes):
        raise OSError("cannot identify image file")

    images._open_image, saved = broken, images._open_image
    try:
        assert condition_image("image/png", "!!!") == ("image/png", "!!!")
    finally:
        images._open_image = saved


# ─── inproc input_schema ─────────────────────────────────────────────────────


class _Args(TypedDict):
    path: str
    count: NotRequired[int]
    mode: Literal["a", "b"]
    tags: list[str]
    meta: dict[str, float]
    maybe: str | None


def test_typeddict_shorthand_maps_every_form() -> None:
    schema = input_schema(_Args)
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["path"] == {"type": "string"}
    assert props["count"] == {"type": "integer"}
    assert props["mode"] == {"enum": ["a", "b"], "type": "string"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["meta"] == {"type": "object", "additionalProperties": {"type": "number"}}
    assert props["maybe"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    # NotRequired is honoured even though this module uses postponed
    # annotations, where TypedDict.__required_keys__ alone gets it wrong.
    assert set(schema["required"]) == {"path", "mode", "tags", "meta", "maybe"}


class _Partial(TypedDict, total=False):
    a: str
    b: Required[int]


def test_total_false_typeddict_keeps_explicit_required() -> None:
    assert input_schema(_Partial)["required"] == ["b"]


def test_dict_shorthand_json_schema_and_fallbacks() -> None:
    full = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert input_schema(full) is full
    assert input_schema({"a": str, "b": list, "c": object}) == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "array"}, "c": {}},
        "required": ["a", "b", "c"],
    }
    assert input_schema(None) == {"type": "object"}
    bare_list = input_schema({"xs": list[int], "any_list": list})
    assert bare_list["properties"]["xs"] == {"type": "array", "items": {"type": "integer"}}


def test_shorthand_with_nested_schema_or_unhashable_value_does_not_crash() -> None:
    schema = input_schema({
        "path": str,
        "opts": {"type": "object", "properties": {"k": {"type": "string"}}},
        "odd": ["not", "a", "type"],
    })
    assert schema["properties"]["path"] == {"type": "string"}
    assert schema["properties"]["opts"]["type"] == "object"
    assert schema["properties"]["odd"] == {}
