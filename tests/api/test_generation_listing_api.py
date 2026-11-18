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
    # A prompt-only category fal may add, a reference endpoint filed under
    # image-to-image, and an endpoint without a category.
    ("fal-ai/music", "text-to-music", "audio"), ("fal-ai/vidu/reference-to-image", "image-to-image", "image"),
    ("fal-ai/mystery-video", "", "video"),
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
    # Reference endpoints share fal's image-to-video category but are not single-image.
    ("video", "image-to-video", ["fal-ai/i2v-a"]),
    ("video", "reference-to-video", ["kling/v4/reference-to-video"]),
    ("video", "extend-video", ["fal-ai/pixverse/extend"]),
    ("image", "edit-image", ["fal-ai/flux/edit"]),
    ("image", "reference-to-image", ["fal-ai/flux/edit", "fal-ai/vidu/reference-to-image"]),
    # Every prompt-only audio category, as the text-to-* rule always took.
    ("audio", None, ["fal-ai/tts", "fal-ai/music"]),
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
                                          ParamField("duration", type="integer", default=5, minimum=1, maximum=10,
                                                     description="Duration of the video in seconds.")],
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
    assert data["providers"][0]["model_count"] == 1


def test_fal_without_a_key_is_listed_unavailable_with_a_reason(client, monkeypatch):
    from app.core import fal_credential
    monkeypatch.setattr(fal_credential, "api_key", lambda *a: None)
    providers = client.get("/api/v1/generation/providers/video").json()["providers"]
    assert [(p["source"], p["available"]) for p in providers] == [("fal", False)]
    assert "No fal.ai API key" in providers[0]["reason"] and "model_count" not in providers[0]
    assert client.get("/api/v1/generation/models/video").json()["models"] == []


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


def _peers(monkeypatch, replies, *, keyed=False, offline=()):
    """Peers answering ``catalog`` with ``replies[device]``; returns the request log.
    The caller (worker-a) holds a fal key only when ``keyed``."""
    from app.core import fal_credential
    from app.network import generation, runners
    if not keyed:
        monkeypatch.setattr(fal_credential, "api_key", lambda *a: None)
    monkeypatch.setattr(runners, "other_workers", lambda: [
        {"device_id": d, "device_name": d.replace("worker", "Box"), "online": d not in offline} for d in replies])
    monkeypatch.setattr(runners, "find", lambda d: {"device_id": d, "device_name": d.replace("worker", "Box")})
    asked = []

    async def request(worker, operation, **body):
        asked.append((worker, body))
        return replies[worker]
    monkeypatch.setattr(generation, "request", request)
    return asked


def _page(device, models, total, *, serves=True, operation="text-to-video", **extra):
    from dataclasses import asdict
    rows = [asdict(replace(candidate(source="fal", account="fal-ai", model=m), worker=device, kind="video"))
            for m in models]
    page = {"total": total, "offset": 0, "details": {m: {"name": m.upper()} for m in models}}
    if serves is not None:
        page["serves"] = serves
    return {"version": 2, "candidates": rows, "media_operation": operation, "page": page, **extra}


def _legacy(device):
    from dataclasses import asdict
    remote = asdict(replace(candidate(source="fal", account="fal-ai", model="fal-ai/t2v-a"), worker=device, kind="video"))
    return {"version": 2, "candidates": [remote]}  # older code: no page, no operation echo


def _fal_rows(client, query=""):
    return [p for p in client.get(f"/api/v1/generation/providers/video{query}").json()["providers"]
            if p["source"] == "fal"]


def test_a_remote_page_comes_from_the_named_worker(client, monkeypatch):
    asked = _peers(monkeypatch, {"worker-b": _page("worker-b", ["fal-ai/i2v-a"], 7, operation="image-to-video")})
    data = client.get("/api/v1/generation/models/video?operation=image-to-video&worker=worker-b").json()
    assert data["total"] == 7 and data["models"][0]["name"] == "FAL-AI/I2V-A" and data["worker"] == "worker-b"
    assert data["models"][0]["worker"] == "worker-b" and data["notice"] is None and data["available"] is True
    assert [w for w, _ in asked] == ["worker-b"]
    assert asked[0][1]["media_operation"] == "image-to-video" and asked[0][1]["page"] == {"offset": 0, "limit": 20}


