"""Production source adapters with fake credentials, schema and provider I/O."""
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.generation_resolver import Candidate, INCLUDED_PRICE, Resolver, RunContext, UNKNOWN_PRICE
from app.services.generation_sources import Sources
from app.services.fal_schema import ModelSchema, ParamField

CONTEXT = RunContext(session_id="session", unattended=False, owner=True)


@pytest.mark.parametrize("source,account,model,size,quality,fmt", [
    ("openai", "api-a", "test-image", "1024x1024", "low", "png"),
    ("xai", "api-b", "test-imagine", "1k", None, "jpeg"),
])
async def test_verified_api_table_drives_catalog_settings_pricing_and_submission(
    backend: Sources, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    source: str, account: str, model: str, size: str, quality: str | None, fmt: str,
) -> None:
    import base64
    from app.core import (
        artifact_store,
        native_image_verification as verification,
        native_generation_settings as native,
    )
    from app.core.provider import api_images
    from app.db import database
    from tests.core.test_native_image_tool import png

    record = verification.Verification(model, size, quality, fmt, "0.02", "mock probe only", "5")
    monkeypatch.setitem(verification.VERIFIED_API_IMAGES, source, record)
    row = {"id": account, "provider_type": source, "auth_mode": "api_key", "enabled": True,
           "base_url": verification.ENDPOINTS[source].rsplit("/images/", 1)[0],
           "auth_token": "fake-api-key"}
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    entry = (await backend.local_catalog("image", source=account, prompt="tree"))[0]
    assert entry.verified and entry.available and entry.price == record.price("tree")
    assert native.load(account).image.model_dump() == record.settings()
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        import json
        calls.append(json.loads(request.content))
        assert str(request.url) == verification.ENDPOINTS[source]
        assert request.headers["Authorization"] == "Bearer fake-api-key"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

    monkeypatch.setattr(
        api_images, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(send)),
    )
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    output = await Resolver(backend).generate(
        "image", {"prompt": "tree", "source": account}, CONTEXT,
    )
    assert output["status"] == "completed" and output["price"] == record.price("tree")
    assert len(calls) == 1 and calls[0]["model"] == model and calls[0]["n"] == 1
    # No unverified option can reach transport, and explicit disabling wins.
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": account,
                                                       "options": {"size": "unverified"}}, CONTEXT)
    assert output["status"] == "refused" and len(calls) == 1
    native.save(account, native.NativeGenerationSettings(image=None, video=None))
    output = await Resolver(backend).generate(
        "image", {"prompt": "tree", "source": account}, CONTEXT,
    )
    assert output["status"] == "refused" and len(calls) == 1


async def test_api_images_unverified_never_reads_credentials_or_opens_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import native_generation_settings as native
    from app.core.provider import api_images
    from app.db import database

    def forbidden(*a: Any, **kw: Any) -> NoReturn:
        pytest.fail("No credential/transport access is allowed without verification")

    monkeypatch.setattr(database, "get_model_provider", forbidden)
    monkeypatch.setattr(api_images, "http_client", forbidden)
    for source in ("openai", "xai"):
        items, warning = await api_images.generate(
            "api", source, "tree",
            native.NativeImageSettings(
                model="unverified", size="auto", quality="auto", format="png",
            ),
        )
        assert items == [] and "not yet verified" in warning

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
    from app.db import database
    rows = [{"id": "plan-a", "provider_type": "chatgpt", "auth_token": "never-return-this"},
            {"id": "plan-b", "provider_type": "grok"}, {"id": "api-a", "provider_type": "openai", "auth_mode": "api_key"},
            {"id": "api-b", "provider_type": "xai", "auth_mode": "api_key"}]
    monkeypatch.setattr(model_providers, "list_usable_model_providers", lambda: rows)
    # Stored Native generation settings are read through the real load() and
    # the isolated test DB; only the provider rows are fixtures.
    monkeypatch.setattr(database, "get_model_provider", lambda provider_id, include_token=True: next(
        ({k: v for k, v in r.items() if k != "auth_token"} for r in rows if r["id"] == provider_id), None))
    monkeypatch.setattr(source, "fal_price", AsyncMock(return_value=dict(PRICE)))
    return source


async def test_catalog_does_not_serialize_credentials_and_uses_verified_table(backend):
    entries = await backend.local_catalog("image")
    assert {e.source for e in entries} == {"fal", "chatgpt", "supergrok", "openai", "xai"}
    assert "never-return-this" not in repr([e.view() for e in entries])
    chatgpt = next(e for e in entries if e.source == "chatgpt")
    assert chatgpt.options == {"size": "auto", "quality": "auto", "format": "png"}
    assert chatgpt.verified
    # Plan images have no per-item charge: a known zero, never an unknown price.
    assert chatgpt.price == INCLUDED_PRICE
    assert next(e for e in entries if e.source == "supergrok").price == INCLUDED_PRICE
    assert all(e.price == UNKNOWN_PRICE for e in entries if e.source in ("openai", "xai"))
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


