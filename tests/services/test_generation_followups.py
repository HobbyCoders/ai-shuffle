"""Issues #512-514: real resolver/catalog/tool paths with fixture-only providers."""
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.generation_resolver import UNKNOWN_PRICE, Resolver, RunContext
from app.mcp import ai_tools_sdk as sdk
from app.services.fal_schema import normalize_openapi
from tests.api.test_generation_choices_api import client, quote_backend
from tests.core.test_generation_resolver import INTERACTIVE, FakeBackend, candidate
from tests.mcp.test_unified_generation_tools import lane_tools, payload
from tests.services.test_generation_sources import backend

# Re-export the fixtures pytest discovers in this module.
__all__ = ["backend", "client", "quote_backend"]


@pytest.mark.parametrize("named", [
    {"source": "supergrok"},
    {"source": "fal", "model": "kling/v4/text-to-video"},
    {"source": "another-account"},
    {"account": "another-account"},
    {"worker": "another-worker"},
])
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_unattended_mismatch_skips_before_any_catalog_access(named, kind):
    stored = candidate(kind=kind)
    backend = FakeBackend([stored])
    backend.catalog = AsyncMock(side_effect=AssertionError("Must not discover a different stored route"))
    context = RunContext(unattended=True, remembered_choice=stored.choice())
    output = await Resolver(backend).generate(kind, {"prompt": "tree", **named}, context)
    assert output["status"] == "skipped"
    assert output["reason"] == "named_choice_differs_from_stored"
    assert "unattended" in output["message"] and "owner" in output["message"]
    backend.catalog.assert_not_awaited()
    assert backend.calls == []


@pytest.mark.parametrize("named", [
    {"source": "supergrok"},
    {"source": "fal", "model": "kling/v4/text-to-video"},
    {"account": "another-account"},
    {"worker": "another-worker"},
])
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_unattended_job_binding_runs_and_ignores_named_route(named, kind):
    # A job's own stored choice is the owner's binding (#507): it runs, and
    # nothing named in the call is looked up or charged instead.
    stored = candidate(kind=kind)
    backend = FakeBackend([stored])
    context = RunContext(unattended=True, job_choice=stored.choice())
    output = await Resolver(backend).generate(kind, {"prompt": "tree", **named}, context)
    assert output["status"] == "completed" and output["job_choice"] is True
    assert output["choice"]["account"] == stored.account and output["choice"]["model"] == stored.model
    assert [c for c in backend.calls if c[0] in ("reserve", "execute")] == [
        ("reserve", stored.account), ("execute", stored.account)]


@pytest.mark.parametrize("source", ["fal", "FAL.AI", "account-a"])
async def test_unattended_matching_identity_still_runs(source):
    entry = candidate()
    backend = FakeBackend([entry])
    output = await Resolver(backend).generate("image", {
        "prompt": "tree", "source": source, "model": entry.model},
        RunContext(unattended=True, job_choice=entry.choice()))
    assert output["status"] == "completed" and output["choice"] == entry.choice()
    assert ("execute", entry.account) in backend.calls


@pytest.mark.parametrize("source", ["fal", "FAL.AI"])
async def test_unattended_matching_account_only_fal_choice_still_runs(source):
    entry = candidate(account="fal-ai")
    backend = FakeBackend([entry])
    output = await Resolver(backend).generate("image", {
        "prompt": "tree", "source": source, "model": entry.model},
        RunContext(unattended=True, job_choice={"account": "fal-ai", "model": entry.model}))
    assert output["status"] == "completed" and output["choice"] == entry.choice()
    assert ("execute", entry.account) in backend.calls


async def test_catalog_collapses_holders_but_preserves_accounts_and_options(backend, monkeypatch):
    from app.network import generation
    local = candidate(source="supergrok")
    local.price = {"currency": "USD", "unit": "item", "amount": "0", "total": "0", "status": "included"}
    duplicate = replace(local, worker="worker-b")
    other_account = replace(duplicate, account="another-account")
    other_options = replace(duplicate, options={"size": "wide"})
    backend.local_only = False
    backend.local_catalog = AsyncMock(return_value=[local])
    peers = AsyncMock(return_value=[duplicate, other_account, other_options])
    monkeypatch.setattr(generation, "catalog", peers)
    entries = await backend.catalog("image")
    assert len(entries) == 3
    assert next(c for c in entries if c.account == local.account and c.options == local.options).worker == "worker-a"
    output = await Resolver(backend).generate("image", {"prompt": "tree"}, INTERACTIVE)
    assert len(output["options"]) == 3
    assert sum(o["account"] == local.account and o["options"] == local.options for o in output["options"]) == 1
    # Without a local holder, peer ordering never changes the chosen holder.
    backend.local_catalog.return_value = []
    for peers_order in ([duplicate, replace(duplicate, worker="worker-c")],
                        [replace(duplicate, worker="worker-c"), duplicate]):
        peers.return_value = peers_order
        assert [c.worker for c in await backend.catalog("image")] == ["worker-b"]


