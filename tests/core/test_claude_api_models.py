import httpx
import pytest

from app.api.model_registry import (
    ClaudeApiModelEntry,
    SaveClaudeApiModelsRequest,
    list_claude_api_models,
    save_claude_api_models,
)
from app.core import claude_accounts, claude_api_models, claude_api_provider
from fastapi import HTTPException, Response


@pytest.mark.asyncio
async def test_catalog_uses_credentials_and_returns_all_pages():
    cursors = []

    def respond(request):
        assert request.method == "GET"
        assert str(request.url.copy_with(query=None)) == claude_api_models.ANTHROPIC_MODELS_URL
        assert request.headers["x-api-key"] == "test-credential"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        assert request.url.params["limit"] == "100"
        cursor = request.url.params.get("after_id")
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(200, json={"data": [
                {"id": "claude-sonnet-test", "display_name": "Sonnet Test"},
            ], "has_more": True, "last_id": "claude-sonnet-test"})
        return httpx.Response(200, json={"data": [
            {"id": "claude-haiku-test"}, {"id": "claude-sonnet-test"},
        ], "has_more": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        assert await claude_api_models.list_models("test-credential", client=client) == [
            {"id": "claude-sonnet-test", "label": "Sonnet Test"},
            {"id": "claude-haiku-test", "label": "claude-haiku-test"},
        ]
    assert cursors == [None, "claude-sonnet-test"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 500])
async def test_catalog_does_not_redirect_or_echo_upstream_error(status):
    seen = []

    def respond(request):
        seen.append(request.url.host)
        return httpx.Response(status, text="private upstream error", headers={"Location": "https://invalid.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(claude_api_models.CatalogError) as exc:
            await claude_api_models.list_models("test-credential", client=client)
    assert "private upstream error" not in str(exc.value)
    assert seen == ["api.anthropic.com"]


@pytest.mark.asyncio
async def test_catalog_rejects_repeated_cursor_without_partial_results():
    def respond(_):
        return httpx.Response(200, json={"data": [{"id": "claude-test"}], "has_more": True, "last_id": "claude-test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(claude_api_models.CatalogError, match="Could not read"):
            await claude_api_models.list_models("test-credential", client=client)


@pytest.mark.asyncio
async def test_route_uses_only_the_named_account(mock_db, monkeypatch):
    keys = {"selected": "selected-test-key", "inactive": "inactive-test-key"}
    monkeypatch.setattr(claude_accounts, "_keys", lambda: {
        "active_id": "selected", "accounts": [{"id": aid, "key": key} for aid, key in keys.items()],
    })
    looked_up = []

    async def list_models(key):
        looked_up.append(key)
        return []

    monkeypatch.setattr(claude_api_models, "list_models", list_models)
    response = Response()
    first = await list_claude_api_models("inactive", response, "admin")
    assert first["models"] == []
    assert first["curated"] is None
    assert isinstance(first["fetched_at"], str) and first["fetched_at"]
    assert response.headers["Cache-Control"] == "no-store"
    # Second call serves the cached empty catalog without re-contacting Anthropic.
    second = await list_claude_api_models("inactive", Response(), "admin")
    assert second == first
    with pytest.raises(HTTPException) as exc:
        await list_claude_api_models("deleted", Response(), "admin")
    assert exc.value.status_code == 404
    assert looked_up == ["inactive-test-key"]


@pytest.mark.asyncio
async def test_route_serves_cached_catalog_unless_refresh_and_saves_curation(mock_db, monkeypatch):
    keys = {"selected": "selected-test-key"}
    monkeypatch.setattr(claude_accounts, "_keys", lambda: {
        "active_id": "selected", "accounts": [{"id": "selected", "key": "selected-test-key"}],
    })
    fetches = []

    async def list_models(key):
        fetches.append(key)
        return [
            {"id": "claude-sonnet-5", "label": "Sonnet 5"},
            {"id": "claude-haiku-5-5", "label": "Haiku 5.5"},
        ]

    monkeypatch.setattr(claude_api_models, "list_models", list_models)
    first = await list_claude_api_models("selected", Response(), "admin", refresh=False)
    assert len(fetches) == 1
    assert first["curated"] is None
    assert first["fetched_at"] is not None

    # Second call with refresh=False uses cache without upstream fetch.
    second = await list_claude_api_models("selected", Response(), "admin", refresh=False)
    assert len(fetches) == 1
    assert second == first

    # Explicit refresh=True contacts upstream again and advances fetched_at even when unchanged,
    # while a follow-up refresh=False serves the updated timestamp with zero DB writes (no loop).
    from app.db import database

    writes = []
    real_set = database.set_system_setting
    monkeypatch.setattr(database, "set_system_setting", lambda k, v: (writes.append(k), real_set(k, v))[1])
    third = await list_claude_api_models("selected", Response(), "admin", refresh=True)
    assert len(fetches) == 2
    assert third["models"] == first["models"]
    assert third["fetched_at"] is not None and third["fetched_at"] != first["fetched_at"]
    assert len(writes) == 1 and writes[0].startswith(claude_api_provider.MODELS_FETCHED_AT_PREFIX)

    after_refresh = await list_claude_api_models("selected", Response(), "admin", refresh=False)
    assert len(fetches) == 2
    assert len(writes) == 1
    assert after_refresh == third

    # Save curated list and confirm GET returns it.
    saved = await save_claude_api_models(
        "selected",
        SaveClaudeApiModelsRequest(models=[ClaudeApiModelEntry(id="claude-haiku-5-5", label="Haiku 5.5")]),
        "admin",
    )
    assert saved == {"models": [{"id": "claude-haiku-5-5", "label": "Haiku 5.5"}]}
    fourth = await list_claude_api_models("selected", Response(), "admin", refresh=False)
    assert len(fetches) == 2
    assert fourth["curated"] == [{"id": "claude-haiku-5-5", "label": "Haiku 5.5"}]
    assert claude_api_provider.curated_models("selected-test-key") == [
        {"id": "claude-haiku-5-5", "label": "Haiku 5.5"}
    ]


def test_real_credentials_cannot_be_sent_to_fake(monkeypatch):
    monkeypatch.setenv("SHUFFLE_DEV_FAKE_AI_URL", "http://127.0.0.1:1234")
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: True)
    assert claude_api_models._catalog_url("test-credential") == claude_api_models.ANTHROPIC_MODELS_URL
    assert claude_api_models._catalog_url(claude_api_models.DEV_KEY) == "http://127.0.0.1:1234/anthropic/v1/models"
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: False)
    with pytest.raises(claude_api_models.CatalogError):
        claude_api_models._catalog_url(claude_api_models.DEV_KEY)
