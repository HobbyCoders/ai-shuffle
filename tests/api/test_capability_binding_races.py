"""Catalog responses are observations of one DB revision, never another account."""
import asyncio
from datetime import datetime, timezone

import pytest

from app.api import model_providers as api
from app.db import database as db
from app.db import model_providers as providers_db


@pytest.fixture(autouse=True)
def clean_refresh_state():
    api._capability_locks.clear()
    api._capability_results.clear()
    yield
    api._capability_locks.clear()
    api._capability_results.clear()


def make_provider():
    return db.create_model_provider("catalog-race", "Catalog", "custom", "https://old.invalid",
                                    models=[{"id": "shared-model"}])


def models(level):
    return [{"id": "shared-model", "reasoning_capabilities": {
        "version": 1, "effort_levels": [level], "default_effort": level,
    }}]


async def test_response_cannot_cross_binding_edit_and_new_binding_bypasses_cooldown(mock_db, monkeypatch):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(providers_db, 'datetime', FrozenClock)
    before = make_provider()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return api.FetchModelsResponse(source="upstream", models=models("high"))
        return api.FetchModelsResponse(source="upstream", models=models("low"))

    monkeypatch.setattr(api, "fetch_models", fetch)
    first = asyncio.create_task(api._refresh_provider_capabilities("catalog-race"))
    await entered.wait()
    db.update_model_provider("catalog-race", base_url="https://new.invalid", auth_token="new-dummy-key",
                             models_json=models("low"))
    current = db.get_model_provider("catalog-race")
    assert current["updated_at"] == before["updated_at"]
    assert current["revision"] != before["revision"]
    release.set()
    assert await first is False
    assert db.get_model_provider("catalog-race")["models_json"][0]["reasoning_capabilities"]["effort_levels"] == ["low"]
    assert await api._refresh_provider_capabilities("catalog-race") is True
    assert len(calls) == 2


async def test_conditional_write_rejects_edit_after_final_read(mock_db, monkeypatch):
    make_provider()

    async def fetch(*args, **kwargs):
        return api.FetchModelsResponse(source="upstream", models=models("high"))

    original_update = db.update_model_provider

    def race(provider_id, **kwargs):
        original_update(provider_id, models_json=models("low"))
        return original_update(provider_id, **kwargs)

    monkeypatch.setattr(api, "fetch_models", fetch)
    monkeypatch.setattr(db, "update_model_provider", race)
    assert await api._refresh_provider_capabilities("catalog-race") is False
    assert db.get_model_provider("catalog-race")["models_json"][0]["reasoning_capabilities"]["effort_levels"] == ["low"]


async def test_request_deadline_includes_global_queue_wait(mock_db, monkeypatch):
    make_provider()
    monkeypatch.setattr(api, "_capability_semaphore", asyncio.Semaphore(0))
    monkeypatch.setattr(api, "_CAPABILITY_REQUEST_TIMEOUT", 0.01)
    result = await api.refresh_capabilities(api.RefreshCapabilitiesRequest(provider_ids=["catalog-race"]), token="fake")
    assert result == {"refreshed": [], "failed": ["catalog-race"]}
