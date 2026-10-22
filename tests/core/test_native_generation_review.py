"""Regression coverage for the Native generation review; no real providers."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fluid.tools.registry import ToolCtx

from app.core.native_generation_settings import account_view, list_accounts, load, save
from app.core.provider import native_images, native_video, supergrok_images
from tests.core.test_native_generation import accounts
from tests.core.test_native_image_settings import jpeg


@pytest.mark.parametrize("overrides", [{}, {"image_model": "unproven"}, {"size": "2048x2048"}, {"quality": "high"}])
async def test_no_account_never_sends_an_image_request(tmp_path, overrides):
    from fluid.providers.openai_responses.client import OpenAIResponsesClient
    calls = []
    def boundary(request):
        calls.append(request)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary)) as http:
        engine = SimpleNamespace(provider=OpenAIResponsesClient(api_key="test-env-key", http_client=http), model="chat", record_auxiliary_usage=AsyncMock())
        result = await native_images.make_handler(lambda: engine, lambda: "session")({"prompt": "tree", **overrides}, ToolCtx(cwd=str(tmp_path)))
    assert not calls
    assert result.is_error and "not yet verified; no request was made" in result.text


@pytest.mark.parametrize("key,raw,image_enabled,video_enabled", [
    ("native_generation:grok-a", "{broken", True, False),
    ("native_generation:grok-a", '{"image":null,"video":{"model":"unproven"}}', False, False),
    ("native_generation:grok-a", '{"image":{"model":"grok-imagine-image-2.0","size":"2k","format":"png"},"video":null}', True, False),
    ("native_generation:grok-a", '{"image":null}', False, False),
    ("native_generation:grok-a", '{"video":null}', True, False),
    ("native_generation:grok-a", 'null', True, False),
    ("native_generation:grok-a", '[]', True, False),
    ("native_images_grok", "{broken", True, False),
    ("native_images_grok", "", True, False),
    ("native_images_grok", '{"model":"unproven","size":"2k","format":"png"}', True, False),
])
def test_stale_account_falls_back_and_does_not_break_list(monkeypatch, key, raw, image_enabled, video_enabled):
    _, stored = accounts(monkeypatch)
    stored[key] = raw
    before = dict(stored)
    views = {row["provider_id"]: row for row in list_accounts()["accounts"]}
    view = views["grok-a"]
    assert view["stale"] is True and "reason" in view
    assert "unproven" not in view["reason"] and "broken" not in view["reason"]
    assert (view["settings"]["image"] is not None) is image_enabled
    assert (load("grok-a").video is not None) is video_enabled
    if image_enabled:
        assert view["settings"]["image"]["model"] == "grok-imagine-image"
    if video_enabled:
        assert load("grok-a").video.duration == 1
    assert stored == before
    if key.startswith("native_generation:"):
        assert not views["grok-b"].get("stale", False)




@pytest.mark.parametrize("disabled", ["image", "video"])
async def test_stale_peer_group_never_reenables_a_disabled_tool(monkeypatch, tmp_path, disabled):
    _, stored = accounts(monkeypatch)
    value = load("grok-a").model_dump()
    value[disabled] = None
    value["video" if disabled == "image" else "image"] = {"model": "unproven"}
    stored["native_generation:grok-a"] = json.dumps(value)
    engine = SimpleNamespace(provider=SimpleNamespace(name="openai_responses"), model="chat", record_auxiliary_usage=AsyncMock())
    image = AsyncMock()
    video = AsyncMock()
    monkeypatch.setattr(supergrok_images, "generate", image)
    monkeypatch.setattr(native_video, "generate", video)
    handler = native_images.make_handler if disabled == "image" else native_video.make_handler
    result = await handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
    assert result.is_error and "disabled" in result.text
    image.assert_not_called()
    video.assert_not_called()
    assert account_view("grok-a")["stale"] is True


async def test_disabled_video_with_corrupt_row_never_calls_provider(monkeypatch, tmp_path):
    _, stored = accounts(monkeypatch)
    settings = load("grok-a")
    settings.video = None
    save("grok-a", settings)
    assert json.loads(stored["native_generation:grok-a"])["video"] is None
    stored["native_generation:grok-a"] = "{broken"
    video = AsyncMock()
    monkeypatch.setattr(native_video, "generate", video)
    engine = SimpleNamespace(record_auxiliary_usage=AsyncMock())
    result = await native_video.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
    assert result.is_error and "disabled" in result.text
    video.assert_not_called()
    view = account_view("grok-a")
    assert view["stale"] is True
    assert view["reason"] == "Stored Native generation settings are invalid or no longer verified; disabled groups preserved and invalid groups reset safely."


@pytest.mark.parametrize("invalid", ["image", "video"])
def test_stale_group_keeps_valid_peer_unchanged(monkeypatch, invalid):
    _, stored = accounts(monkeypatch)
    value = load("grok-a").model_dump()
    valid_peer = value["video" if invalid == "image" else "image"]
    value[invalid] = {"model": "unproven"}
    stored["native_generation:grok-a"] = json.dumps(value)
    view = account_view("grok-a")
    assert view["settings"]["video" if invalid == "image" else "image"] == valid_peer
    assert view["stale"] is True
    if invalid == "video":
        assert view["settings"]["video"] is None


async def test_video_save_failure_keeps_reported_cost_in_result_fields(monkeypatch, tmp_path):
    from app.core import artifact_store
    accounts(monkeypatch)
    def generated(provider_id, prompt, options, *, usage):
        usage.update(cost_in_usd_ticks=200000000, reported_cost_usd="0.02")
        return {"success": True, "data": b"test-video", **usage}
    monkeypatch.setattr(native_video, "generate", AsyncMock(side_effect=generated))
    def failed_save(*args, **kwargs):
        raise OSError("test-private-storage-detail")
    monkeypatch.setattr(artifact_store, "store_bytes", failed_save)
    engine = SimpleNamespace(record_auxiliary_usage=AsyncMock())
    result = await native_video.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "circle"}, ToolCtx(cwd=str(tmp_path)))
    assert result.is_error and "safely saved" in result.text
    assert result.result_fields["reported_cost_usd"] == "0.02"
    assert result.result_fields["cost_in_usd_ticks"] == 200000000
    assert json.loads(result.text) == result.result_fields
    assert "test-private-storage-detail" not in result.text
    engine.record_auxiliary_usage.assert_awaited_once()


async def test_stale_settings_image_uses_verified_default_and_video_refuses(monkeypatch, tmp_path):
    _, stored = accounts(monkeypatch)
    stored["native_generation:grok-a"] = "{broken"
    image = AsyncMock(return_value=([{"result": base64.b64encode(jpeg()).decode()}], None))
    video = AsyncMock(return_value={"success": False, "error": "test stop"})
    monkeypatch.setattr(supergrok_images, "generate", image)
    monkeypatch.setattr(native_video, "generate", video)
    engine = SimpleNamespace(provider=SimpleNamespace(name="openai_responses"), model="chat", record_auxiliary_usage=AsyncMock())
    outcome = await native_images.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
    assert not outcome.is_error, outcome.text
    result = await native_video.make_handler(lambda: engine, lambda: "session", provider_id_supplier=lambda: "grok-a")({"prompt": "tree"}, ToolCtx(cwd=str(tmp_path)))
    assert image.call_args.args[2].model == "grok-imagine-image"
    assert result.is_error and "disabled" in result.text
    video.assert_not_called()


@pytest.mark.parametrize("provider_id,exposed", [(None, False), ("openai-api", False), ("xai-api", False), ("grok-a", True), ("chatgpt-a", True)])
async def test_image_registration_requires_verified_account(monkeypatch, provider_id, exposed):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    rows, _ = accounts(monkeypatch)
    monkeypatch.setattr(ha, "_lookup_provider_row", rows.get)
    adapter = ha.HarnessAdapter()
    adapter._provider_kind = "openai_responses"
    options = SessionOptions(model="unrelated", extra={"provider_id": provider_id, "session_mode": "chat"})
    adapter._options = options
    registry = await adapter._build_registry(options, native=True)
    assert ("GenerateNativeImage" in {s.name for s in registry.list_tools(loaded_only=False)}) is exposed


async def test_video_media_endpoint_is_inline_range_and_session_authorized(monkeypatch, tmp_path):
    from app.api import artifacts
    from app.api.auth import require_auth
    from app.core import artifact_access
    digest, data = "a" * 64, b"\x00\x00\x00\x18ftypisom" + b"0" * 32
    path = tmp_path / "video.mp4"
    path.write_bytes(data)
    app = FastAPI()
    app.include_router(artifacts.router)
    monkeypatch.setattr(artifacts, "get_artifact", lambda key: {"mime": "video/mp4", "filename": "native-video.mp4"})
    monkeypatch.setattr(artifacts, "artifact_path", lambda key: path)
    authorized = []
    def authorize(request, row, key, session_id):
        authorized.append(session_id)
        if session_id != "owner-session":
            raise HTTPException(404)
    monkeypatch.setattr(artifact_access, "authorize_session_artifact", authorize)
    app.dependency_overrides[artifacts._require_auth_or_signed] = lambda: "test-auth"
    url = f"/api/v1/artifacts/{digest}/video.mp4"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(url, params={"session_id": "owner-session"}, headers={"Range": "bytes=0-11"})
        assert response.status_code == 206
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["content-range"] == f"bytes 0-11/{len(data)}"
        assert response.content == data[:12]
        assert (await client.get(url, params={"session_id": "other-session"})).status_code == 404
        app.dependency_overrides.clear()
        # No browser/cluster credential is accepted just because this is a video.
        assert (await client.get(url)).status_code in (401, 403)
    assert authorized == ["owner-session", "other-session"]


@pytest.mark.parametrize("persistent", [False, True])
async def test_image_401_refresh_once_with_new_credential(monkeypatch, persistent):
    from app.core import xai_token_refresh
    accounts(monkeypatch)
    resolver = AsyncMock(side_effect=["old-test-token", "new-test-token"])
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(supergrok_images, "resolve_credential", resolver)
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", refresh)
    calls = []
    def boundary(request):
        calls.append(request)
        if len(calls) == 1 or persistent:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(jpeg()).decode()}]})
    monkeypatch.setattr(supergrok_images, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    items, error = await supergrok_images.generate("grok-a", "tree", load("grok-a").image)
    assert len(calls) == 2
    assert calls[1].headers["authorization"] == "Bearer new-test-token"
    refresh.assert_awaited_once_with("grok-a", stale_access_token="old-test-token")
    assert bool(items) is not persistent
    assert (error is not None) is persistent
    assert calls[0].content == calls[1].content


@pytest.mark.parametrize("phase", ["submit", "poll", "both"])
async def test_video_401_refresh_once_without_repeating_an_accepted_job(monkeypatch, phase):
    from app.core import xai_token_refresh
    accounts(monkeypatch)
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", refresh)
    monkeypatch.setattr(supergrok_images, "resolve_credential", AsyncMock(side_effect=["old-test-token", "new-test-token"]))
    monkeypatch.setattr(native_video, "POLL_INTERVAL", 0)
    monkeypatch.setattr(native_video, "_download", AsyncMock(return_value=b"test-mp4"))
    calls, posts, polls = [], [], []
    def boundary(request):
        calls.append(request)
        if request.method == "POST":
            posts.append(request)
            if phase in ("submit", "both") and len(posts) == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"request_id": "job-1", "usage": {"cost_in_usd_ticks": 200000000}})
        polls.append(request)
        if phase in ("poll", "both") and len(polls) == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"status": "done", "video": {"url": "https://media.x.ai/video.mp4"}})
    monkeypatch.setattr(native_video, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boundary)))
    result = await native_video.generate("grok-a", "circle", load("grok-a").video)
    refresh.assert_awaited_once_with("grok-a", stale_access_token="old-test-token")
    assert len(posts) == (2 if phase in ("submit", "both") else 1)
    assert len(polls) == (2 if phase == "poll" else 1)
    assert result["success"] is (phase != "both")
    assert result["reported_cost_usd"] == "0.02"
    assert calls[-1].headers["authorization"] == "Bearer new-test-token"


@pytest.mark.parametrize("range_header,status,expected", [("bytes=1-3", 206, b"ide"), ("bytes=-3", 206, b"eos"), ("bytes=99-", 416, b"")])
async def test_video_proxy_bytes_support_range(monkeypatch, range_header, status, expected):
    from fastapi.responses import Response
    from app.api import artifacts
    app = FastAPI()
    app.include_router(artifacts.router)
    app.dependency_overrides[artifacts._require_auth_or_signed] = lambda: "test-auth"
    delegated = AsyncMock(return_value=Response(b"videos", media_type="video/mp4", headers={"Content-Disposition": "attachment"}))
    monkeypatch.setattr(artifacts, "download_artifact", delegated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/artifacts/" + "a" * 64 + "/video.mp4?session_id=owner", headers={"Range": range_header})
    assert response.status_code == status
    assert response.content == expected
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["content-disposition"].startswith("inline;")
    assert delegated.call_args.args[2:] == ("owner", "test-auth")


async def test_video_holder_redirect_keeps_signed_playable_route(monkeypatch):
    from fastapi.responses import RedirectResponse
    from app.api import artifacts
    digest = "a" * 64
    target = f"https://holder.example/api/v1/artifacts/{digest}?exp=123&sig=test-signature"
    monkeypatch.setattr(artifacts, "download_artifact", AsyncMock(return_value=RedirectResponse(target)))
    monkeypatch.setattr(artifacts, "get_artifact", lambda key: {"mime": "video/mp4"})
    app = FastAPI()
    app.include_router(artifacts.router)
    app.dependency_overrides[artifacts._require_auth_or_signed] = lambda: "test-auth"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/artifacts/{digest}/video.mp4?session_id=owner")
    assert response.status_code == 307
    assert response.headers["location"] == f"https://holder.example/api/v1/artifacts/{digest}/video.mp4?exp=123&sig=test-signature"


async def test_video_alias_rejects_non_video_artifacts(monkeypatch, tmp_path):
    from app.api import artifacts
    from app.core import artifact_access
    path = tmp_path / "document.txt"
    path.write_bytes(b"document")
    monkeypatch.setattr(artifacts, "get_artifact", lambda key: {"mime": "text/plain", "filename": "document.txt"})
    monkeypatch.setattr(artifacts, "artifact_path", lambda key: path)
    monkeypatch.setattr(artifact_access, "authorize_session_artifact", lambda *a: None)
    app = FastAPI()
    app.include_router(artifacts.router)
    app.dependency_overrides[artifacts._require_auth_or_signed] = lambda: "test-auth"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/artifacts/" + "a" * 64 + "/video.mp4?session_id=owner")).status_code == 404
