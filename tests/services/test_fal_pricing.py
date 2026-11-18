"""fal prices from declared rates and schemas. All fal replies are fakes; the
schema shapes and rate rows are copied from fal's real responses (2026-11-17)."""
from decimal import Decimal

import httpx
import pytest

from app.services import fal_pricing
from app.services.fal_schema import normalize_openapi


def schema(properties, required=("prompt",), endpoint="fake/model"):
    return normalize_openapi(endpoint, {"components": {"schemas": {"Input": {
        "type": "object", "required": list(required), "properties": properties}}}})


DURATION_DESC = "Duration of the video in seconds. Supports 4 to 30 seconds, or auto to let the model decide based on the prompt."
SEEDANCE = {
    "prompt": {"type": "string"},
    "duration": {"type": "string", "enum": ["auto", *map(str, range(4, 31))], "default": "auto", "description": DURATION_DESC},
    "resolution": {"type": "string", "enum": ["480p", "720p", "1080p", "4k"], "default": "720p"},
    "aspect_ratio": {"type": "string", "enum": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"], "default": "auto"},
    "generate_audio": {"type": "boolean", "default": True},
}
SEEDANCE_ROW = {"endpoint_id": "bytedance/seedance-2.5/us/text-to-video", "unit_price": 0.0214, "unit": "1000 tokens", "currency": "USD"}


def price(row, properties, options=None, prompt="a fox", required=("prompt",)):
    return fal_pricing.price_from_row(row, schema(properties, required), options, prompt)


def test_seedance_auto_length_and_shape_price_the_largest_declared():
    # auto -> 30 s (largest enum), auto shape -> 21:9 (widest declared), 720p:
    # 720 * 1680 * 30 * 24 / 1024 * 1.15 = 978,075 tokens.
    p = price(SEEDANCE_ROW, SEEDANCE)
    assert p["status"] == "estimated" and Decimal(p["total"]) == Decimal("0.0214") * Decimal("978.075")
    assert "30 s" in p["basis"] and "widest" in p["basis"]


@pytest.mark.parametrize("resolution,lines,documented_per_second", [
    ("480p", 480, Decimal("0.2205")), ("720p", 720, Decimal("0.4730")), ("1080p", 1080, Decimal("1.164"))])
def test_seedance_selected_options_price_below_worst_case_and_cover_fal_figures(resolution, lines, documented_per_second):
    p = price(SEEDANCE_ROW, SEEDANCE, {"duration": "5", "resolution": resolution, "aspect_ratio": "16:9"})
    total = Decimal(p["total"])
    # Never under fal's own documented per-second figure, never the auto worst case.
    assert documented_per_second * 5 <= total < Decimal(price(SEEDANCE_ROW, SEEDANCE)["total"])
    assert "auto" not in p.get("basis", "") and "picks" not in p.get("basis", "")


def test_seedance_4k_and_input_video_are_unknown_with_reason():
    p = price(SEEDANCE_ROW, SEEDANCE, {"resolution": "4k"})
    assert p["status"] == "unknown" and p["total"] is None and p["reason"] == "rate_not_listed"
    assert p["rate"] == {"amount": "0.0214", "unit": "1000 tokens"}
    reference = {**SEEDANCE, "video_urls": {"type": "array", "items": {"type": "string"}}}
    p = price(SEEDANCE_ROW, reference, {"video_urls": ["https://example.com/a.mp4"]})
    assert p["reason"] == "depends_on_input"
    # Images alone are not billed as input video, so reference-to-video prices.
    images = {**reference, "image_urls": {"type": "array", "items": {"type": "string"}}}
    assert price(SEEDANCE_ROW, images, {"image_urls": ["https://example.com/a.png"]})["status"] == "estimated"


def test_auto_duration_bounded_by_declared_maximum_and_prose_range():
    per_second = {"unit_price": 0.28, "unit": "seconds", "currency": "USD"}
    numeric = {"prompt": {"type": "string"},
               "duration": {"anyOf": [{"type": "integer", "minimum": 3, "maximum": 15}, {"type": "null"}],
                            "description": "Video length in seconds."}}
    assert Decimal(price(per_second, numeric)["total"]) == Decimal("0.28") * 15
    assert Decimal(price(per_second, numeric, {"duration": 5})["total"]) == Decimal("0.28") * 5
    prose = {"prompt": {"type": "string"}, "duration": {"type": "number", "description": "Duration in seconds (0.5-22)"}}
    assert Decimal(price(per_second, prose)["total"]) == Decimal("0.28") * 22


def test_millisecond_and_block_units():
    music = {"prompt": {"type": "string"},
             "music_length_ms": {"type": "integer", "minimum": 3000, "maximum": 600000, "description": "Length in milliseconds."}}
    per_minute = {"unit_price": 0.6, "unit": "minutes", "currency": "USD"}
    assert Decimal(price(per_minute, music, {"music_length_ms": 90000})["total"]) == Decimal("1.2")  # 1.5 min -> 2 blocks
    assert Decimal(price(per_minute, music)["total"]) == Decimal("6")  # unset -> 10 min maximum
    clip = {"prompt": {"type": "string"}, "duration": {"type": "string", "enum": ["5", "10"], "default": "5",
                                                      "description": "Seconds of video."}}
    five = {"unit_price": 0.35, "unit": "5 seconds", "currency": "USD"}
    assert Decimal(price(five, clip, {"duration": "10"})["total"]) == Decimal("0.70")


def test_frame_count_models_price_frames_over_the_slower_frame_rate():
    # fal-ai/wan/v2.2-a14b/text-to-video: $0.08 per second, 81 frames at 16 fps.
    wan = {"prompt": {"type": "string"},
           "num_frames": {"type": "integer", "default": 81, "minimum": 17, "maximum": 161},
           "frames_per_second": {"type": "integer", "default": 16, "minimum": 4, "maximum": 60}}
    per_second = {"unit_price": 0.08, "unit": "seconds", "currency": "USD"}
    p = price(per_second, wan)
    assert Decimal(p["total"]) == Decimal("0.08") * 6 and "81 frames at 16 fps" in p["basis"]  # 5.06 s -> 6
    assert Decimal(price(per_second, wan, {"num_frames": 161, "frames_per_second": 4})["total"]) == Decimal("0.08") * 41
    # A faster frame rate never prices below the default-rate length.
    assert price(per_second, wan, {"frames_per_second": 60})["total"] == p["total"]


def test_bare_duration_is_seconds_and_input_matched_length_depends_on_input():
    per_second = {"unit_price": 0.001, "unit": "seconds", "currency": "USD"}
    mmaudio = {"prompt": {"type": "string"},
               "duration": {"type": "number", "default": 8, "minimum": 1, "maximum": 30,
                            "description": "The duration of the audio to generate."}}
    assert Decimal(price(per_second, mmaudio)["total"]) == Decimal("0.008")
    edit = {"prompt": {"type": "string"}, "video_url": {"type": "string"},
            "duration": {"type": "integer", "enum": [0, 2, 5, 10], "default": 0,
                         "description": "Output video duration in seconds. Default 0 means match input video duration."}}
    assert price(per_second, edit)["reason"] == "depends_on_input"
    assert Decimal(price(per_second, edit, {"duration": 5})["total"]) == Decimal("0.005")


@pytest.mark.parametrize("unit,reason", [
    ("units", "billed_by_usage"), ("credits", "billed_by_usage"), ("compute seconds", "billed_by_usage"),
    ("", "billed_by_usage"), ("30 frames", "depends_on_input"), ("input seconds", "depends_on_input"),
    ("video segments", "unsupported_unit")])
def test_units_without_a_bound_are_unknown_with_reason_and_rate(unit, reason):
    p = price({"unit_price": 0.1, "unit": unit, "currency": "USD"}, {"prompt": {"type": "string"}})
    assert p["status"] == "unknown" and p["amount"] is None and p["total"] is None
    assert p["reason"] == reason and p["detail"] and p["rate"]["amount"] == "0.1"


def test_no_row_and_models_without_text_input():
    assert price(None, SEEDANCE)["reason"] == "no_pricing_row"
    # A prompt-less image tool still quotes (partial options), per image.
    upscale = {"image_url": {"type": "string"}}
    assert price({"unit_price": 0.01, "unit": "images", "currency": "USD"}, upscale, required=("image_url",))["total"] == "0.01"


def test_image_size_union_is_a_setting_not_an_upload():
    properties = {"prompt": {"type": "string"},
                  "image_size": {"anyOf": [{"type": "string", "enum": ["square_hd", "landscape_16_9"]},
                                           {"type": "object", "properties": {"width": {"type": "integer"}}}],
                                 "default": "square_hd"}}
    fields = {f.name: f for f in schema(properties).fields}
    assert not fields["image_size"].is_file
    assert price({"unit_price": 0.025, "unit": "megapixels", "currency": "USD"}, properties)["total"] == "0.050"


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, body, *headers = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)