async def test_plan_image_is_included_and_reserves_nothing(backend, monkeypatch):
    from decimal import Decimal
    from app.network import generation
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(generation, "reserve", reserve)
    execute = AsyncMock(return_value={"success": True, "image_url": "/a"})
    monkeypatch.setattr(backend, "execute_local", execute)
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "plan-a"}, CONTEXT)
    assert output["status"] == "completed" and output["price"] == INCLUDED_PRICE
    reserve.assert_awaited_once_with(Decimal("0"))
    execute.assert_awaited_once()


async def test_unpriced_plan_is_not_submitted_even_when_confirmed(backend, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(backend, "execute", execute)
    original = backend.local_catalog
    async def unpriced(*args, **kw):
        return [replace(c, price=dict(UNKNOWN_PRICE)) for c in await original(*args, **kw)]
    monkeypatch.setattr(backend, "local_catalog", unpriced)
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
    monkeypatch.setattr(database, "get_model_provider", lambda *args, **kw: {"id": "plan-a", "provider_type": "chatgpt", "default_model": "text-model"})
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


# ---- Stored Native generation settings decide eligibility (#377 recovery) ----

def store(provider_id, raw):
    from app.db import database
    database.set_system_setting(f"native_generation:{provider_id}", raw)


async def test_missing_settings_row_keeps_provider_defaults_including_supergrok_video(backend):
    video = await backend.local_catalog("video", source="plan-b")
    assert [(e.source, e.available, e.price["total"]) for e in video] == [("supergrok", True, "0.02")]
    assert {e.source for e in await backend.local_catalog("image") if e.available} >= {"chatgpt", "supergrok"}


async def test_unreadable_settings_json_disables_video_but_keeps_verified_image(backend):
    store("plan-b", "{not json")
    disabled = await backend.local_catalog("video", source="plan-b")
    assert len(disabled) == 1 and disabled[0].verified and not disabled[0].available
    assert disabled[0].price["total"] == "0.02"
    image = await backend.local_catalog("image", source="plan-b")
    assert [e.options for e in image] == [{"size": "1k", "quality": None, "format": "jpeg"}]


async def test_stored_null_video_is_never_offered(backend):
    import json
    from app.core.native_generation_settings import capabilities
    store("plan-b", json.dumps({"image": capabilities("grok")["image"]["verified_combination"], "video": None}))
    disabled = await backend.local_catalog("video", source="plan-b")
    assert len(disabled) == 1 and disabled[0].verified and not disabled[0].available
    assert disabled[0].price["total"] == "0.02"
    assert await backend.local_catalog("image", source="plan-b") != []


@pytest.mark.parametrize("raw", ["{not json", '{"image": null, "video": null}'])
async def test_disabled_supergrok_video_never_spends_without_confirmation(backend, monkeypatch, raw):
    # The owner disabled SuperGrok video (or recovery did): a $0.02 video,
    # under the confirmation threshold, must still not be submitted.
    from app.core.provider import native_video
    submit = AsyncMock()
    monkeypatch.setattr(native_video, "generate", submit)
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(backend, "reserve", reserve)
    store("plan-b", raw)
    output = await Resolver(backend).generate("video", {"prompt": "tree", "source": "plan-b"}, CONTEXT)
    assert output["status"] == "refused"
    submit.assert_not_awaited()
    reserve.assert_not_awaited()


async def test_settings_disabled_after_quote_block_submission(backend, monkeypatch):
    from app.core.provider import native_video
    submit = AsyncMock()
    monkeypatch.setattr(native_video, "generate", submit)
    entry = (await backend.local_catalog("video", source="plan-b"))[0]
    store("plan-b", '{"image": null, "video": null}')
    output = await backend.execute_local(entry, "tree", CONTEXT)
    assert output["success"] is False
    submit.assert_not_awaited()


async def test_source_names_are_case_insensitive(backend, monkeypatch):
    monkeypatch.setattr(backend, "reserve", AsyncMock(return_value=True))
    for source in ("FAL", "Fal.AI", "fal"):
        output = await Resolver(backend).generate("image", {"prompt": "tree", "source": source, "model": "fake/model"},
                                                  replace(CONTEXT, session_id=None))
        assert output["status"] == "completed", source
    catalog = await Resolver(backend).catalog("video", source="SuperGrok")
    assert [o["source"] for o in catalog["options"]] == ["supergrok"]
    # Account IDs stay exact: a differently cased ID is another account.
    assert (await Resolver(backend).catalog("video", source="PLAN-B"))["options"] == []


async def test_remote_unknown_outcome_keeps_do_not_retry_instruction(backend, monkeypatch):
    from app.core.generation_resolver import DO_NOT_RETRY
    from app.network import generation
    async def unreachable(*args, **kw):
        raise ValueError("The account-holding worker did not confirm the outcome. Do not automatically retry a generation submission.")
    monkeypatch.setattr(generation, "execute", unreachable)
    remote = Candidate("fal", "fal-ai", "worker-b", "fake/model", "image", False, price=dict(PRICE))
    output = await backend.execute(remote, "tree", CONTEXT)
    assert output["success"] is False and DO_NOT_RETRY in output["error"]
