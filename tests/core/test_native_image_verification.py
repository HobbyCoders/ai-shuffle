"""The reviewed OpenAI/xAI API-image records from the #541 live probes.

Fake credentials and a mock transport only; no provider is contacted.
"""
import base64
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import native_generation_settings as native, native_image_verification as verification
from app.core.generation_resolver import CONFIRM_ABOVE_USD, Resolver
from app.services.generation_sources import Sources
from tests.services.test_generation_sources import CONTEXT, backend  # noqa: F401

PROMPT = "A small blue square on a white background"
EXPECTED = {
    "openai": dict(model="gpt-image-2.5-sunburst-2026-09-08", size="1024x1024", quality="low", format="png"),
    "xai": dict(model="grok-imagine-image", size="1k", quality=None, format="jpeg"),
}


def test_only_the_probed_combinations_are_recorded() -> None:
    assert set(verification.VERIFIED_API_IMAGES) == {"openai", "xai"}
    for kind, settings in EXPECTED.items():
        record = verification.verified(kind)
        assert record is not None and record.settings() == settings
        assert f"generation-379.{kind}-image.r1028" in record.evidence and "#541" in record.evidence
        assert native.image_default(kind) == settings
    # Plan defaults are unchanged and unknown kinds have no record.
    assert native.image_default("chatgpt")["model"] == "default"
    assert verification.verified("grok") is None and verification.verified("fal") is None


@pytest.mark.parametrize("kind,locked", [
    ("openai", {"model": "default", "size": "auto", "quality": "auto"}),
    ("xai", {"model": "grok-imagine-image-2.0", "size": "2k", "format": "png"}),
])
def test_capability_fields_offer_only_the_recorded_values(kind: str, locked: dict[str, str]) -> None:
    caps = native.capabilities(kind)
    image = caps["image"]
    assert image["available"] and image["verified_combination"] == EXPECTED[kind]
    assert image["route"] == "images/generations"
    assert image["quality_visible"] is (kind == "openai")
    for field, options in image["fields"].items():
        selectable = [o["value"] for o in options if o["selectable"]]
        expected = EXPECTED[kind][field]
        assert selectable == ([] if expected is None else [expected])
    for field, value in locked.items():
        option = next(o for o in image["fields"][field] if o["value"] == value)
        assert option["selectable"] is False and option["reason"] == "not yet verified"
    assert not caps["video"]["available"]


def test_prices_bound_output_and_prompt_input() -> None:
    openai, xai = verification.verified("openai"), verification.verified("xai")
    # $0.02 output bound + $5/M over (prompt bytes + 1024 overhead) tokens.
    tokens = len(PROMPT.encode()) + 1024
    assert Decimal(openai.price(PROMPT)["total"]) == Decimal("0.02") + Decimal(5) * tokens / Decimal(1000000)
    # xAI reported $0.02 per image including the prompt: no separate input rate.
    assert xai.price(PROMPT)["total"] == xai.price()["total"] == "0.02"
    for record in (openai, xai):
        quote = record.price(PROMPT)
        assert quote["currency"] == "USD" and quote["status"] == "estimated"
        assert Decimal(quote["total"]) < CONFIRM_ABOVE_USD
        # The catalog preview quotes the longest supported prompt, never less.
        assert Decimal(record.price()["total"]) >= Decimal(quote["total"])


@pytest.mark.parametrize("kind,change", [
    ("openai", {"quality": "high"}), ("openai", {"size": "1536x1024"}),
    ("openai", {"format": "jpeg"}), ("openai", {"model": "gpt-image-1"}),
    ("xai", {"size": "2k"}), ("xai", {"format": "png"}),
    ("xai", {"model": "grok-imagine-image-2.0"}), ("xai", {"quality": "low"}),
])
def test_any_other_option_is_refused(kind: str, change: dict[str, str]) -> None:
    native.validate(kind, native.NativeImageSettings(**EXPECTED[kind]))
    with pytest.raises(ValueError):
        native.validate(kind, native.NativeImageSettings(**{**EXPECTED[kind], **change}))


@pytest.mark.parametrize("kind,account", [("openai", "api-a"), ("xai", "api-b")])
async def test_admission_quotes_record_and_submits_exact_options_once(
    backend: Sources, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
    kind: str, account: str,
) -> None:
    from app.core import artifact_store
    from app.core.provider import api_images
    from app.db import database
    from tests.core.test_native_image_tool import png

    record = verification.verified(kind)
    row = {"id": account, "provider_type": kind, "auth_mode": "api_key", "enabled": True,
           "base_url": verification.ENDPOINTS[kind].rsplit("/images/", 1)[0],
           "auth_token": "fake-api-key"}
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert str(request.url) == verification.ENDPOINTS[kind]
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

    monkeypatch.setattr(
        api_images, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(send)),
    )
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(backend, "reserve", reserve)
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    output = await Resolver(backend).generate("image", {"prompt": PROMPT, "source": account}, CONTEXT)
    # Below the confirmation threshold: reserved at the record price and run once.
    assert output["status"] == "completed", output
    assert output["price"] == record.price(PROMPT)
    assert reserve.await_args.args[0].price == record.price(PROMPT)
    expected = {"model": record.model, "prompt": PROMPT, "n": 1}
    if kind == "openai":
        expected.update(size="1024x1024", quality="low", output_format="png")
    else:
        expected.update(resolution="1k", aspect_ratio="1:1", response_format="b64_json")
    assert calls == [expected]
    # An option outside the record is refused before any second submission.
    refused = await Resolver(backend).generate(
        "image", {"prompt": PROMPT, "source": account, "options": {"size": "2k"}}, CONTEXT,
    )
    assert refused["status"] == "refused" and len(calls) == 1
