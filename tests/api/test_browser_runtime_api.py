"""Cold-start and internal proxy RPC must authenticate before allocating actors."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import browser_panel as panel
from app.core import browser_runtime as runtime, browser_panel_registry as registry


@pytest.mark.asyncio
async def test_start_checks_auth_and_session_before_spawn(monkeypatch):
    app = FastAPI()
    app.include_router(panel.router)
    start = AsyncMock(return_value={"available": True})
    monkeypatch.setattr(runtime, "start", start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/sessions/private/browser/panel/start")
        assert response.status_code in (401, 403)
        start.assert_not_called()
        app.dependency_overrides[panel.require_auth] = lambda: "auth"
        async def denied(*args):
            raise HTTPException(403)
        monkeypatch.setattr(panel, "require_session", denied)
        assert (await client.post("/api/v1/sessions/private/browser/panel/start")).status_code == 403
        start.assert_not_called()
        monkeypatch.setattr(panel, "require_session", AsyncMock(return_value={"id": "private"}))
        assert (await client.post("/api/v1/sessions/private/browser/panel/start")).json() == {"available": True}
        start.assert_awaited_once_with("private")


@pytest.mark.asyncio
async def test_rpc_token_session_and_payload_validation_before_spawn(monkeypatch):
    app = FastAPI()
    app.include_router(panel.router)
    spawn = AsyncMock()
    monkeypatch.setattr(runtime, "start", spawn)
    monkeypatch.setattr(runtime, "get", spawn)
    monkeypatch.setattr(panel.database, "get_session", lambda sid: {"id": sid} if sid == "rpc-session" else None)
    token = registry.issue_token("rpc-session")
    body = {"session_id": "rpc-session", "token": token, "tool": "browser_status", "arguments": {}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for updates, code in [({"token": "wrong"}, 403), ({"session_id": "other"}, 403), ({"tool": "shell"}, 422), ({"arguments": []}, 422)]:
            response = await client.post("/api/v1/browser/panel/rpc", json={**body, **updates})
            assert response.status_code == code
        assert (await client.post("/api/v1/browser/panel/rpc", content="x" * 262145)).status_code == 413
        monkeypatch.setattr(panel.database, "get_session", lambda sid: None)
        assert (await client.post("/api/v1/browser/panel/rpc", json=body)).status_code == 404
    spawn.assert_not_called()
    registry.clear("rpc-session")
