"""Provider -> model listing for the generation picker (operation, search, paging)."""
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import generation_choices as api
from app.core import generation_operations
from app.core.generation_resolver import INCLUDED_PRICE
from app.services.fal_catalog import CatalogModel
from app.services.fal_schema import ModelSchema, ParamField
from tests.core.test_generation_resolver import candidate

IDENTITY = ("account", "origin#owner")

FAL = [
    ("fal-ai/t2v-a", "text-to-video", "video"), ("fal-ai/t2v-b", "text-to-video", "video"),
    ("fal-ai/t2v-c", "text-to-video", "video"),
    ("fal-ai/i2v-a", "image-to-video", "video"), ("kling/v4/reference-to-video", "image-to-video", "video"),
    ("fal-ai/pixverse/extend", "video-to-video", "video"), ("fal-ai/upscale-video", "video-to-video", "video"),
    ("fal-ai/flux", "text-to-image", "image"), ("fal-ai/flux/edit", "image-to-image", "image"),
    ("fal-ai/stems", "audio-to-audio", "audio"), ("fal-ai/tts", "text-to-speech", "audio"),
    ("fal-ai/meshy/image-to-3d", "image-to-3d", "3d"),
]
ROW = {"unit_price": 0.1, "unit": "second", "currency": "USD"}


@pytest.fixture
def client(mock_db, monkeypatch):
    from app.core import fal_credential
    from app.core.provider import subscription_images
    from app.db import model_providers
    from app.network import generation, runners
    from app.services import fal_pricing
    from app.services.fal_catalog import catalog_service
    from app.services.fal_schema import schema_service

    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    monkeypatch.setattr(api, "request_preference_identity", lambda request: IDENTITY)
    monkeypatch.setattr(fal_credential, "api_key", lambda *a: "test-fal-key")
    monkeypatch.setattr(subscription_images, "plan_images_configured", lambda: False)
    monkeypatch.setattr(model_providers, "list_usable_model_providers", lambda: [])
    monkeypatch.setattr(catalog_service, "_cache", [
        CatalogModel(endpoint_id=e, display_name=e.split("/")[-1].title(), category=c, description=f"{c} model",
                     output_type=k) for e, c, k in FAL])
    monkeypatch.setattr(catalog_service, "_last_refresh", time.time())
    rate_reads = []

    async def rows(endpoints, key):
        rate_reads.append(list(endpoints))
        return {e: (None if e == "fal-ai/t2v-c" else ROW) for e in endpoints}
    monkeypatch.setattr(fal_pricing.rates, "rows", rows)
    schema_reads = AsyncMock(return_value=ModelSchema(
        "x", [ParamField("prompt", is_primary_text=True, required=True),
              ParamField("duration", type="integer", default=5, minimum=1, maximum=10)], "prompt"))
    monkeypatch.setattr(schema_service, "get_schema", schema_reads)
    monkeypatch.setattr(schema_service, "_cache", {})
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_auth] = lambda: "token"
    with TestClient(app) as test_client:
        test_client.rate_reads, test_client.schema_reads = rate_reads, schema_reads
        yield test_client


@pytest.mark.parametrize("kind,operation,expected", [
    ("video", None, ["fal-ai/t2v-a", "fal-ai/t2v-b", "fal-ai/t2v-c"]),
    ("video", "image-to-video", ["fal-ai/i2v-a", "kling/v4/reference-to-video"]),
    ("video", "reference-to-video", ["kling/v4/reference-to-video"]),
    ("video", "extend-video", ["fal-ai/pixverse/extend"]),
    ("image", "edit-image", ["fal-ai/flux/edit"]),
    ("audio", None, ["fal-ai/tts"]),
    ("audio", "audio-to-audio", ["fal-ai/stems"]),
    ("3d", "image-to-3d", ["fal-ai/meshy/image-to-3d"]),
])
def test_models_match_the_operation(client, kind, operation, expected):
    query = f"&operation={operation}" if operation else ""
    data = client.get(f"/api/v1/generation/models/{kind}?source=fal{query}").json()
    assert [m["model"] for m in data["models"]] == expected
    assert data["operation"] == (operation or generation_operations.TEXT_OPERATION[kind])


def test_models_search_and_page_server_side_without_per_row_schema_reads(client):
    first = client.get("/api/v1/generation/models/video?limit=2").json()
    assert [m["model"] for m in first["models"]] == ["fal-ai/t2v-a", "fal-ai/t2v-b"]
    assert first["total"] == 3 and first["has_more"] is True
    second = client.get("/api/v1/generation/models/video?limit=2&offset=2").json()
    assert [m["model"] for m in second["models"]] == ["fal-ai/t2v-c"] and second["has_more"] is False
    assert client.get("/api/v1/generation/models/video?query=T2V-B").json()["total"] == 1
    # One batched rate read per page and no schema fetch for listing.
    assert client.rate_reads[:2] == [["fal-ai/t2v-a", "fal-ai/t2v-b"], ["fal-ai/t2v-c"]]
    client.schema_reads.assert_not_called()
    assert first["models"][0]["name"] == "T2V-A" and first["models"][0]["category"] == "text-to-video"


def test_unpriced_rows_say_why_and_are_never_free(client):
    rows = {m["model"]: m for m in client.get("/api/v1/generation/models/video").json()["models"]}
    assert rows["fal-ai/t2v-a"]["status"] == "unpriced"
    assert rows["fal-ai/t2v-a"]["price"]["reason"] == "not_quoted"
    assert rows["fal-ai/t2v-a"]["price"]["rate"] == {"amount": "0.1", "unit": "second"}
    assert rows["fal-ai/t2v-c"]["price"]["reason"] == "no_pricing_row"
    assert all(r["price"]["total"] is None for r in rows.values())


