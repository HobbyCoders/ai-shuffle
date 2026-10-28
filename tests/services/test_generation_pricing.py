"""Declared pricing units and verified native cost paths; no paid provider calls."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.generation_resolver import Resolver, UNKNOWN_PRICE
from app.services.fal_schema import ModelSchema, ParamField
from tests.services.test_generation_sources import backend, CONTEXT


def schema(*fields):
    return ModelSchema("fake/model", [ParamField("prompt", is_primary_text=True), *fields], "prompt")


@pytest.mark.parametrize("unit,fields,options,prompt,expected", [
    ("generation", [], {}, None, ("1", "1")),
    ("second", [ParamField("duration", description="Output duration in seconds")], {"duration": 1.2}, None, ("2", "1")),
    ("minute", [ParamField("duration_seconds")], {"duration_seconds": 61}, None, ("2", "1")),
    ("second", [ParamField("duration", enum=["5s"])], {"duration": "5s"}, None, ("5", "1")),
    ("1K chars", [], {}, "x" * 1001, ("2", "1")),
    ("character", [], {}, "hello", ("5", "1")),
    ("megapixel", [ParamField("image_size"), ParamField("num_images")],
     {"image_size": {"width": 1024, "height": 1024}, "num_images": 2}, None, ("2", "2")),
])
def test_schema_resolves_declared_units_conservatively(unit, fields, options, prompt, expected):
    assert schema(*fields).pricing_quantity(unit, options, prompt) == tuple(map(Decimal, expected))


@pytest.mark.parametrize("unit,fields,options,prompt", [
    ("token", [], {}, "hello"),
    ("second", [ParamField("duration")], {"duration": 2}, None),
    ("second", [ParamField("duration", description="milliseconds")], {"duration": 2}, None),
    ("megapixel", [ParamField("image_size")], {"image_size": "2k"}, None),
    ("megapixel", [ParamField("image_size", enum=["square_hd", "auto"])], {"image_size": "auto"}, None),
    ("megapixel", [ParamField("image_size")], {"image_size": {"width": True, "height": 1024}}, None),
    ("1m tokens", [], {}, "hello"),
    ("compute seconds", [ParamField("duration_seconds")], {"duration_seconds": 5}, None),
    ("1K chars", [], {}, None),
    ("image", [ParamField("num_images")], {"num_images": True}, None),
])
def test_undeclared_or_ambiguous_quantity_is_not_guessed(unit, fields, options, prompt):
    with pytest.raises(ValueError):
        schema(*fields).pricing_quantity(unit, options, prompt)


@pytest.mark.parametrize("unit", ["images", " IMAGES ", "per image", "Per   Images", "videos", "generations", "meshes"])
def test_item_unit_spellings_keep_the_output_count(unit):
    declared = schema(ParamField("num_images"))
    assert declared.pricing_quantity(unit, {"num_images": 3}) == (Decimal(1), Decimal(3))


@pytest.mark.parametrize("preset,quantity", [
    ("square_hd", 2), ("square", 1), ("portrait_4_3", 1),
    ("portrait_16_9", 1), ("landscape_4_3", 1), ("landscape_16_9", 1),
])
def test_documented_image_size_presets_round_up_each_output(preset, quantity):
    declared = schema(ParamField("image_size"), ParamField("num_images"))
    assert declared.pricing_quantity("Megapixels", {"image_size": preset, "num_images": 2}) == (Decimal(quantity), Decimal(2))


@pytest.mark.parametrize("options", [{}, {"image_size": "unresolved"}])
def test_unknown_image_size_uses_largest_fully_bounded_enum(options):
    declared = schema(ParamField("image_size", enum=["square", "square_hd", "landscape_4_3"]))
    assert declared.pricing_quantity("megapixel", options) == (Decimal(2), Decimal(1))


async def test_real_fal_unit_rate_prices_choose_and_confirmed_execution(backend, monkeypatch):
    from app.services.fal_schema import schema_service
    monkeypatch.delattr(backend, "fal_price")
    declared = schema(ParamField("duration", type="number", default=6, description="Duration in seconds"))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=declared))
    original = httpx.AsyncClient
    response = {"prices": [{"endpoint_id": "fake/model", "currency": "USD", "unit": "second", "unit_price": "0.02"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kw))
    backend.context = replace(CONTEXT, session_id=None)
    resolver = Resolver(backend)
    choose = await resolver.generate("video", {"prompt": "tree"}, backend.context)
    option = next(c for c in choose["options"] if c["source"] == "fal")
    assert option["price"]["total"] == "0.12"
    args = {"prompt": "tree", "source": "fal", "model": "fake/model"}
    confirmation = await resolver.generate("video", args, backend.context)
    assert confirmation["status"] == "needs-confirmation"
    backend.node.call.assert_not_awaited()
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    done = await resolver.generate("video", {**args, **confirmation["retry"]}, backend.context)
    assert done["status"] == "completed"
    backend.node.call.assert_awaited_once()


async def test_fal_preset_defaults_are_normalized_and_priced_like_saved_options(backend, monkeypatch):
    from app.services.fal_schema import normalize_openapi, schema_service
    monkeypatch.delattr(backend, "fal_price")
    # The real fal input shape is an object-or-enum union, not a plain enum
    # ParamField. Exercise normalization and default resolution before quoting.
    declared = normalize_openapi("fake/model", {
        "components": {"schemas": {
            "FluxInput": {"type": "object", "required": ["prompt"], "properties": {
                "prompt": {"type": "string"},
                "image_size": {"anyOf": [
                    {"$ref": "#/components/schemas/ImageSize"},
                    {"type": "string", "enum": ["square_hd", "square", "landscape_4_3"]},
                ], "default": "landscape_4_3"},
                "num_images": {"type": "integer", "default": 1, "minimum": 1},
            }},
            "ImageSize": {"type": "object", "properties": {
                "width": {"type": "integer"}, "height": {"type": "integer"},
            }, "required": ["width", "height"]},
        }},
    })
    assert declared.fields[1].enum is None
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=declared))
    original = httpx.AsyncClient
    response = {"prices": [{"endpoint_id": "fake/model", "currency": "USD", "unit": "megapixels", "unit_price": "0.025"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kw))
    assert (await backend.fal_price("fake/model", {}))["total"] == "0.025"
    assert (await backend.fal_price("fake/model", {"image_size": "square_hd", "num_images": 3}))["total"] == "0.150"
    assert await backend.fal_price("fake/model", {"image_size": "unrecognized"}) == UNKNOWN_PRICE


async def test_audio_choose_has_input_dependent_character_price(backend, monkeypatch):
    from app.services.fal_schema import schema_service
    monkeypatch.delattr(backend, "fal_price")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema()))
    original = httpx.AsyncClient
    response = {"prices": [{"endpoint_id": "fake/model", "currency": "USD", "unit": "1K chars", "unit_price": "0.02"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kw))
    result = await Resolver(backend).generate("audio", {"prompt": "x" * 1001}, CONTEXT)
    assert next(c for c in result["options"] if c["source"] == "fal")["price"]["total"] == "0.04"


async def test_verified_native_video_quote_and_reported_overrun_are_preserved(backend, monkeypatch, tmp_path):
    from app.core import artifact_store, spending_limits
    from app.core.provider import native_video
    entry = (await backend.local_catalog("video", source="plan-b"))[0]
    assert entry.price["total"] == "0.02"
    assert entry.price["basis"] == "verified_native_probe"
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    submissions = []
    async def generate(account, prompt, options, *, usage):
        submissions.append(account)
        native_video._observe({"usage": {"cost_in_usd_ticks": 300_000_000}}, usage)
        return {"success": True, "data": b"fake-video", **usage}
    monkeypatch.setattr(native_video, "generate", generate)
    result = await Resolver(backend).generate("video", {"prompt": "tree", "source": "plan-b"}, CONTEXT)
    assert submissions == ["plan-b"]
    assert result["status"] == "completed"
    assert result["price"] == {"currency": "USD", "unit": "item", "amount": "0.03", "total": "0.03", "status": "reported"}
    assert spending_limits.status()["spent_today_usd"] == 0.03
    assert "/video.mp4?" in result["result"]["video_url"]


def test_verified_probe_quote_does_not_extend_when_capabilities_expand(monkeypatch):
    from app.core.native_generation_settings import NativeVideoSettings, capabilities
    from app.core.provider import native_video
    options = NativeVideoSettings(**capabilities("grok")["video"]["verified_combination"])
    monkeypatch.setattr(native_video, "validate_generation", lambda *args: None)
    assert native_video.estimate_price(options.model_copy(update={"duration": 2})) == UNKNOWN_PRICE


async def test_named_model_is_not_ambiguous_with_unavailable_api_accounts(backend, monkeypatch):
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    result = await Resolver(backend).generate("image", {"prompt": "tree", "model": "fake/model"}, replace(CONTEXT, session_id=None))
    assert result["status"] == "completed"
    assert result["choice"]["source"] == "fal"
    backend.node.call.assert_awaited_once()


async def test_openai_and_xai_api_images_still_refuse_truthfully(backend):
    # Plan images (ChatGPT/SuperGrok) are priced "included" on the verified
    # route and run (#379); test_generation_sources covers that path and the
    # refusal when a plan route has no known price.
    for source in ("api-a", "api-b"):
        result = await Resolver(backend).generate("image", {"prompt": "tree", "source": source, "confirmed": True}, CONTEXT)
        assert result["status"] == "refused"
        assert result["price"] == UNKNOWN_PRICE
    backend.node.call.assert_not_awaited()
