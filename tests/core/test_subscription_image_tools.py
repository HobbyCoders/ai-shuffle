"""Image tools on the ChatGPT subscription for chats on any provider (#1796).

When Settings names the ChatGPT subscription as the image model, the ai-tools
image tools every lane gets (delegated Claude, native non-OpenAI, voice) run in
the app process on the stored sign-in instead of on fal.ai. The provider is
stubbed with an ``httpx.MockTransport``; no real image provider is called.
"""

import base64
import json
import re
from pathlib import Path

import httpx
import pytest
from fluid.providers.openai_responses.client import OpenAIResponsesClient

from app.core import query_options
from app.core.provider import subscription_images as sub
from app.core.provider.fluid_credentials import ProviderCredentialError
from app.mcp import ai_tools_sdk
from app.mcp.ai_tools_sdk import _create_generic_tools, _create_image_tools

from tests.core.test_native_image_tool import png, stream_response

SECRET = "sk-stored-subscription-token-never-shown"
ROW = {"id": "prov-chatgpt", "provider_type": "chatgpt", "default_model": "gpt-5.5"}
IMAGE_TOOLS = ("generateImage", "editImage", "generateWithReference")


class _Worker:
    """The fal.ai Node worker: any call reaching it is a failure here."""

    def __init__(self):
        self.calls = []

    async def call(self, method, args):
        self.calls.append((method, args))
        return {"success": True}


def _tools(env, worker=None, cwd=None):
    return {t.name: t for t in _create_image_tools(worker or _Worker(), env, cwd)}


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def provider(monkeypatch):
    """Stub the signed-in row and the Responses client; record requests."""
    state = {"requests": [], "row": dict(ROW), "open_error": None, "status": 200, "opened": []}
    data = png()

    def transport(request):
        state["requests"].append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"code": "bad_request", "message": SECRET}})
        return httpx.Response(200, content=stream_response(data), headers={"content-type": "text/event-stream"})

    async def signed_in_row():
        return state["row"]

    async def open_client(provider_id):
        state["opened"].append(provider_id)
        if state["open_error"]:
            raise state["open_error"]
        http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        client = OpenAIResponsesClient(api_key=SECRET, base_url="https://chatgpt.example/backend/responses",
                                       http_client=http)
        return client, http

    monkeypatch.setattr(sub, "_signed_in_row", signed_in_row)
    monkeypatch.setattr(sub, "_open_client", open_client)
    monkeypatch.setattr(sub, "_text_model", lambda row: row.get("default_model"))
    state["png"] = data
    return state


ENV = {"IMAGE_MODEL": "chatgpt-subscription", "IMAGE_ASPECT_RATIO": "3:2"}


async def test_generate_routes_to_the_subscription_not_fal(provider, tmp_path):
    worker = _Worker()
    result = await _tools(ENV, worker, str(tmp_path))["generateImage"].handler({"prompt": "A red fox"})
    payload = _payload(result)
    assert payload["success"] is True
    assert payload["provider_used"] == "chatgpt_subscription"
    assert worker.calls == []
    assert provider["opened"] == ["prov-chatgpt"]
    body = json.loads(provider["requests"][0].content)
    assert body["model"] == "gpt-5.5"
    assert body["tool_choice"] == {"type": "image_generation"}
    assert body["tools"] == [{"type": "image_generation", "output_format": "png", "partial_images": 0,
                              "size": "1536x1024"}]
    saved = Path(payload["file_path"])
    assert saved.parent == tmp_path / "generated-images"
    assert saved.read_bytes() == provider["png"]
    assert payload["image_url"].startswith("/api/generated-images/by-path?path=")
    assert SECRET not in json.dumps(payload)


async def test_edit_sends_the_image_to_the_subscription(provider, tmp_path):
    source = tmp_path / "in.png"
    source.write_bytes(provider["png"])
    env = {"IMAGE_MODEL": "fal-ai/flux-pro/v1.1-ultra", "IMAGE_EDIT_MODEL": "chatgpt-subscription/gpt-image-2"}
    worker = _Worker()
    tools = _tools(env, worker, str(tmp_path))
    payload = _payload(await tools["editImage"].handler({"prompt": "Make it blue", "image_path": "in.png"}))
    assert payload["success"] is True and payload["model_used"] == "gpt-image-2"
    body = json.loads(provider["requests"][0].content)
    assert body["tools"][0]["model"] == "gpt-image-2"
    parts = body["input"][0]["content"]
    assert any(p.get("type") == "input_image" and base64.b64encode(provider["png"]).decode() in p.get("image_url", "")
               for p in parts)
    # Generation was left on fal.ai, so that tool still goes to the worker.
    await tools["generateImage"].handler({"prompt": "x"})
    assert [c[0] for c in worker.calls] == ["generateImage"]


