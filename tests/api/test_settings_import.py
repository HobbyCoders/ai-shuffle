from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings_import
from app.api.auth import require_admin


def _app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(settings_import.router)
    if authenticated:
        app.dependency_overrides[require_admin] = lambda: "admin"
    return app


def test_preview_requires_admin() -> None:
    with TestClient(_app(authenticated=False)) as client:
        response = client.post("/api/settings/import/preview", json={})
    assert response.status_code in {401, 403}


def test_preview_passes_provider_order_and_source(monkeypatch) -> None:
    captured = {}

    def preview_import(**kwargs):
        captured.update(kwargs)
        return {"token": "fresh", "categories": []}

    monkeypatch.setattr(settings_import, "preview_import", preview_import)
    with TestClient(_app(authenticated=True)) as client:
        response = client.post(
            "/api/settings/import/preview",
            json={"source_home": "/mounted legacy home", "providers": ["codex", "claude"]},
        )

    assert response.status_code == 200
    assert response.json()["token"] == "fresh"
    assert captured == {
        "source_home": "/mounted legacy home",
        "providers": ["codex", "claude"],
    }


def test_apply_maps_stale_plan_to_409(monkeypatch) -> None:
    def apply_import(**_kwargs):
        raise settings_import.StaleImportPlan("preview again")

    monkeypatch.setattr(settings_import, "apply_import", apply_import)
    with TestClient(_app(authenticated=True)) as client:
        response = client.post(
            "/api/settings/import/apply",
            json={"token": "stale", "selected_item_ids": []},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "preview again"


def test_legacy_claude_route_scans_only_claude(monkeypatch) -> None:
    captured = {}

    def preview_import(**kwargs):
        captured.update(kwargs)
        return {"token": "alias", "categories": []}

    monkeypatch.setattr(settings_import, "preview_import", preview_import)
    with TestClient(_app(authenticated=True)) as client:
        response = client.post("/api/settings/import/claude/preview", json={})
    assert response.status_code == 200
    assert response.json()["token"] == "alias"
    assert captured["providers"] == ["claude"]
