"""Choice-card API: remembered picks per account, Settings options and live quotes."""
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import generation_choices as api
from app.core.generation_resolver import Candidate
from app.services.generation_sources import Sources as ProductionSources
from tests.core.test_generation_resolver import FakeBackend, candidate

IDENTITY = ("account", "origin#owner")


def test_catalog_lists_status_price_and_worker_with_independent_favourite(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import generation_sources
    from app.core.generation_resolver import INCLUDED_PRICE, UNKNOWN_PRICE
    entries = [replace(candidate(source="chatgpt", account="plan"), price=dict(INCLUDED_PRICE)),
               replace(candidate(source="xai", account="api"), available=False, verified=False,
                       price=dict(UNKNOWN_PRICE), reason="not yet verified"),
               replace(candidate(source="openai", account="other"), price=dict(UNKNOWN_PRICE))]
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: FakeBackend(entries))
    pick = entries[0].choice()
    assert client.put("/api/v1/generation/favourites/image", json=pick).status_code == 200
    response = client.get("/api/v1/generation/catalog?kind=image&limit=99")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 50 and data["kinds"] == ["image"]
    assert [(r["source"], r["status"]) for r in data["entries"]] == [
        ("chatgpt", "available"), ("xai", "unavailable"), ("openai", "unpriced")]
    assert data["entries"][0]["worker"] == "worker-a"
    assert data["entries"][0]["price"] == INCLUDED_PRICE
    assert data["favourites"] == {"image": pick}
    # A bookmark is neither last-used nor consent for unattended generation.
    choices = client.get("/api/v1/generation/choices").json()
    assert choices["remembered"] == choices["last"] == {}
    assert client.delete("/api/v1/generation/favourites/image").json()["favourite"] is None
    assert client.get("/api/v1/generation/catalog?kind=image").json()["favourites"] == {}
    assert client.get("/api/v1/generation/catalog?kind=nope").status_code == 404




def test_catalog_uses_production_sources_and_lists_peer_and_disabled_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    from app.core import fal_credential, native_generation_settings as native
    from app.core.generation_resolver import INCLUDED_PRICE
    from app.db import database, model_providers
    from app.network import generation
    from app.services import generation_sources

    # The client fixture stubs Sources; this test exercises the production one.
    monkeypatch.setattr(generation_sources, "Sources", ProductionSources)
    rows = [{"id": "local-plan", "provider_type": "chatgpt"},
            {"id": "unverified-api", "provider_type": "openai", "auth_mode": "api_key"}]
    monkeypatch.setattr(fal_credential, "api_key", lambda *a: None)
    monkeypatch.setattr(model_providers, "list_usable_model_providers", lambda: rows)
    monkeypatch.setattr(
        database, "get_model_provider",
        lambda account, **kw: next(r for r in rows if r["id"] == account),
    )
    native.save("local-plan", native.NativeGenerationSettings(image=None, video=None))
    peer = replace(
        candidate(source="supergrok", account="peer-plan"),
        worker="worker-b", price=dict(INCLUDED_PRICE),
    )
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[peer]))
    data = client.get("/api/v1/generation/catalog?kind=image").json()
    entries = {row["account"]: row for row in data["entries"]}
    assert entries["local-plan"]["verified"] and entries["local-plan"]["status"] == "unavailable"
    assert entries["local-plan"]["price"] == INCLUDED_PRICE
    assert entries["local-plan"]["worker"] == "worker-a"
    assert (entries["unverified-api"]["status"] == "unavailable"
            and not entries["unverified-api"]["verified"])
    assert (entries["peer-plan"]["status"] == "available"
            and entries["peer-plan"]["worker"] == "worker-b")


def test_catalog_forwards_filters_and_collects_every_kind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import generation_sources
    calls = []

    class Backend(FakeBackend):
        async def catalog(self, kind: str, **kw: Any) -> list[Candidate]:
            calls.append((kind, kw))
            return []

    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: Backend([]))
    data = client.get("/api/v1/generation/catalog?source=openai&model=m&query=cat&limit=3").json()
    assert data["kinds"] == ["image", "video", "audio", "3d"]
    assert calls == [
        (k, {"source": "openai", "model": "m", "query": "cat", "limit": 3}) for k in data["kinds"]
    ]


