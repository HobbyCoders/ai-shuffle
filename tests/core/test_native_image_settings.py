"""Subscription-native settings and xAI HTTP boundary (no live credentials)."""
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from fluid.tools.registry import ToolCtx

from app.core.native_generation_settings import NativeImageSettings, capabilities, load, save, validate
from app.core.provider import native_images, supergrok_images


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(out, format="JPEG")
    return out.getvalue()


def test_settings_round_trip_and_separation(monkeypatch):
    from app.db import database
    stored = {}
    monkeypatch.setattr(database, "get_system_setting", stored.get)
    monkeypatch.setattr(database, "set_system_setting", stored.__setitem__)
    monkeypatch.setattr(database, "get_model_provider", lambda key, **kw: {"provider_type": key})
    assert load("chatgpt").image.model_dump() == dict(model="default", size="auto", quality="auto", format="png")
    settings = load("grok")
    assert settings.image.model_dump() == dict(model="grok-imagine-image", size="1k", quality=None, format="jpeg")
    save("grok", settings)
    assert load("grok") == settings
    assert set(stored) == {"native_generation:grok"}
    assert capabilities("grok")["image"]["verified_combination"]["model"] == "grok-imagine-image"


@pytest.mark.parametrize("field,value", [("model", "grok-imagine-image-2.0"), ("size", "2k"), ("quality", "high"), ("format", "png")])
def test_reject_unproven_options(field, value):
    options = dict(model="grok-imagine-image", size="1k", quality=None, format="jpeg")
    options[field] = value
    with pytest.raises(ValueError):
        validate("grok", NativeImageSettings(**options))