def test_a_rate_limited_page_keeps_known_rates(client, monkeypatch):
    from app.services import fal_pricing

    async def limited(endpoints, key):
        raise fal_pricing.PricingRequestFailed("fal pricing is rate limited for now.")
    monkeypatch.setattr(fal_pricing.rates, "rows", limited)
    monkeypatch.setattr(fal_pricing.rates, "cached", lambda endpoints, key: {"fal-ai/t2v-a": ROW})
    rows = {m["model"]: m for m in client.get("/api/v1/generation/models/video").json()["models"]}
    assert rows["fal-ai/t2v-a"]["price"]["reason"] == "not_quoted"
    assert rows["fal-ai/t2v-a"]["price"]["rate"] == {"amount": "0.1", "unit": "second"}
    assert rows["fal-ai/t2v-b"]["price"]["reason"] == "pricing_request_failed"
    assert all(r["status"] == "unpriced" and r["price"]["total"] is None for r in rows.values())


def test_cached_schema_rows_carry_a_real_price(client):
    from app.services.fal_schema import schema_service
    schema = ModelSchema("fal-ai/t2v-a", [ParamField("prompt", is_primary_text=True, required=True),
                                          ParamField("duration", type="integer", default=5, minimum=1, maximum=10)],
                         "prompt")
    schema_service._cache["fal-ai/t2v-a"] = (time.time(), schema)
    row = client.get("/api/v1/generation/models/video?limit=1").json()["models"][0]
    assert row["status"] == "available" and row["price"]["total"] == "0.5"
    assert row["options"] == {"duration": 5} and row["schema"]["fields"]


def test_rejects_unknown_or_mismatched_operations(client):
    assert client.get("/api/v1/generation/models/video?operation=edit-image").status_code == 422
    assert client.get("/api/v1/generation/models/video?operation=nope").status_code == 422
    assert client.get("/api/v1/generation/providers/music").status_code == 404


def test_providers_list_fal_and_accounts_with_unavailable_reasons(client, monkeypatch):
    from app.network import generation
    peer = replace(candidate(source="supergrok", account="peer-plan", model="grok-video"), worker="worker-b",
                   kind="video", price=dict(INCLUDED_PRICE))
    down = replace(candidate(source="unavailable", account="worker-c", model="unavailable"), worker="worker-c",
                   kind="video", available=False, reason="Update that worker.")
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[peer, down]))
    data = client.get("/api/v1/generation/providers/video").json()
    rows = {p["account"]: p for p in data["providers"]}
    assert rows["fal-ai"]["available"] and rows["fal-ai"]["model_count"] == 3
    assert rows["peer-plan"]["available"] and rows["peer-plan"]["workers"] == ["worker-b"]
    assert rows["worker-c"]["available"] is False and rows["worker-c"]["reason"] == "Update that worker."
    assert generation.catalog.await_args.kwargs["operation"] == "text-to-video"
    # Subscription accounts only generate from a prompt: not offered for image-to-video.
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[]))
    data = client.get("/api/v1/generation/providers/video?operation=image-to-video").json()
    assert [p["source"] for p in data["providers"]] == ["fal"]
    assert data["providers"][0]["model_count"] == 2


def test_native_account_models_and_plan_image_rule(client, monkeypatch):
    from app.core.provider import subscription_images
    from app.network import generation
    peer = replace(candidate(source="supergrok", account="peer-plan", model="grok-imagine"), worker="worker-b",
                   price=dict(INCLUDED_PRICE))
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[peer]))
    data = client.get("/api/v1/generation/models/image?source=peer-plan").json()
    assert [(m["model"], m["status"]) for m in data["models"]] == [("grok-imagine", "available")]
    monkeypatch.setattr(subscription_images, "plan_images_configured", lambda: True)
    providers = client.get("/api/v1/generation/providers/image").json()["providers"]
    fal = next(p for p in providers if p["source"] == "fal")
    assert fal["available"] is False and "ChatGPT plan" in fal["reason"]
    assert client.get("/api/v1/generation/models/image").json()["models"] == []


def test_remote_fal_pages_and_older_peers(client, monkeypatch):
    from app.core import fal_credential
    from app.network import generation, runners
    monkeypatch.setattr(fal_credential, "api_key", lambda *a: None)
    monkeypatch.setattr(runners, "other_workers", lambda: [{"device_id": "worker-b", "online": True}])
    remote = candidate(source="fal", account="fal-ai", model="fal-ai/i2v-a")
    remote = replace(remote, worker="worker-b", kind="video")
    from dataclasses import asdict
    replies = {}

    async def request(worker, operation, **body):
        replies["body"] = body
        return replies["reply"]
    monkeypatch.setattr(generation, "request", request)
    # A current peer pages and filters by operation itself.
    replies["reply"] = {"version": 2, "candidates": [asdict(remote)], "media_operation": "image-to-video",
                        "page": {"total": 7, "offset": 0, "details": {"fal-ai/i2v-a": {"name": "I2V"}}}}
    data = client.get("/api/v1/generation/models/video?operation=image-to-video").json()
    assert data["total"] == 7 and data["models"][0]["name"] == "I2V" and data["models"][0]["worker"] == "worker-b"
    assert replies["body"]["media_operation"] == "image-to-video" and replies["body"]["page"]["limit"] == 20
    # An older peer ignores paging and lists text-to-* only: not used for other operations.
    replies["reply"] = {"version": 2, "candidates": [asdict(remote)]}
    assert client.get("/api/v1/generation/models/video?operation=image-to-video").json()["models"] == []
    text = client.get("/api/v1/generation/models/video").json()
    assert text["total"] == 1 and client.get("/api/v1/generation/models/video?offset=20").json()["models"] == []