def test_job_writer_binds_real_session_to_unattended_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json
    from app.api import automations
    from app.core import generation_choices, model_effort, query_options
    from app.db import database
    from app.mcp import ai_tools_sdk
    from app.services import generation_sources

    client.app.dependency_overrides[api.require_admin] = lambda: "owner"
    monkeypatch.setattr(model_effort, "_caller_preference_identity", lambda row: IDENTITY)
    client.app.include_router(automations.router)
    created = client.post("/api/v1/automations", json={
        "name": "Draw", "trigger_type": "interval", "trigger_config": {"seconds": 3600},
        "profile_id": "default", "prompt": "Draw a tree",
    })
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    assert job_id.startswith("auto-") and len(job_id) == 13
    assert client.get(f"/api/v1/generation/jobs/{job_id}/choices").json() == {
        "job_id": job_id, "choices": {},
    }
    entry = candidate()
    pick = entry.choice()
    route = f"/api/v1/generation/jobs/{job_id}/choices/image"
    response = client.put(route, json=pick)
    assert response.status_code == 200 and response.json()["choice"] == pick
    assert client.get(f"/api/v1/generation/jobs/{job_id}/choices").json()["choices"] == {
        "image": pick,
    }
    assert client.get("/api/v1/generation/choices").json()["remembered"] == {}
    database.create_session("run", "default", automation_id=job_id)
    row = database.get_session("run")
    # The production query-options binder, with the row the scheduler writes.
    context = query_options._generation_context({"image_generation": True}, row, None,
                                                session_id="run", unattended=True)
    assert context.job_choices == {"image": pick}
    backend = FakeBackend([entry])
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: backend)
    tool = next(
        t for t in ai_tools_sdk._create_unified_tools(None, context) if t.name == "generateImage"
    )
    # Tool arguments cannot replace the trusted job binding in unattended runs.
    output = asyncio.run(tool.handler({"prompt": "tree", "model": "other", "source": "xai"}))
    assert json.loads(output["content"][0]["text"])["status"] == "completed"
    assert backend.calls[-2:] == [("reserve", entry.account), ("execute", entry.account)]
    assert client.delete(route).status_code == 200
    assert generation_choices.load_job(IDENTITY, job_id) == {}
    assert client.put(route, json={"source": "fal", "model": "m"}).status_code == 422
    assert client.put(
        "/api/v1/generation/jobs/auto-ffffffff/choices/image", json=pick,
    ).status_code == 404


@pytest.mark.parametrize("job_id", [
    "auto-ffffffff", "auto-12345678.image", "x" * 129, "auto-12345678' OR '1'='1",
])
def test_job_routes_reject_unknown_or_unsafe_ids(client: TestClient, job_id: str) -> None:
    client.app.dependency_overrides[api.require_admin] = lambda: "owner"
    route = f"/api/v1/generation/jobs/{job_id}/choices"
    assert client.get(route).status_code == 404
    assert client.put(route + "/image", json={"source": "fal", "model": "m"}).status_code == 404
    assert client.delete(route + "/image").status_code == 404


def test_job_choices_require_owner_even_for_preference_writers(client: TestClient) -> None:
    route = "/api/v1/generation/jobs/auto-12345678/choices"
    assert client.get(route).status_code == 403
    assert client.put(route + "/image", json={"source": "fal", "model": "x"}).status_code == 403
    assert client.delete(route + "/image").status_code == 403



@pytest.fixture
def client(mock_db, monkeypatch):
    from app.network import runners
    from app.services import generation_sources
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: FakeBackend([]))
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    monkeypatch.setattr(api, "request_preference_identity", lambda request: IDENTITY)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_auth] = lambda: "token"
    app.dependency_overrides[api.require_preference_write] = lambda: "token"
    with TestClient(app) as test_client:
        yield test_client


def test_pick_is_remembered_listed_and_forgotten(client):
    assert client.get("/api/v1/generation/choices").json() == {
        "remembered": {}, "last": {}, "migrated": True, "worker": "worker-a", "kinds": ["image", "video", "audio", "3d"]}
    pick = {"source": "fal", "account": "fal-ai", "worker": "worker-a", "model": "fal-ai/x", "options": {"size": "wide"}}
    response = client.put("/api/v1/generation/choices/image", json={"choice": pick, "remember": True})
    assert response.status_code == 200 and response.json()["remembered"] is True
    listed = client.get("/api/v1/generation/choices").json()
    assert listed["remembered"]["image"] == {**pick, "origin": "card"} == listed["last"]["image"]
    assert client.delete("/api/v1/generation/choices/image").json() == {"kind": "image", "forgotten": True}
    listed = client.get("/api/v1/generation/choices").json()
    assert "image" not in listed["remembered"] and listed["last"]["image"]["model"] == "fal-ai/x"


