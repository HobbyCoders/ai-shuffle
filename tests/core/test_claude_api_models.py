import httpx
import pytest

from app.api.model_registry import list_claude_api_models
from app.core import claude_accounts, claude_api_models
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
async def test_route_uses_only_the_named_account(monkeypatch):
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
    assert await list_claude_api_models("inactive", response, "admin") == {"models": []}
    assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPException) as exc:
        await list_claude_api_models("deleted", Response(), "admin")
    assert exc.value.status_code == 404
    assert looked_up == ["inactive-test-key"]


def test_real_credentials_cannot_be_sent_to_fake(monkeypatch):
    monkeypatch.setenv("SHUFFLE_DEV_FAKE_AI_URL", "http://127.0.0.1:1234")
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: True)
    assert claude_api_models._catalog_url("test-credential") == claude_api_models.ANTHROPIC_MODELS_URL
    assert claude_api_models._catalog_url(claude_api_models.DEV_KEY) == "http://127.0.0.1:1234/anthropic/v1/models"
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: False)
    with pytest.raises(claude_api_models.CatalogError):
        claude_api_models._catalog_url(claude_api_models.DEV_KEY)