def test_every_keyed_worker_is_its_own_fal_provider(client, monkeypatch):
    # Local key + a current peer with a key + a legacy peer with a key.
    replies = {"worker-b": _page("worker-b", ["fal-ai/t2v-b"], 90), "worker-c": _legacy("worker-c")}
    asked = _peers(monkeypatch, replies, keyed=True)
    rows = {(p["worker"], p["worker_name"]): p for p in _fal_rows(client)}
    assert set(rows) == {("worker-a", "This device"), ("worker-b", "Box-b"), ("worker-c", "Box-c")}
    assert rows["worker-a", "This device"]["available"] and rows["worker-a", "This device"]["model_count"] == 3
    assert rows["worker-b", "Box-b"]["available"] and rows["worker-b", "Box-b"]["model_count"] == 90
    legacy = rows["worker-c", "Box-c"]
    assert legacy["available"] is False and legacy["reason"] == "Update Box-c to browse its fal.ai models here."
    # Each row's models come from that worker only; there is no implicit winner.
    asked.clear()
    local = client.get("/api/v1/generation/models/video?worker=worker-a").json()
    assert local["worker"] == "worker-a" and local["total"] == 3 and asked == []
    remote = client.get("/api/v1/generation/models/video?worker=worker-b").json()
    assert remote["worker"] == "worker-b" and remote["total"] == 90 and [w for w, _ in asked] == ["worker-b"]
    # A zero-match search on the chosen worker is simply empty.
    replies["worker-b"] = _page("worker-b", [], 0)
    empty = client.get("/api/v1/generation/models/video?worker=worker-b&query=zzz").json()
    assert (empty["models"], empty["total"], empty["available"], empty["notice"]) == ([], 0, True, None)


def test_one_serving_worker_keeps_a_single_fal_row_and_no_key_anywhere_is_one_reason(client, monkeypatch):
    replies = {"worker-c": _page("worker-c", [], 0, serves=False), "worker-b": _page("worker-b", ["fal-ai/t2v-a"], 3)}
    _peers(monkeypatch, replies)
    rows = _fal_rows(client)
    assert [(r["worker"], r["available"], r["model_count"]) for r in rows] == [("worker-b", True, 3)]
    # A peer before ``serves`` that sends an empty page is treated as having no fal.
    replies["worker-c"] = _page("worker-c", [], 0, serves=None)
    assert [r["worker"] for r in _fal_rows(client)] == ["worker-b"]
    replies["worker-b"] = _page("worker-b", [], 0, serves=False)
    rows = _fal_rows(client)
    assert len(rows) == 1 and rows[0]["available"] is False and "No fal.ai API key" in rows[0]["reason"]


def test_a_worker_that_stops_answering_between_pages_is_named_never_replaced(client, monkeypatch):
    replies = {"worker-b": _page("worker-b", ["fal-ai/t2v-a"], 30), "worker-c": _page("worker-c", ["fal-ai/t2v-b"], 90)}
    asked = _peers(monkeypatch, replies)
    data = client.get("/api/v1/generation/models/video?offset=20&worker=worker-c").json()
    assert [w for w, _ in asked] == ["worker-c"] and data["worker"] == "worker-c" and data["total"] == 90
    asked.clear()
    # It no longer serves fal: nothing from another worker, and it says so.
    replies["worker-c"] = _page("worker-c", [], 0, serves=False)
    data = client.get("/api/v1/generation/models/video?offset=20&worker=worker-c").json()
    assert data["models"] == [] and data["available"] is False and [w for w, _ in asked] == ["worker-c"]
    assert data["notice"] == "Box-c is not serving fal.ai models right now. Reload to try again."
    # Offline: same, and no peer is asked.
    asked = _peers(monkeypatch, replies, offline=("worker-c",))
    data = client.get("/api/v1/generation/models/video?offset=20&worker=worker-c").json()
    assert data["available"] is False and data["total"] == 0 and asked == [] and "Reload" in data["notice"]