@pytest.fixture
def transport(monkeypatch):
    holder = {}
    original = httpx.AsyncClient

    def install(*replies):
        holder["t"] = Transport(*replies)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(holder["t"]), **kw))
        return holder["t"]
    monkeypatch.setattr(fal_pricing.asyncio, "sleep", lambda _s: _noop())
    return install


async def _noop():
    return None


async def test_rates_batch_cache_and_split_a_404_batch(transport):
    row = {"endpoint_id": "a", "unit_price": 1, "unit": "images", "currency": "USD"}
    t = transport((404, {"error": {"type": "not_found"}}), (200, {"prices": [row]}), (404, {}))
    rates = fal_pricing.FalRates()
    assert await rates.rows(["a", "retired"], "key") == {"a": row, "retired": None}
    assert [r.url.params.get_list("endpoint_id") for r in t.requests] == [["a", "retired"], ["a"], ["retired"]]
    await rates.rows(["a", "retired"], "key")
    assert len(t.requests) == 3  # both answers cached


async def test_rate_limit_retries_then_failure_is_a_reason_not_a_cache(transport):
    row = {"endpoint_id": "a", "unit_price": 1, "unit": "images", "currency": "USD"}
    t = transport((429, {}), (200, {"prices": [row]}))
    rates = fal_pricing.FalRates()
    assert (await rates.rows(["a"], "key"))["a"] == row and len(t.requests) == 2
    transport((500, {}))
    with pytest.raises(fal_pricing.PricingRequestFailed):
        await rates.rows(["b"], "key")
    assert ("b" not in {endpoint for _scope, endpoint in rates._cache})


async def test_a_long_retry_after_fails_fast_and_keeps_known_rows(transport):
    row = {"endpoint_id": "a", "unit_price": 1, "unit": "images", "currency": "USD"}
    t = transport((200, {"prices": [row]}), (429, {}, {"retry-after": "42"}))
    rates = fal_pricing.FalRates()
    await rates.rows(["a"], "key")
    with pytest.raises(fal_pricing.PricingRequestFailed):
        await rates.rows(["a", "b"], "key")
    assert len(t.requests) == 2  # no retries against a 42 s window
    with pytest.raises(fal_pricing.PricingRequestFailed):
        await rates.rows(["c"], "key")
    assert len(t.requests) == 2  # still blocked: no request at all
    assert await rates.rows(["a"], "key") == {"a": row}  # cached rows still answer
    assert rates.cached(["a", "b"], "key") == {"a": row}
