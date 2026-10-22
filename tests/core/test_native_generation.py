"""Account-scoped Native generation settings and bounded video producing path."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fluid.tools.registry import ToolCtx

from app.core.native_generation_settings import NativeGenerationSettings, NativeVideoSettings, account_view, load, save
from app.core.provider import native_video, supergrok_images


def accounts(monkeypatch):
    from app.core import xai_token_refresh
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", AsyncMock(return_value=False))
    from app.db import database
    rows = {"grok-a": {"id": "grok-a", "name": "Plan A", "provider_type": "grok"},
            "grok-b": {"id": "grok-b", "provider_type": "grok"},
            "chatgpt-a": {"id": "chatgpt-a", "provider_type": "chatgpt"},
            "openai-api": {"id": "openai-api", "provider_type": "openai", "auth_mode": "api_key"},
            "xai-api": {"id": "xai-api", "provider_type": "xai", "auth_mode": "api_key"}}
    stored = {}
    monkeypatch.setattr(database, "get_model_provider", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(database, "get_all_model_providers", lambda **kw: list(rows.values()))
    monkeypatch.setattr(database, "get_system_setting", stored.get)
    monkeypatch.setattr(database, "set_system_setting", stored.__setitem__)
    return rows, stored


def test_account_settings_group_slots_and_locked_options(monkeypatch):
    rows, stored = accounts(monkeypatch)
    value = load("grok-a")
    assert value.image.model == "grok-imagine-image"
    assert value.image.quality is None
    assert value.video.model_dump() == dict(model="grok-imagine-video-1.5-lite", duration=1, resolution="480p", aspect_ratio="16:9", generate_audio=False)
    save("grok-a", value)
    assert set(stored) == {"native_generation:grok-a"}
    assert account_view("grok-b")["provider_id"] == "grok-b"
    caps = account_view("grok-a")["capabilities"]
    assert not caps["image"]["quality_visible"]
    assert next(o for o in caps["image"]["fields"]["model"] if o["value"] == "grok-imagine-image-2.0")["reason"] == "not yet verified"
    assert next(o for o in caps["image"]["fields"]["size"] if o["value"] == "2k")["selectable"] is False
    assert not account_view("chatgpt-a")["capabilities"]["video"]["available"]
    for key in ("openai-api", "xai-api"):
        view = account_view(key)
        assert not view["capabilities"]["image"]["available"]
        assert view["settings"] == {"image": None, "video": None}
        assert "auth_token" not in view
    for field, value in (("duration", 2), ("resolution", "720p"), ("aspect_ratio", "1:1"), ("generate_audio", True)):
        settings = load("grok-a").model_dump()
        settings["video"][field] = value
        with pytest.raises(ValueError):
            save("grok-a", NativeGenerationSettings(**settings))
    stored["native_images_chatgpt"] = json.dumps(dict(model="default", size="auto", quality="auto", format="png"))
    assert load("chatgpt-a").image.format == "png"
    assert "native_generation:chatgpt-a" not in stored


async def test_video_handler_exact_submission_poll_download_cost_and_artifact(monkeypatch, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="httpx")
    from app.core import artifact_store
    from app.core.provider import fluid_credentials
    accounts(monkeypatch)
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    requests, saved = [], []
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    def boundary(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "job-1", "usage": {"cost_in_usd_ticks": 200000000}})
        if request.url.path == "/v1/videos/job-1":
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://media.x.ai/clip.mp4?private=signed-token", "duration": 1}})
        assert "authorization" not in request.headers and "x-xai-token-auth" not in request.headers
        return httpx.Response(200, content=mp4)
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    monkeypatch.setattr(native_video, "media_transport", lambda: httpx.MockTransport(boundary))
    monkeypatch.setattr(native_video, "POLL_INTERVAL", 0)
    monkeypatch.setattr(artifact_store, "store_bytes", lambda data, **kw: saved.append((data, kw)) or {"hash": "digest"})
    engine = SimpleNamespace(provider=SimpleNamespace(name="openai_responses"), model="grok-chat", record_auxiliary_usage=AsyncMock())
    result = await native_video.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "circle"}, ToolCtx(cwd=str(tmp_path)))
    assert not result.is_error, result.text
    output = json.loads(result.text)
    assert output["reported_cost_usd"] == "0.02" and output["cost_in_usd_ticks"] == 200000000
    assert result.result_fields["reported_cost_usd"] == "0.02"
    assert output["video_url"] == "/api/v1/artifacts/digest/video.mp4?session_id=session"
    assert saved == [(mp4, dict(mime="video/mp4", filename="native-video.mp4", created_by_session="session"))]
    assert json.loads(requests[0].content) == {"model": "grok-imagine-video-1.5-lite", "prompt": "circle", "duration": 1, "resolution": "480p", "aspect_ratio": "16:9", "generate_audio": False}
    assert str(requests[0].url) == "https://api.x.ai/v1/videos/generations"
    assert requests[0].headers["authorization"] == "Bearer test-private-token"
    assert len(requests) == 3
    assert "test-private-token" not in result.text + caplog.text
    assert "signed-token" not in result.text + caplog.text
    usage = engine.record_auxiliary_usage.call_args.args[0]
    assert usage["reported_video_cost_usd"] == "0.02"


async def test_video_bounded_timeout_does_not_resubmit(monkeypatch):
    from app.core.provider import fluid_credentials
    accounts(monkeypatch)
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    requests = []
    def boundary(request):
        requests.append(request)
        return httpx.Response(200 if request.method == "POST" else 202, json={"request_id": "job-1", "status": "pending"})
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    monkeypatch.setattr(native_video, "POLL_INTERVAL", 0)
    monkeypatch.setattr(native_video, "MAX_POLLS", 2)
    result = await native_video.generate("grok-a", "circle", load("grok-a").video)
    assert "timed out" in result["error"] and "not resubmitted" in result["error"]
    assert "Do not call again without the user's go-ahead." in result["error"]
    assert [r.method for r in requests] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("status,word", [(401, "expired"), (403, "entitled"), (429, "rate limit"), (400, "refused"), (500, "unavailable")])
async def test_video_errors_redact_credentials_and_never_resubmit(monkeypatch, caplog, status, word):
    import logging
    caplog.set_level(logging.INFO, logger="httpx")
    from app.core.provider import fluid_credentials
    accounts(monkeypatch)
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    calls = []
    def boundary(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "test-private-token", "code": "test-private-token"}})
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    result = await native_video.generate("grok-a", "circle", load("grok-a").video)
    assert not result["success"] and word in result["error"]
    assert len(calls) == 1 and "test-private-token" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("url", ["http://media.x.ai/clip.mp4", "https://media.x.ai.evil/clip.mp4", "https://127.0.0.1/clip.mp4", "https://user:password@media.x.ai/clip.mp4", "https://media.x.ai:444/clip.mp4"])
async def test_video_media_url_allowlist(url):
    with pytest.raises(ValueError):
        await native_video._download(url)


async def test_video_deadline_and_cancel_preserve_reported_cost(monkeypatch):
    from app.core.provider import fluid_credentials
    accounts(monkeypatch)
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", AsyncMock(return_value="test-private-token"))
    calls = []
    def boundary(request):
        calls.append(request)
        return httpx.Response(200, json={"request_id": "job-1", "usage": {"cost_in_usd_ticks": 200000000}})
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    monkeypatch.setattr(native_video, "MAX_WAIT", 0.01)
    monkeypatch.setattr(native_video, "POLL_INTERVAL", 1)
    result = await native_video.generate("grok-a", "circle", load("grok-a").video)
    assert not result["success"] and "timed out" in result["error"]
    assert result["reported_cost_usd"] == "0.02" and len(calls) == 1
    def cancelled(request):
        raise asyncio.CancelledError()
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(cancelled)))
    with pytest.raises(asyncio.CancelledError):
        await native_video.generate("grok-a", "circle", load("grok-a").video)


async def test_locked_account_cannot_generate_native_image_or_video(monkeypatch, tmp_path):
    from app.core.provider import native_images
    accounts(monkeypatch)
    engine = SimpleNamespace(provider=SimpleNamespace(name="openai_responses"), model="paid-model")
    for provider_id in ("openai-api", "xai-api"):
        image = await native_images.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: provider_id)({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
        video = await native_video.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: provider_id)({"prompt": "circle"}, ToolCtx(cwd=str(tmp_path)))
        assert image.is_error and video.is_error
        assert "not yet verified" in image.text + video.text


def test_legacy_grok_settings_are_read_compatible_and_account_local(monkeypatch):
    _, stored = accounts(monkeypatch)
    stored["native_images_grok"] = json.dumps(dict(model="grok-imagine-image", size="1k", quality="default", format="jpeg"))
    before = dict(stored)
    assert load("grok-a").image.quality is None
    assert stored == before
    disabled = NativeGenerationSettings(image=None, video=None)
    save("grok-a", disabled)
    assert load("grok-a") == disabled
    assert load("grok-b").image.model == "grok-imagine-image"


@pytest.mark.parametrize("kind,exposed", [("grok", True), ("chatgpt", False), ("xai", False), ("openai", False)])
async def test_video_registry_is_subscription_specific(monkeypatch, kind, exposed):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE, PROACTIVE_DENIED_TOOLS
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda key: {"id": "account", "provider_type": kind})
    adapter = ha.HarnessAdapter()
    adapter._provider_kind = "openai_responses"
    options = SessionOptions(model="unrelated-model", extra={"provider_id": "account", "session_mode": "chat"})
    adapter._options = options
    registry = await adapter._build_registry(options, native=True)
    assert ("GenerateNativeVideo" in {s.name for s in registry.list_tools(loaded_only=False)}) is exposed
    assert "GenerateNativeVideo" in CHAT_MODE_TOOL_SURFACE
    assert "GenerateNativeVideo" in PROACTIVE_DENIED_TOOLS