async def test_reference_images_route_to_the_subscription(provider, tmp_path):
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(provider["png"])
    payload = _payload(await _tools(ENV, cwd=str(tmp_path))["generateWithReference"].handler(
        {"prompt": "Same character", "reference_images": ["a.png", str(tmp_path / "b.png")]}))
    assert payload["success"] is True
    parts = json.loads(provider["requests"][0].content)["input"][0]["content"]
    assert sum(p.get("type") == "input_image" for p in parts) == 2


@pytest.mark.parametrize("name", IMAGE_TOOLS)
async def test_foreign_model_id_is_refused_without_a_request(provider, tmp_path, name):
    args = {"prompt": "p", "model": "fal-ai/nano-banana-2", "image_path": "x.png", "reference_images": ["x.png"]}
    worker = _Worker()
    payload = _payload(await _tools(ENV, worker, str(tmp_path))[name].handler(args))
    assert payload["success"] is False
    assert "'fal-ai/nano-banana-2' is not one of its models" in payload["error"]
    assert "ChatGPT subscription" in payload["error"] and "chatgpt-subscription" in payload["error"]
    assert provider["requests"] == [] and provider["opened"] == [] and worker.calls == []


async def test_the_configured_model_id_is_accepted(provider, tmp_path):
    payload = _payload(await _tools(ENV, cwd=str(tmp_path))["generateImage"].handler(
        {"prompt": "p", "model": "chatgpt-subscription"}))
    assert payload["success"] is True


@pytest.mark.parametrize("cause", ["no_row", "needs_reauth"])
async def test_not_signed_in_is_said_plainly(provider, tmp_path, cause):
    if cause == "no_row":
        provider["row"] = None
    else:
        provider["open_error"] = ProviderCredentialError(f"token {SECRET} expired")
    worker = _Worker()
    payload = _payload(await _tools(ENV, worker, str(tmp_path))["generateImage"].handler({"prompt": "p"}))
    assert payload == {"success": False, "error": sub.NOT_SIGNED_IN, "provider_used": "chatgpt_subscription"}
    assert "no ChatGPT account is signed in on this worker" in payload["error"]
    assert provider["requests"] == [] and worker.calls == []


async def test_provider_error_is_sanitized(provider, tmp_path):
    provider["status"] = 400
    payload = _payload(await _tools(ENV, cwd=str(tmp_path))["generateImage"].handler({"prompt": "p"}))
    assert payload["success"] is False
    assert "HTTP 400" in payload["error"] and SECRET not in json.dumps(payload)
    assert not (tmp_path / "generated-images").exists()


@pytest.mark.parametrize("name", IMAGE_TOOLS)
def test_descriptions_name_only_the_configured_provider(name):
    t = _tools({"IMAGE_MODEL": "chatgpt-subscription"})[name]
    text = " ".join([t.description, *(json.dumps(p) for p in t.input_schema["properties"].values())])
    assert "ChatGPT subscription" in text and "chatgpt-subscription" in text
    assert "OpenAI's default image model" in text
    lowered = text.lower()
    for other in ("fal", "nano", "flux", "kontext", "seedream", "imagen"):
        assert other not in lowered


def test_fal_configuration_keeps_the_fal_tools():
    tools = _tools({"IMAGE_MODEL": "fal-ai/nano-banana-2"})
    assert all("fal.ai" in tools[n].description for n in IMAGE_TOOLS)
    assert all("subscription" not in tools[n].description for n in IMAGE_TOOLS)


def _run_fal_model(env, worker):
    return {t.name: t for t in _create_generic_tools(worker, env)}["runFalModel"]


@pytest.fixture
def fal_catalog(monkeypatch):
    """A loaded fal.ai catalog with one listed image model under a neutral name."""
    from app.services.fal_catalog import CatalogModel, catalog_service

    models = [CatalogModel(endpoint_id="fal-ai/bria/fibo", display_name="Fibo", category="text-to-image",
                           description="", output_type="image"),
              CatalogModel(endpoint_id="beatoven/compose", display_name="Compose", category="text-to-audio",
                           description="", output_type="audio")]
    monkeypatch.setattr(catalog_service, "_cache", models)


