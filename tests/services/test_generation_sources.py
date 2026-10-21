"""Production source adapters with fake credentials, schema and provider I/O."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.generation_resolver import Candidate, Resolver, RunContext, UNKNOWN_PRICE
from app.services.generation_sources import Sources
from app.services.fal_schema import ModelSchema, ParamField

CONTEXT = RunContext(session_id="session", unattended=False, owner=True)
PRICE = {"currency": "USD", "unit": "item", "amount": "0.05", "total": "0.05", "status": "estimated"}


@pytest.fixture
def backend(monkeypatch):
    from app.services.fal_catalog import catalog_service
    from app.services.fal_schema import schema_service
    from app.db import model_providers
    from app.network import runners
    from app.core import fal_credential

    node = SimpleNamespace(call=AsyncMock(return_value={"success": True, "output_url": "/fake-image"}))
    source = Sources(node, CONTEXT, local_only=True)
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    monkeypatch.setattr(fal_credential, "api_key", lambda user=None: "test-fal-key")
    monkeypatch.setattr(catalog_service, "refresh", AsyncMock())
    monkeypatch.setattr(catalog_service, "search", lambda **kw: [SimpleNamespace(endpoint_id="fake/model", category="text-to-image")])
    schema = ModelSchema("fake/model", [ParamField("prompt", is_primary_text=True, required=True),
        ParamField("num_images", type="integer", default=1, minimum=1, maximum=4),
        ParamField("size", type="enum", default="square", enum=["square", "wide"])], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(model_providers, "list_usable_model_providers", lambda: [
        {"id": "plan-a", "provider_type": "chatgpt", "auth_token": "never-return-this"},
        {"id": "plan-b", "provider_type": "grok"}, {"id": "api-a", "provider_type": "openai", "auth_mode": "api_key"},
        {"id": "api-b", "provider_type": "xai", "auth_mode": "api_key"}])
    monkeypatch.setattr(source, "fal_price", AsyncMock(return_value=dict(PRICE)))
    return source


async def test_catalog_does_not_serialize_credentials_and_uses_verified_table(backend):
    entries = await backend.local_catalog("image")
    assert {e.source for e in entries} == {"fal", "chatgpt", "supergrok", "openai", "xai"}
    assert "never-return-this" not in repr([e.view() for e in entries])
    chatgpt = next(e for e in entries if e.source == "chatgpt")
    assert chatgpt.options == {"size": "auto", "quality": "auto", "format": "png"}
    assert chatgpt.verified
    assert chatgpt.price == UNKNOWN_PRICE
    api = (await backend.local_catalog("image", source="api-a"))[0]
    assert not api.available and not api.verified


async def test_fal_declared_options_reach_price_and_execution_identically(backend, monkeypatch, tmp_path):
    entry = (await backend.local_catalog("image", source="fal", model="fake/model"))[0]
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    media = tmp_path / "generated.png"
    from tests.core.test_native_image_tool import png
    media.write_bytes(png())
    backend.node.call.return_value = {"success": True, "file_path": str(media), "mime_type": "image/png"}
    from app.core import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "fal", "model": entry.model,
        "options": {"num_images": 2, "size": "wide"}}, CONTEXT)
    assert output["status"] == "completed"
    backend.fal_price.assert_awaited_with("fake/model", {"num_images": 2, "size": "wide"}, prompt="tree")
    backend.node.call.assert_awaited_once_with("runFalModel", {"endpoint_id": "fake/model", "output_type": "image",
        "input": {"prompt": "tree", "num_images": 2, "size": "wide"}})


@pytest.mark.parametrize("options", [{"size": "bad"}, {"num_images": True}, {"num_images": 5}, {"unknown": 1}, {"prompt": "injected"}])
async def test_declared_options_are_enforced_before_submit(backend, options):
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "fal", "model": "fake/model", "options": options}, CONTEXT)
    assert output["status"] == "refused"
    backend.node.call.assert_not_awaited()


@pytest.mark.parametrize("source,kind,options", [
    ("chatgpt", "image", {"size": "1024x1024"}),
    ("supergrok", "image", {"size": "2k"}),
    ("supergrok", "video", {"duration": 2}),
    ("supergrok", "video", {"model": "unverified"}),
])
async def test_only_exact_verified_subscription_combinations(backend, source, kind, options):
    output = await Resolver(backend).generate(kind, {"prompt": "tree", "source": source, "options": options}, CONTEXT)
    assert output["status"] == "refused"
    assert "verified" in output["error"] or "separately" in output["error"]
    backend.node.call.assert_not_awaited()


async def test_api_user_cannot_discover_owner_plans_or_peers(backend, monkeypatch):
    from app.network import generation
    peer_catalog = AsyncMock()
    monkeypatch.setattr(generation, "catalog", peer_catalog)
    backend.context = replace(CONTEXT, owner=False, api_user_id="user-a")
    backend.local_only = False
    assert {c.source for c in await backend.catalog("image")} == {"fal"}
    peer_catalog.assert_not_awaited()
    assert await backend.catalog("image", source="plan-a") == []


async def test_flat_fal_prices_count_canonical_options_and_unknown_is_not_zero(backend, monkeypatch):
    # Exercise the real pricing adapter, not the fixture's priced catalog.
    monkeypatch.delattr(backend, "fal_price")
    response = {"prices": [{"endpoint_id": "fake/model", "unit_price": "0.05", "unit": "image", "currency": "USD"}]}
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=response)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(transport), **kw))
    price = await backend.fal_price("fake/model", {"num_images": 3})
    assert price == {**PRICE, "total": "0.15"}
    assert requests[0].url.params["endpoint_id"] == "fake/model"
    response["prices"][0]["unit"] = "megapixel"
    assert await backend.fal_price("fake/model", {}) == UNKNOWN_PRICE


async def test_unpriced_plan_is_not_submitted_even_when_confirmed(backend, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(backend, "execute", execute)
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "plan-a", "confirmed": True}, CONTEXT)
    assert output["reason"] == "price_unavailable"
    execute.assert_not_awaited()


async def test_selected_chatgpt_account_produces_session_artifact(backend, monkeypatch, tmp_path):
    import base64
    from app.core import artifact_store
    from app.core.provider import native_images, subscription_images
    from app.db import database, artifacts
    from tests.core.test_native_image_tool import png

    entry = (await backend.local_catalog("image", source="plan-a"))[0]
    entry.price = dict(PRICE)  # Fake a trusted quote; production has none yet.
    monkeypatch.setattr(backend, "catalog", AsyncMock(return_value=[entry]))
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(database, "get_model_provider", lambda *args, **kw: {"id": "plan-a", "default_model": "text-model"})
    monkeypatch.setattr(subscription_images, "_text_model", lambda row: row["default_model"])
    open_client = AsyncMock(return_value=(object(), None))
    monkeypatch.setattr(subscription_images, "_open_client", open_client)
    requests = []
    async def stream(client, request, items, usage):
        requests.append(request)
        items.append({"result": base64.b64encode(png()).decode(), "status": "completed"})
        return None
    monkeypatch.setattr(native_images, "stream_images", stream)
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "plan-a"}, CONTEXT)
    assert output["status"] == "completed"
    open_client.assert_awaited_once_with("plan-a")
    assert requests[0].model == "text-model"
    assert requests[0].tools[0]["output_format"] == "png"
    url = output["result"]["image_url"]
    digest = url.split("/artifacts/")[1].split("?")[0]
    assert artifacts.get_artifact(digest)["created_by_session"] == CONTEXT.session_id
    assert artifact_store.artifact_path(digest).read_bytes() == png()
    backend.node.call.assert_not_awaited()


async def test_chat_generation_cannot_upload_arbitrary_local_file(backend, monkeypatch):
    from app.services.fal_schema import schema_service
    schema = ModelSchema("fake/model", [ParamField("prompt", is_primary_text=True), ParamField("image_url", type="file")], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    backend.context = replace(CONTEXT, chat_mode=True)
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "fal", "model": "fake/model",
        "options": {"image_url": "/outside-uploads/private.png"}}, backend.context)
    assert output["status"] == "refused"
    assert "chat uploads" in output["error"]
    backend.node.call.assert_not_awaited()