def test_pick_without_remember_only_becomes_the_last_choice(client):
    pick = {"source": "supergrok", "account": "grok-1", "model": "grok-imagine-image"}
    client.put("/api/v1/generation/choices/image", json={"choice": pick, "remember": False, "origin": "settings"})
    listed = client.get("/api/v1/generation/choices").json()
    assert listed["remembered"] == {} and listed["last"]["image"]["origin"] == "settings"


@pytest.mark.parametrize("path,body,status", [
    ("/api/v1/generation/choices/music", {"choice": {"source": "fal", "model": "x"}}, 404),
    ("/api/v1/generation/choices/image", {"choice": {"model": "x"}}, 422),
    ("/api/v1/generation/choices/image", {"choice": {"source": "fal", "model": "x"}, "origin": "agent"}, 422),
    ("/api/v1/generation/choices/image", {"choice": {"source": "fal", "model": "x", "options": "wide"}}, 422),
])
def test_invalid_picks_are_rejected(client, path, body, status):
    assert client.put(path, json=body).status_code == status


def test_preview_cookie_cannot_write_choices(mock_db, monkeypatch):
    from app.core import preview_access
    monkeypatch.setattr(api, "request_preference_identity", lambda request: IDENTITY)
    monkeypatch.setattr(preview_access, "grant", lambda request: {"session": "s"})
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_auth] = lambda: "token"
    from app.api import preferences
    app.dependency_overrides[preferences.require_auth] = lambda: "token"
    with TestClient(app) as client:
        response = client.put("/api/v1/generation/choices/image", json={"choice": {"source": "fal", "model": "x"}})
    assert response.status_code == 403


@pytest.mark.parametrize("preview", [False, True])
def test_preview_cookie_lists_choices_without_running_the_migration(mock_db, monkeypatch, preview):
    from app.core import generation_choices, preview_access
    from app.network import runners
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    monkeypatch.setattr(api, "request_preference_identity", lambda request: IDENTITY)
    monkeypatch.setattr(preview_access, "grant", lambda request: {"session": "s"} if preview else None)
    migrations = []
    monkeypatch.setattr(generation_choices, "migrate", lambda identity: migrations.append(identity))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_auth] = lambda: "token"
    with TestClient(app) as client:
        response = client.get("/api/v1/generation/choices")
    assert response.status_code == 200 and response.json()["remembered"] == {}
    assert migrations == ([] if preview else [IDENTITY])


class QuoteBackend(FakeBackend):
    async def catalog(self, kind, **kw):
        self.calls.append(("catalog", kw.get("source"), kw.get("model"), kw.get("worker")))
        return [c for c in self.entries if c.kind == kind]

    async def prepare(self, c, options, prompt):
        self.calls.append(("prepare", c.account, options))
        if options.get("size") == "bad":
            raise ValueError("Model options do not match the declared input schema.")
        total = "0.10" if options.get("size") == "wide" else "0.05"
        return replace(c, options=options, price={**c.price, "amount": total, "total": total})


@pytest.fixture
def quote_backend(monkeypatch):
    from app.services import generation_sources
    backend = QuoteBackend([candidate(), candidate(account="account-b", model="model-b")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    return backend


def test_quote_reprices_as_options_change_without_reserving(client, quote_backend):
    body = {"kind": "image", "choice": {"source": "fal", "account": "account-a", "worker": "worker-a", "model": "model-a",
                                        "options": {"size": "wide"}}, "prompt": "a tree"}
    output = client.post("/api/v1/generation/quote", json=body).json()
    assert output["ok"] is True and output["priced"] is True
    assert output["price"]["total"] == "0.10" and output["options"] == {"size": "wide"}
    body["choice"]["options"] = {"size": "square"}
    assert client.post("/api/v1/generation/quote", json=body).json()["price"]["total"] == "0.05"
    assert not any(call[0] in ("reserve", "execute") for call in quote_backend.calls)
    assert ("catalog", "account-a", "model-a", "worker-a") in quote_backend.calls


def test_quote_reports_invalid_options_and_missing_accounts(client, quote_backend):
    body = {"kind": "image", "choice": {"account": "account-a", "model": "model-a", "options": {"size": "bad"}}}
    output = client.post("/api/v1/generation/quote", json=body).json()
    assert output["ok"] is False and "declared" in output["error"]
    body["choice"] = {"account": "gone", "model": "model-a"}
    assert client.post("/api/v1/generation/quote", json=body).json()["ok"] is False


def test_options_list_priced_choices_for_the_settings_picker(client, quote_backend):
    output = client.get("/api/v1/generation/options/image").json()
    assert [o["account"] for o in output["options"]] == ["account-a", "account-b"]
    assert all("price" in o and "schema" in o for o in output["options"])
    assert client.get("/api/v1/generation/options/music").status_code == 404
