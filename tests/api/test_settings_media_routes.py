"""Media tool settings (``app.api.settings_media``) and the model catalog
(``app.api.catalog``) through the real app.

The media setters are owner-only; an API user's Bearer key gets ``403`` and
writes nothing. The catalog is open to any authenticated caller. fal.ai's
platform API is served by an ``httpx.MockTransport`` handler installed on the
catalog/schema services' own ``httpx`` names, against fresh service instances.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.api import catalog as catalog_api
from app.api.auth import hash_api_key
from app.api.settings_crypto import set_encrypted_api_key
from app.db import database
from app.services import fal_catalog, fal_schema
from tests.fakes.http import mock_transport_client

BASE = "/api/v1/settings"
USER_KEY = "aih_media_user_key"
FAL_KEY = "fal-catalog-key-0123456789abcdefghijkl"

MODEL_ROUTES = [
    ("/integrations/image/model", "image_model"),
    ("/integrations/image/edit-model", "image_edit_model"),
    ("/integrations/video/model", "video_model"),
    ("/integrations/video/i2v-model", "i2v_model"),
    ("/integrations/3d/model", "model3d_model"),
    ("/integrations/3d/i2-3d-model", "image_to_3d_model"),
    ("/integrations/audio/generation-model", "audio_model"),
    ("/integrations/audio/audio-to-audio-model", "audio_to_audio_model"),
]

OWNER_ONLY = [
    *(("patch", path, {"model": "fal-ai/x"}) for path, _ in MODEL_ROUTES),
    ("patch", "/integrations/image/settings", {"aspect_ratio": "1:1"}),
    ("patch", "/integrations/video/settings", {"duration": 5}),
    ("patch", "/integrations/stt/settings", {"language": "fr"}),
    ("get", "/integrations/model-params?key=image_model", None),
    ("patch", "/integrations/model-params", {"key": "image_model", "params": {"seed": 1}}),
]


@pytest.fixture
def c(authenticated_client):
    return authenticated_client


@pytest.fixture
def api_user(mock_db):
    database.create_api_user("media-user", "Media User", hash_api_key(USER_KEY))
    return {"Authorization": f"Bearer {USER_KEY}"}


# ---------------------------------------------------------------------------
# Owner boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", OWNER_ONLY)
def test_media_settings_refuse_api_users(client, api_user, method, path, body):
    database.set_system_setting("image_model_params", json.dumps({"guidance_scale": 3}))
    kwargs = {"headers": api_user}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(BASE + path, **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Owner access required"
    assert "guidance_scale" not in r.text
    for _, key in MODEL_ROUTES:
        assert database.get_system_setting(key) is None
    for key in ("image_aspect_ratio", "video_duration", "stt_language"):
        assert database.get_system_setting(key) is None
    assert database.get_system_setting("image_model_params") == json.dumps({"guidance_scale": 3})


@pytest.mark.parametrize("method,path,body", OWNER_ONLY[:3])
def test_media_settings_refuse_anonymous_callers(client, method, path, body):
    assert getattr(client, method)(BASE + path, json=body).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,key", MODEL_ROUTES)
def test_model_setters_trim_save_and_read_back(c, path, key):
    r = c.patch(BASE + path, json={"model": "  fal-ai/flux-pro/v1.1-ultra \n"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "model": "fal-ai/flux-pro/v1.1-ultra"}
    assert database.get_system_setting(key) == "fal-ai/flux-pro/v1.1-ultra"
    assert c.get(f"{BASE}/integrations").json()[key] == "fal-ai/flux-pro/v1.1-ultra"


@pytest.mark.parametrize("path,key", MODEL_ROUTES)
def test_model_setters_reject_blank_and_keep_the_old_value(c, path, key):
    database.set_system_setting(key, "fal-ai/kept")
    r = c.patch(BASE + path, json={"model": "   "})
    assert r.status_code == 400
    assert r.json()["detail"] == "Model cannot be empty"
    assert database.get_system_setting(key) == "fal-ai/kept"


def test_model_setters_need_a_model_field(c):
    assert c.patch(f"{BASE}/integrations/image/model", json={}).status_code == 422


def test_integrations_defaults_when_nothing_is_saved(c):
    body = c.get(f"{BASE}/integrations").json()
    for _, key in MODEL_ROUTES:
        assert body[key] is None
    assert body["tts_model"] == "fal-ai/kokoro/american-english"
    assert body["tts_voice"] == "af_heart"
    assert body["tts_speed"] == 1.0
    assert body["tts_output_format"] == "wav"
    assert body["stt_model"] == "fal-ai/whisper"
    for key in ("image_aspect_ratio", "image_resolution", "video_aspect_ratio",
                "video_duration", "video_resolution", "stt_language"):
        assert body[key] is None


# ---------------------------------------------------------------------------
# Generation defaults
# ---------------------------------------------------------------------------

def test_image_and_video_defaults_save_only_sent_fields(c):
    database.set_system_setting("image_resolution", "2K")
    assert c.patch(f"{BASE}/integrations/image/settings", json={"aspect_ratio": "16:9"}).json() == {"success": True}
    assert c.patch(f"{BASE}/integrations/video/settings", json={"duration": 8, "resolution": "1080p"}).status_code == 200
    body = c.get(f"{BASE}/integrations").json()
    assert body["image_aspect_ratio"] == "16:9"
    assert body["image_resolution"] == "2K"
    assert body["video_duration"] == 8
    assert body["video_resolution"] == "1080p"
    assert body["video_aspect_ratio"] is None
    assert c.patch(f"{BASE}/integrations/image/settings", json={"resolution": ""}).status_code == 200
    assert database.get_system_setting("image_resolution") == ""


def test_video_duration_must_be_an_integer(c):
    r = c.patch(f"{BASE}/integrations/video/settings", json={"duration": "long"})
    assert r.status_code == 422
    assert database.get_system_setting("video_duration") is None


def test_stt_language_default(c):
    assert c.patch(f"{BASE}/integrations/stt/settings", json={}).json() == {"success": True}
    assert database.get_system_setting("stt_language") is None
    assert c.patch(f"{BASE}/integrations/stt/settings", json={"language": "de"}).status_code == 200
    assert c.get(f"{BASE}/integrations").json()["stt_language"] == "de"


# ---------------------------------------------------------------------------
# Schema-driven model params
# ---------------------------------------------------------------------------

def test_model_params_round_trip_drops_blank_values(c):
    r = c.patch(f"{BASE}/integrations/model-params", json={
        "key": " image_model ", "params": {"guidance_scale": 3.5, "seed": "", "style": None, "steps": 0}})
    assert r.status_code == 200
    assert r.json() == {"success": True, "params": {"guidance_scale": 3.5, "steps": 0}}
    got = c.get(f"{BASE}/integrations/model-params", params={"key": "image_model"})
    assert got.json() == {"key": "image_model", "params": {"guidance_scale": 3.5, "steps": 0}}
    assert c.patch(f"{BASE}/integrations/model-params", json={"key": "image_model", "params": {}}).status_code == 200
    assert c.get(f"{BASE}/integrations/model-params?key=image_model").json()["params"] == {}


def test_model_params_blank_key_is_400(c):
    r = c.patch(f"{BASE}/integrations/model-params", json={"key": "  ", "params": {"a": 1}})
    assert r.status_code == 400
    assert r.json()["detail"] == "key cannot be empty"
    assert c.get(f"{BASE}/integrations/model-params").status_code == 422


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "\"text\""])
def test_model_params_unreadable_saved_value_reads_as_empty(c, raw):
    database.set_system_setting("video_model_params", raw)
    assert c.get(f"{BASE}/integrations/model-params?key=video_model").json() == {"key": "video_model", "params": {}}


def test_unknown_model_params_key_reads_empty(c):
    assert c.get(f"{BASE}/integrations/model-params?key=nope").json() == {"key": "nope", "params": {}}


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

def _catalog_model(endpoint_id, category, name, tags=(), status="active"):
    return {"endpoint_id": endpoint_id, "metadata": {
        "display_name": name, "category": category, "description": f"{name} model",
        "status": status, "tags": list(tags), "group": {"key": "g", "label": "G"}}}


CATALOG_PAGES = [
    {"models": [
        _catalog_model("fal-ai/flux/dev", "text-to-image", "FLUX dev", tags=("fast",)),
        _catalog_model("fal-ai/kling/video", "text-to-video", "Kling"),
        _catalog_model("fal-ai/old", "text-to-image", "Old", status="deprecated"),
        _catalog_model("fal-ai/trainer", "training", "Trainer"),
    ], "has_more": True, "next_cursor": "page2"},
    {"models": [_catalog_model("fal-ai/stable-audio", "text-to-audio", "Stable Audio")], "has_more": False},
]


@pytest.fixture
def catalog(monkeypatch):
    service = fal_catalog.FalCatalogService()
    monkeypatch.setattr(catalog_api, "catalog_service", service)
    calls = []

    def handler(request):
        calls.append(request)
        page = 1 if request.url.params.get("cursor") == "page2" else 0
        return httpx.Response(200, json=CATALOG_PAGES[page])

    monkeypatch.setattr(fal_catalog, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(handler)))
    return SimpleNamespace(service=service, calls=calls)


def test_catalog_list_refreshes_a_stale_cache_with_the_fal_key(c, catalog):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    body = c.get("/api/v1/catalog/models").json()
    assert [m["endpoint_id"] for m in body["models"]] == ["fal-ai/flux/dev", "fal-ai/kling/video", "fal-ai/stable-audio"]
    assert body["total"] == 3 and body["catalog_size"] == 3
    assert set(body["models"][0]) == {
        "endpoint_id", "display_name", "category", "description", "status", "thumbnail_url", "tags",
        "group_key", "group_label", "updated_at", "tool_type", "output_type"}
    assert len(catalog.calls) == 2
    assert catalog.calls[0].headers["Authorization"] == f"Key {FAL_KEY}"
    assert FAL_KEY not in c.get("/api/v1/catalog/models").text
    # Fresh cache: no second fetch.
    assert len(catalog.calls) == 2


@pytest.mark.parametrize("params,expected", [
    ({"output_type": "video"}, ["fal-ai/kling/video"]),
    ({"category": "text-to-image"}, ["fal-ai/flux/dev"]),
    ({"q": "FAST"}, ["fal-ai/flux/dev"]),
    ({"q": "stable", "output_type": "audio"}, ["fal-ai/stable-audio"]),
    ({"q": "nothing-matches"}, []),
])
def test_catalog_list_filters(c, catalog, params, expected):
    body = c.get("/api/v1/catalog/models", params=params).json()
    assert [m["endpoint_id"] for m in body["models"]] == expected
    assert body["total"] == len(expected)
    assert body["catalog_size"] == 3


def test_catalog_upstream_failure_keeps_serving_the_cache(c, catalog, monkeypatch):
    c.post("/api/v1/catalog/refresh")
    monkeypatch.setattr(fal_catalog, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(lambda r: httpx.Response(503))))
    catalog.service._last_refresh = 0
    body = c.get("/api/v1/catalog/models").json()
    assert body["total"] == 3
    # Regression: a failed refresh answered success with the cached count as
    # "models_fetched"; it is a 502 that names the error and the cache kept.
    r = c.post("/api/v1/catalog/refresh")
    assert r.status_code == 502
    body = r.json()
    assert body["success"] is False
    assert body["error"] == "fal.ai answered HTTP 503"
    assert body["cached_models"] == 3
    assert "models_fetched" not in body
    assert "3 cached models" in body["detail"]
    assert {row["category"] for row in body["categories"]} == {"text-to-image", "text-to-video", "text-to-audio"}
    assert c.get("/api/v1/catalog/models").json()["total"] == 3


@pytest.mark.parametrize("handler,error", [
    (lambda r: httpx.Response(200, json={"models": [], "has_more": False}), "fal.ai returned no models"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("offline")), "could not reach fal.ai (ConnectError)"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "fal.ai did not answer in time"),
])
def test_catalog_refresh_failure_on_an_empty_cache_is_502(c, monkeypatch, handler, error):
    service = fal_catalog.FalCatalogService()
    monkeypatch.setattr(catalog_api, "catalog_service", service)
    monkeypatch.setattr(fal_catalog, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(handler)))
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    r = c.post("/api/v1/catalog/refresh")
    assert r.status_code == 502
    assert r.json()["success"] is False
    assert r.json()["error"] == error
    assert r.json()["cached_models"] == 0
    assert FAL_KEY not in r.text


def test_catalog_refresh_and_categories(c, catalog):
    r = c.post("/api/v1/catalog/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["models_fetched"] == 3
    categories = c.get("/api/v1/catalog/categories").json()
    assert categories["total_models"] == 3
    assert {row["category"]: row["count"] for row in categories["categories"]} == {
        "text-to-image": 1, "text-to-video": 1, "text-to-audio": 1}
    assert all(set(row) == {"category", "count", "tool_type"} for row in categories["categories"])


def test_catalog_schema_found_and_missing(c, monkeypatch):
    service = fal_schema.FalSchemaService()
    monkeypatch.setattr(catalog_api, "schema_service", service)
    openapi = {
        "paths": {"/": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Input"}}}}}}},
        "components": {"schemas": {"Input": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "title": "Prompt"},
            "num_steps": {"type": "integer", "minimum": 1, "maximum": 50, "default": 28}}}}},
    }

    def handler(request):
        if request.url.params["endpoint_id"] == "fal-ai/flux/dev":
            return httpx.Response(200, json={"models": [{"endpoint_id": "fal-ai/flux/dev", "openapi": openapi}]})
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(fal_schema, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(handler)))
    found = c.get("/api/v1/catalog/schema", params={"endpoint_id": "fal-ai/flux/dev"}).json()
    assert found["found"] is True
    schema = found["schema"]
    assert schema["endpoint_id"] == "fal-ai/flux/dev"
    assert schema["primary_text_field"] == "prompt"
    steps = next(f for f in schema["fields"] if f["name"] == "num_steps")
    assert (steps["type"], steps["minimum"], steps["maximum"], steps["default"]) == ("integer", 1, 50, 28)
    missing = c.get("/api/v1/catalog/schema", params={"endpoint_id": "fal-ai/none"}).json()
    assert missing == {"found": False, "endpoint_id": "fal-ai/none"}
    assert c.get("/api/v1/catalog/schema").status_code == 422


def test_catalog_is_open_to_api_users_but_not_anonymous(client, api_user, catalog):
    assert client.get("/api/v1/catalog/categories", headers=api_user).status_code == 200
    assert client.get("/api/v1/catalog/categories").status_code in (401, 403)