def kling_input_schema(*, huge=False):
    # Declared defaults/units from Kling v4's public OpenAPI shape; no live key.
    properties = {
        "prompt": {"type": "string"},
        "duration": {"type": "integer", "default": 5, "minimum": 3, "maximum": 30,
                     "description": "Video duration in seconds (3-30)."},
        "generate_audio": {"type": "boolean", "default": True},
    }
    if huge:
        properties.update({f"option_{i}": {"type": "string", "description": "x" * 10000,
                                         "enum": ["a", "b"], "default": "a"} for i in range(100)})
    return normalize_openapi("kling/v4/text-to-video", {
        "components": {"schemas": {"VideoInput": {"type": "object", "properties": properties,
                                                     "required": ["prompt"]}}}})


async def test_search_card_and_generate_use_price_endpoint_defaults(backend, monkeypatch):
    from app.services import generation_sources
    from app.services.fal_catalog import CatalogModel, catalog_service
    from app.services.fal_schema import schema_service
    monkeypatch.delattr(backend, "fal_price")
    model = "kling/v4/text-to-video"
    monkeypatch.setattr(catalog_service, "search", lambda **kw: [CatalogModel(
        model, "Kling", "text-to-video", "", output_type="video")] if kw.get("output_type") == "video" else [])
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=kling_input_schema()))
    original = httpx.AsyncClient
    response = {"prices": [{"endpoint_id": model, "currency": "USD", "unit": "second", "unit_price": "0.157"}]}
    requests = []
    def price_response(request):
        requests.append(request)
        return httpx.Response(200, json=response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(price_response), **kw))
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, backend.context)}
    searched = payload(await tools["searchModels"].handler({"source": "fal", "model": model}))
    searched_price = searched["options"][0]["price"]
    choice_card = await Resolver(backend).generate("video", {"prompt": "tree"}, backend.context)
    card_price = next(o["price"] for o in choice_card["options"] if o["source"] == "fal")
    generation = payload(await tools["generateVideo"].handler({"prompt": "tree", "source": "fal", "model": model}))
    assert generation["status"] == "needs-confirmation"
    assert searched["price"] == searched_price == card_price == generation["price"]
    assert searched_price["status"] == "estimated" and searched_price["total"] == "0.785"
    ten_seconds = payload(await tools["generateVideo"].handler({
        "prompt": "tree", "source": "fal", "model": model, "duration": 10}))
    assert ten_seconds["status"] == "needs-confirmation" and ten_seconds["price"]["total"] == "1.570"
    # Default resolution is shared even when the quote caller omits options.
    assert await backend.fal_price(model, {}) == searched_price
    assert requests and all(r.url.path == "/v1/models/pricing" for r in requests)
    backend.node.call.assert_not_awaited()


async def test_search_schema_is_bounded_and_full_schema_is_opt_in(backend, monkeypatch):
    from app.services import generation_sources
    from app.services.fal_catalog import CatalogModel, catalog_service
    from app.services.fal_schema import schema_service
    schema = kling_input_schema(huge=True)
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    monkeypatch.setattr(catalog_service, "search", lambda **kw: [CatalogModel(
        "kling/v4/text-to-video", "Kling", "text-to-video", "", output_type="video")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, backend.context)}
    args = {"source": "fal", "model": "kling/v4/text-to-video", "output_type": "video", "limit": 2}
    compact = payload(await tools["searchModels"].handler(args))
    full = payload(await tools["searchModels"].handler({**args, "full_schema": True}))
    assert len(json.dumps(compact, ensure_ascii=True)) <= 32768
    assert len(json.dumps(full)) > 118000
    compact_schema = compact["options"][0]["schema"]
    assert compact_schema["compact"] and compact_schema["truncated"]
    assert "input_schema" not in compact_schema
    fields = {f["name"]: f for f in compact_schema["fields"]}
    assert fields["duration"]["default"] == 5 and fields["duration"]["maximum"] == 30
    assert fields["generate_audio"]["type"] == "boolean" and not fields["generate_audio"]["is_file"]
    assert full["options"][0]["schema"] == schema.to_dict()
    assert tools["searchModels"].input_schema["properties"]["full_schema"]["type"] == "boolean"