def test_older_peers_are_named_for_an_update_not_shown_as_a_whole_catalog(client, monkeypatch):
    replies = {"worker-b": _legacy("worker-b")}
    _peers(monkeypatch, replies)
    for operation in ("", "&operation=image-to-video"):
        data = client.get(f"/api/v1/generation/models/video?worker=worker-b{operation}").json()
        assert data["models"] == [] and data["total"] == 0 and data["available"] is False
        assert data["notice"] == "Update Box-b to browse its fal.ai models here."
    fal = _fal_rows(client)
    assert len(fal) == 1 and fal[0]["available"] is False and fal[0]["reason"].startswith("Update Box-b")
    # A paged reply that echoes another operation is not used either.
    replies["worker-b"] = _page("worker-b", ["fal-ai/t2v-a"], 3, operation="image-to-video")
    data = client.get("/api/v1/generation/models/video?worker=worker-b").json()
    assert data["models"] == [] and data["notice"].startswith("Update Box-b")
    assert _fal_rows(client)[0]["reason"].startswith("Update Box-b")


def test_fal_availability_does_not_hang_on_one_models_schema(client):
    # The first text-to-video endpoint has no schema; fal stays selectable.
    client.schema_reads.return_value = None
    fal = next(p for p in client.get("/api/v1/generation/providers/video").json()["providers"] if p["source"] == "fal")
    assert fal["available"] is True and fal["model_count"] == 3 and fal["reason"] is None
    client.schema_reads.assert_not_called()


def test_a_named_detail_lookup_reads_no_listing_price(client):
    # The card's detail lookup for a browsed row is priced fresh by /quote
    # next; it must not read (or queue behind) a listing rate read first.
    url = ("/api/v1/generation/catalog?kind=video&source=fal&model=fal-ai/t2v-a&limit=1"
           "&operation=text-to-video&worker=worker-a")
    entry = client.get(url).json()["entries"][0]
    assert client.rate_reads == [] and entry["model"] == "fal-ai/t2v-a" and entry["worker"] == "worker-a"
    assert entry["price"]["total"] is None and entry["options"] == {"duration": 5}


def _edit_schema(endpoint, *fields, required=()):
    from app.services.fal_schema import ModelSchema
    props = {f.name: ({"type": "array", "items": {"type": "string"}, **({"minItems": f.minimum} if f.minimum else {})}
                      if f.type == "array" else {"type": f.type}) for f in fields}
    return ModelSchema(endpoint, [ParamField("prompt", is_primary_text=True, required=True), *fields], "prompt",
                       input_schema={"type": "object", "properties": {"prompt": {"type": "string"}, **props},
                                     "required": ["prompt", *required]})


@pytest.mark.parametrize("schema,problem", [
    # One image in image_url, as editImage sends it.
    (_edit_schema("fal-ai/flux/edit", ParamField("image_url", type="string", required=True), required=["image_url"]),
     None),
    # A list field takes editImage's one image as a one-item list.
    (_edit_schema("fal-ai/flux/edit", ParamField("image_urls", type="array", required=True), required=["image_urls"]),
     None),
    # Needs at least two images: editImage sends one.
    (_edit_schema("fal-ai/flux/edit", ParamField("image_urls", type="array", required=True, minimum=2),
                  required=["image_urls"]), "This model's declared inputs do not fit editImage."),
    # A required field editImage never fills.
    (_edit_schema("fal-ai/flux/edit", ParamField("image_url", type="string", required=True),
                  ParamField("mask_url", type="string", required=True), required=["image_url", "mask_url"]),
     "This model needs mask_url, which editImage does not send."),
    # No image input at all.
    (_edit_schema("fal-ai/flux/edit"), "This model does not take an image, which editImage sends."),
])
def test_a_browsed_models_inputs_must_fit_the_tool(client, schema, problem):
    client.schema_reads.return_value = schema
    url = "/api/v1/generation/catalog?kind=image&source=fal&model=fal-ai/flux/edit&limit=1&operation=edit-image"
    entry = client.get(url).json()["entries"][0]
    assert (entry["available"], entry["reason"]) == (problem is None, problem)
    assert entry["status"] == ("unavailable" if problem else "unpriced")