@pytest.mark.parametrize("output", ["jpeg", "png"])
async def test_grok_handler_exact_http_body_image_bytes_and_no_fallback(monkeypatch, tmp_path, output):
    from app.core.provider import fluid_credentials
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    monkeypatch.setattr(supergrok_images, "subscription_kind", AsyncMock(return_value="grok"))
    monkeypatch.setattr(native_images, "load_native_settings", AsyncMock(return_value=NativeImageSettings(model="grok-imagine-image", size="1k", quality=None, format="jpeg")))
    from tests.core.test_native_image_tool import png
    data, requests = (jpeg() if output == "jpeg" else png()), []
    def boundary(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary)) as http:
        monkeypatch.setattr(supergrok_images, "http_client", lambda: http)
        engine = SimpleNamespace(provider=SimpleNamespace(name="openai_responses"), model="grok-chat", record_auxiliary_usage=AsyncMock())
        result = await native_images.make_handler(lambda: engine, lambda: "s", provider_id_supplier=lambda: "grok-row")({"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path)))
    assert not result.is_error, result.text
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.x.ai/v1/images/generations"
    assert requests[0].headers["authorization"] == "Bearer test-private-token"
    assert requests[0].headers["x-xai-token-auth"] == "xai-grok-cli"
    assert json.loads(requests[0].content) == {"model": "grok-imagine-image", "prompt": "A tree", "n": 1, "resolution": "1k", "aspect_ratio": "1:1", "response_format": "b64_json"}
    assert result.images[0].media_type == f"image/{output}"
    path = next((tmp_path / "generated-images").iterdir())
    assert path.suffix == (".jpg" if output == "jpeg" else ".png") and path.read_bytes() == data
    assert "test-private-token" not in result.text
    engine.record_auxiliary_usage.assert_awaited_once()


@pytest.mark.parametrize("status,label", [(401, "expired"), (403, "entitled"), (429, "rate limit"), (400, "refused"), (500, "unavailable")])
async def test_sanitized_errors_no_retries(monkeypatch, caplog, status, label):
    from app.core.provider import fluid_credentials
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    monkeypatch.setattr(supergrok_images, "subscription_kind", AsyncMock(return_value="grok"))
    calls = []
    def boundary(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "test-private-token", "code": "test-private-token"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary)) as http:
        monkeypatch.setattr(supergrok_images, "http_client", lambda: http)
        items, warning = await supergrok_images.generate("row", "tree", NativeImageSettings(model="grok-imagine-image", size="1k", quality=None, format="jpeg"))
    assert not items and label in warning
    assert len(calls) == 1
    assert "test-private-token" not in warning + caplog.text


async def test_chatgpt_settings_preserve_exact_http_tool(monkeypatch, tmp_path):
    from app.db import database
    from fluid.providers.openai_responses.client import OpenAIResponsesClient
    from tests.core.test_native_image_tool import png, stream_response
    monkeypatch.setattr(database, "get_system_setting", lambda key: None)
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: {"provider_type": "chatgpt"})
    requests = []
    def boundary(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=stream_response(png()), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary)) as http:
        provider = OpenAIResponsesClient(api_key="test-private-token", base_url="https://chatgpt.example/responses", http_client=http)
        engine = SimpleNamespace(provider=provider, model="chat-model", record_auxiliary_usage=AsyncMock())
        result = await native_images.make_handler(lambda: engine, lambda: "s", provider_id_supplier=lambda: "chatgpt-row")({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
    assert not result.is_error, result.text
    assert len(requests) == 1
    assert requests[0]["tools"] == [{"type": "image_generation", "output_format": "png", "partial_images": 0}]
    assert requests[0]["tool_choice"] == {"type": "image_generation"}
    assert requests[0]["model"] == "chat-model"


async def test_api_validation_persistence_and_owner_boundary(monkeypatch):
    from fastapi import FastAPI
    from app.api.auth import require_auth, require_admin
    from app.api.settings_native_generation import router
    from app.db import database
    stored = {}
    monkeypatch.setattr(database, "get_system_setting", stored.get)
    monkeypatch.setattr(database, "set_system_setting", stored.__setitem__)
    monkeypatch.setattr(database, "get_model_provider", lambda key, **kw: {"id": key, "provider_type": "grok"} if key == "grok-row" else None)
    monkeypatch.setattr(database, "get_all_model_providers", lambda **kw: [{"id": "grok-row", "provider_type": "grok"}])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/settings")
    app.dependency_overrides[require_auth] = lambda: "reader"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/settings/native-generation")
        assert response.status_code == 200
        settings = response.json()["accounts"][0]["settings"]
        denied = await client.put("/api/v1/settings/native-generation/grok-row", json=settings)
        assert denied.status_code in (401, 403)
        app.dependency_overrides[require_admin] = lambda: "owner"
        updated = await client.put("/api/v1/settings/native-generation/grok-row", json=settings)
        assert updated.status_code == 200
        assert updated.json()["settings"] == settings
        assert (await client.get("/api/v1/settings/native-generation/grok-row")).json()["settings"] == settings
        for body in (settings | {"image": settings["image"] | {"size": "2k"}}, settings | {"image": settings["image"] | {"format": "png"}}, settings | {"extra": True}, settings | {"image": settings["image"] | {"model": None}}):
            assert (await client.put("/api/v1/settings/native-generation/grok-row", json=body)).status_code == 422
        assert (await client.get("/api/v1/settings/native-generation/fal")).status_code == 422
    assert set(stored) == {"native_generation:grok-row"}


@pytest.mark.parametrize("kind", ["grok", "chatgpt"])
async def test_invalid_subscription_override_never_calls_provider(monkeypatch, tmp_path, kind):
    from app.db import database
    monkeypatch.setattr(database, "get_system_setting", lambda key: None)
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: {"provider_type": kind})
    provider = SimpleNamespace(name="openai_responses", stream=AsyncMock())
    engine = SimpleNamespace(provider=provider, model="chat-model")
    result = await native_images.make_handler(lambda: engine, lambda: "s", provider_id_supplier=lambda: "row")({"prompt": "tree", "size": "2k"}, ToolCtx(cwd=str(tmp_path)))
    assert result.is_error
    provider.stream.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("provider_type,mode,expected", [("grok", None, "grok"), ("custom", "grok", "grok"), ("chatgpt", None, "chatgpt"), ("xai", "api_key", None)])
async def test_routing_uses_subscription_row_not_chat_model(monkeypatch, provider_type, mode, expected):
    from app.db import database
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: {"provider_type": provider_type, "auth_mode": mode})
    assert await supergrok_images.subscription_kind("row") == expected