@pytest.mark.parametrize("remember,origin", [(True, "card"), (True, "settings"), (False, "card")])
def test_saving_unpriced_pick_reports_future_unattended_skip(client, quote_backend, remember, origin):
    # Use the real save and quote endpoints with only provider I/O faked.
    entry = candidate()
    quote_backend.entries = [replace(entry, price=dict(UNKNOWN_PRICE))]
    response = client.put("/api/v1/generation/choices/image", json={
        "choice": entry.choice(), "remember": remember, "origin": origin})
    assert response.status_code == 200
    output = response.json()
    assert output["remembered"] is remember
    assert output["priced"] is False and output["price"]["status"] == "unknown"
    assert output["unattended_skip_reason"] == "stored_choice_unpriced"
    assert "Unattended runs will skip" in output["warning"]
    assert not any(call[0] in ("reserve", "execute") for call in quote_backend.calls)


@pytest.mark.parametrize("enum", [None, [True, False]])
def test_boolean_media_switch_remains_boolean(enum):
    field = {"type": "boolean", "default": True}
    if enum is not None:
        field["enum"] = enum
    schema = normalize_openapi("test/model", {"components": {"schemas": {"VideoInput": {
        "type": "object", "properties": {"prompt": {"type": "string"}, "generate_audio": field}}}}})
    switch = next(f for f in schema.fields if f.name == "generate_audio")
    assert switch.type == "boolean" and not switch.is_file and switch.default is True


async def test_search_bounds_actual_wire_json_across_many_large_options(backend, monkeypatch):
    from app.services import generation_sources
    schema = kling_input_schema(huge=True).to_dict()
    backend.catalog = AsyncMock(return_value=[replace(candidate(model=f"model-{i}"), schema=schema) for i in range(50)])
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, backend.context)}
    response = await tools["searchModels"].handler({"source": "fal", "model": "model-0", "output_type": "image", "limit": 50})
    assert len(response["content"][0]["text"]) <= 32768
    output = payload(response)
    assert output["truncated"] and output["total_matches"] == 50
    assert 0 < len(output["options"]) < 50


def test_saving_priced_pick_returns_quote_without_warning(client, quote_backend):
    entry = quote_backend.entries[0]
    output = client.put("/api/v1/generation/choices/image", json={"choice": entry.choice()}).json()
    assert output["priced"] is True and output["warning"] is None
    assert output["unattended_skip_reason"] is None
    assert output["price"]["total"] == "0.05"


@pytest.mark.parametrize("source", [None, "supergrok", "chatgpt"])
async def test_search_models_handles_real_subscription_capability_fields(backend, monkeypatch, source):
    from app.core.native_generation_settings import capabilities
    from app.services import generation_sources
    backend.fal_key = AsyncMock(return_value=None)
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, backend.context)}
    output = payload(await tools["searchModels"].handler({"source": source} if source else {}))
    subscription = next(o for o in output["options"] if o["source"] == (source or "supergrok") and o["kind"] == "image")
    expected = capabilities("chatgpt" if source == "chatgpt" else "grok")["image"]
    assert isinstance(expected["fields"], dict)
    assert subscription["schema"] == expected
    assert len(json.dumps(output, ensure_ascii=True)) <= 32768
    backend.node.call.assert_not_awaited()