@pytest.mark.parametrize("args", [
    {"endpoint_id": "fal-ai/imagen4/preview"},                                # no output_type
    {"endpoint_id": "fal-ai/imagen4/preview", "output_type": "image"},
    {"endpoint_id": "fal-ai/flux/dev", "output_type": "video"},               # image name, wrong type
    {"endpoint_id": "fal-ai/bria/fibo"},                                      # catalog: image
    {"endpoint_id": "fal-ai/bria/fibo", "output_type": "audio"},              # catalog beats the claim
    {"endpoint_id": "fal-ai/kling-video/o3/pro", "output_type": "image"},     # explicit image
    {"endpoint_id": "someone/unlisted-model"},                                # unplaceable, no type
])
async def test_run_fal_model_refuses_images_on_the_subscription(fal_catalog, args):
    worker = _Worker()
    run = _run_fal_model(ENV, worker)
    assert "Imagen" not in run.description and "Not for images" in run.description
    payload = _payload(await run.handler({**args, "input": {"prompt": "p"}}))
    assert payload["success"] is False and "nothing was run" in payload["error"]
    assert worker.calls == []


@pytest.mark.parametrize("args", [
    {"endpoint_id": "fal-ai/kling-video/o3/pro/text-to-video"},
    {"endpoint_id": "fal-ai/veo3", "output_type": "video"},
    {"endpoint_id": "fal-ai/lyria2"},
    {"endpoint_id": "fal-ai/stable-audio", "output_type": "audio"},
    {"endpoint_id": "fal-ai/hunyuan-3d/v2"},
    {"endpoint_id": "beatoven/compose"},                                      # catalog: audio
    {"endpoint_id": "someone/unlisted-model", "output_type": "audio"},        # known limit: allowed
])
async def test_run_fal_model_keeps_video_audio_and_3d(fal_catalog, args):
    worker = _Worker()
    await _run_fal_model(ENV, worker).handler({**args, "input": {"prompt": "p"}})
    assert [c[0] for c in worker.calls] == ["runFalModel"]


@pytest.mark.parametrize("args", [
    {"endpoint_id": "fal-ai/imagen4/preview"},
    {"endpoint_id": "fal-ai/flux/dev", "output_type": "image"},
    {"endpoint_id": "someone/unlisted-model"},
])
async def test_run_fal_model_refuses_nothing_when_images_are_on_fal(fal_catalog, args):
    worker = _Worker()
    run = _run_fal_model({"IMAGE_MODEL": "fal-ai/nano-banana-2"}, worker)
    assert "Not for images" not in run.description
    await run.handler({**args, "input": {"prompt": "p"}})
    assert [c[0] for c in worker.calls] == ["runFalModel"]


def test_output_type_hints_match_the_node_worker():
    """The refusal mirrors ``inferOutputType`` in the Node worker."""
    source = (Path(__file__).resolve().parents[2] / "tools/providers/fal-generic.ts").read_text()
    body = source[source.index("function inferOutputType"):]
    body = body[:body.index("return 'image';")]
    blocks = re.findall(r"if \((.*?)\) \{\s*return '(\w+)';", body, re.S)
    found = {kind: tuple(re.findall(r"includes\('([^']+)'\)", cond)) for cond, kind in blocks}
    assert found == {"video": ai_tools_sdk._FAL_VIDEO_HINTS, "3d": ai_tools_sdk._FAL_3D_HINTS,
                     "audio": ai_tools_sdk._FAL_AUDIO_HINTS}


@pytest.mark.parametrize("settings,expected", [
    ({"image_model": "chatgpt-subscription"}, {"generateImage", "editImage", "generateWithReference"}),
    ({"image_model": "chatgpt-subscription", "image_edit_model": "fal-ai/flux-pro/kontext"}, {"generateImage"}),
    ({"image_model": "fal-ai/x", "image_edit_model": "chatgpt-subscription"}, {"editImage", "generateWithReference"}),
    ({"image_model": "fal-ai/x"}, set()),
])
def test_subscription_tools_need_no_fal_key(monkeypatch, settings, expected):
    """Without a fal.ai key only the subscription image tools are offered."""
    monkeypatch.setattr(query_options.database, "get_system_setting", lambda key: settings.get(key))
    assert query_options._subscription_image_tool_names() == expected