async def test_grok_resolver_rejects_paid_api_key_row(monkeypatch):
    from app.db import database
    from app.core.provider import fluid_credentials
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: {"provider_type": "xai", "auth_mode": "api_key"})
    credential = AsyncMock()
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", credential)
    items, warning = await supergrok_images.generate("row", "tree", NativeImageSettings(model="grok-imagine-image", size="1k", quality=None, format="jpeg"))
    assert not items and "expired" in warning
    credential.assert_not_called()


def test_jpeg_and_png_artifacts_detect_bytes_not_claimed_format(monkeypatch):
    from app.core import artifact_store
    from tests.core.test_native_image_tool import png
    captured = []
    monkeypatch.setattr(artifact_store, "store_bytes", lambda data, **kw: captured.append((data, kw)) or {"hash": "digest"})
    for data, mime, filename in ((jpeg(), "image/jpeg", "native-image.jpg"), (png(), "image/png", "native-image.png")):
        native_images._save_artifacts("session", [{"result": base64.b64encode(data).decode(), "output_format": "wrong"}])
        assert captured[-1] == (data, dict(mime=mime, filename=filename, created_by_session="session"))
    for data in (jpeg()[:-3], b"not an image", b"\xff\xd8\xff\xd9"):
        with pytest.raises(ValueError):
            native_images._decode_image({"result": base64.b64encode(data).decode()})


async def test_expired_resolver_and_transport_errors_are_redacted(monkeypatch, caplog):
    from app.core.provider import fluid_credentials
    from app.core.provider.fluid_credentials import ProviderCredentialError
    monkeypatch.setattr(supergrok_images, "subscription_kind", AsyncMock(return_value="grok"))
    options = NativeImageSettings(model="grok-imagine-image", size="1k", quality=None, format="jpeg")
    for error in (ProviderCredentialError("test-private-token"), RuntimeError("test-private-token")):
        monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(side_effect=error))
        items, warning = await supergrok_images.generate("row", "tree", options)
        assert not items and "test-private-token" not in warning + caplog.text
    assert "test-private-token" not in native_images._error(400, {"message": json.dumps({"error": {"code": "test-private-token"}})})


def test_supergrok_is_not_a_direct_image_model():
    from app.core.provider import subscription_images as sub
    assert not sub.is_subscription_model("supergrok-subscription")
    assert sub.is_subscription_model("chatgpt-subscription")
    assert sub.is_subscription_model("chatgpt-subscription/gpt-image-1")


async def test_content_refusal_and_cancellation_never_retry(monkeypatch):
    import asyncio
    from app.core.provider import fluid_credentials
    monkeypatch.setattr(supergrok_images, "subscription_kind", AsyncMock(return_value="grok"))
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    options = NativeImageSettings(model="grok-imagine-image", size="1k", quality=None, format="jpeg")
    requests = []
    def boundary(request):
        requests.append(request)
        return httpx.Response(403, json={"error": {"code": "content_policy_violation", "message": "test-private-token"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary)) as http:
        monkeypatch.setattr(supergrok_images, "http_client", lambda: http)
        items, warning = await supergrok_images.generate("row", "tree", options)
    assert not items and "refused the image content" in warning
    assert "test-private-token" not in warning and len(requests) == 1
    def cancelled(request):
        raise asyncio.CancelledError()
    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as http:
        monkeypatch.setattr(supergrok_images, "http_client", lambda: http)
        with pytest.raises(asyncio.CancelledError):
            await supergrok_images.generate("row", "tree", options)