@pytest.mark.parametrize("worker", ["worker-b", None])
@pytest.mark.parametrize("unattended", [True, False])
async def test_run_fal_model_respects_per_kind_remembered_holder(monkeypatch, worker, unattended):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    entry = replace(candidate(model="fal-ai/x"), worker="worker-b")
    choice = entry.choice()
    if worker is None:
        choice.pop("worker")
    context = replace(INTERACTIVE, unattended=unattended, remembered_choices={"image": choice})
    tools, backend, legacy = lane_tools(monkeypatch, [entry], context)
    backend.fal_key = AsyncMock(return_value="test-key")
    schema = ModelSchema(entry.model, [ParamField("prompt", is_primary_text=True)], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    output = payload(await tools["runFalModel"].handler({
        "endpoint_id": entry.model, "input": {"prompt": "tree"}, "output_type": "image"}))
    assert output["status"] == "completed" and output["choice"]["worker"] == "worker-b"
    assert ("execute", entry.account) in backend.calls
    legacy.assert_not_awaited()


@pytest.mark.parametrize("source", ["supergrok", "fal"])
@pytest.mark.parametrize("identity", ["account", "source", "both"])
async def test_unattended_account_uuid_compares_resolved_source_without_discovery(backend, monkeypatch, source, identity):
    from app.db import database, model_providers
    account = "12345678-1234-4321-8765-123456789abc"
    row = {"id": account, "provider_type": "grok"}
    metadata_reads = []
    def get_account(provider_id, include_token=False):
        metadata_reads.append((provider_id, include_token))
        return row if provider_id == account else None
    monkeypatch.setattr(database, "get_model_provider", get_account)
    monkeypatch.setattr(model_providers, "list_usable_model_providers", lambda: [row])
    entry = (await backend.local_catalog("image", source=account))[0]
    choice = {k: v for k, v in entry.choice().items() if k != "source"}
    if identity in ("source", "both"):
        choice["source"] = account
    if identity == "source":
        choice.pop("account")
    backend.catalog = AsyncMock(wraps=backend.catalog)
    backend.reserve = AsyncMock(return_value=True)
    backend.execute = AsyncMock(return_value={"success": True})
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": source},
        replace(backend.context, unattended=True, remembered_choices={"image": choice}))
    if source == "supergrok":
        assert output["status"] == "completed" and output["choice"]["account"] == account
        assert backend.catalog.await_args.kwargs["source"] == account
        backend.execute.assert_awaited_once()
    else:
        assert output["reason"] == "named_choice_differs_from_stored"
        backend.catalog.assert_not_awaited()
        backend.execute.assert_not_awaited()
    assert metadata_reads and all(not include_token for _, include_token in metadata_reads)


def test_save_unavailable_pick_preserves_quote_error(client, quote_backend):
    choice = quote_backend.entries[0].choice()
    quote_backend.entries = []
    output = client.put("/api/v1/generation/choices/image", json={"choice": choice}).json()
    assert output["remembered"] and not output["priced"]
    assert output["quote_error"] == "This account or model is not available right now."
    assert output["quote_error"] in output["warning"]
    assert "no verified price" not in output["warning"]
    assert output["unattended_skip_reason"] == "stored_choice_unavailable"
    assert client.get("/api/v1/generation/choices").json()["remembered"]["image"]["account"] == choice["account"]


def test_save_bounds_quote_wait_and_preserves_pick(client, quote_backend, monkeypatch):
    import asyncio

    from app.api import generation_choices as api
    cancelled = []
    async def stalled_catalog(*a, **kw):
        try:
            await asyncio.sleep(1)
            return quote_backend.entries
        finally:
            cancelled.append(True)
    monkeypatch.setattr(api, "_SAVE_QUOTE_TIMEOUT_SECONDS", 0.01)
    quote_backend.catalog = AsyncMock(side_effect=stalled_catalog)
    choice = quote_backend.entries[0].choice()
    response = client.put("/api/v1/generation/choices/image", json={"choice": choice})
    output = response.json()
    assert response.status_code == 200 and cancelled
    assert output["remembered"] and not output["priced"]
    assert "timed out" in output["quote_error"] and output["quote_error"] in output["warning"]
    assert output["unattended_skip_reason"] == "stored_choice_unavailable"
    assert client.get("/api/v1/generation/choices").json()["remembered"]["image"]["account"] == choice["account"]
    assert not any(call[0] in ("reserve", "execute") for call in quote_backend.calls)


async def test_unattended_mismatched_model_does_not_resolve_account_metadata():
    entry = candidate(source="supergrok")
    backend = FakeBackend([entry])
    backend.catalog = AsyncMock(side_effect=AssertionError("No catalog access"))
    backend.source_for_account = AsyncMock(side_effect=AssertionError("No metadata access"))
    output = await Resolver(backend).generate("image", {
        "prompt": "tree", "source": "supergrok", "model": "different-model"},
        replace(INTERACTIVE, unattended=True, remembered_choices={"image": {
            "account": entry.account, "model": entry.model}}))
    assert output["reason"] == "named_choice_differs_from_stored"
    backend.catalog.assert_not_awaited()
    backend.source_for_account.assert_not_awaited()


async def test_unattended_unreadable_account_metadata_skips_before_catalog():
    entry = candidate(source="supergrok")
    backend = FakeBackend([entry])
    backend.catalog = AsyncMock(side_effect=AssertionError("No catalog access"))
    backend.source_for_account = AsyncMock(side_effect=RuntimeError("Account metadata unavailable"))
    output = await Resolver(backend).generate("image", {"prompt": "tree", "source": "supergrok"},
        replace(INTERACTIVE, unattended=True, remembered_choices={"image": {
            "account": entry.account, "model": entry.model}}))
    assert output["status"] == "skipped" and output["reason"] == "choices_unreadable"
    backend.catalog.assert_not_awaited()
    assert backend.calls == []
