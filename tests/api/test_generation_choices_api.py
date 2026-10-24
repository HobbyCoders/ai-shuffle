"""Choice-card API: remembered picks per account, Settings options and live quotes."""
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import generation_choices as api
from tests.core.test_generation_resolver import FakeBackend, candidate

IDENTITY = ("account", "origin#owner")


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
