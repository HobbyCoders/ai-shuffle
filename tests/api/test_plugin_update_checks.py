"""Explicit refresh is owner-only; passive checks stay off the event loop."""

import threading
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import plugins
from app.api.auth import require_admin, require_auth


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, True])
async def test_update_checks_run_off_loop(monkeypatch, refresh):
    loop_thread = threading.get_ident()
    calls = []
    service = MagicMock()

    def check(*args):
        calls.append(args)
        assert threading.get_ident() != loop_thread
        return []

    service.check_for_updates.side_effect = check
    monkeypatch.setattr(plugins, "get_plugin_service", lambda: service)
    endpoint = plugins.refresh_plugin_updates if refresh else plugins.check_for_updates
    assert await endpoint(token="test") == []
    assert calls == [(True,)] if refresh else calls == [()]


def test_refresh_does_not_accept_read_only_auth(monkeypatch):
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_auth] = lambda: "read-only"

    def refuse_admin():
        raise HTTPException(403, "Owner required")

    app.dependency_overrides[require_admin] = refuse_admin
    service = MagicMock()
    monkeypatch.setattr(plugins, "get_plugin_service", lambda: service)
    response = TestClient(app).post("/api/v1/plugins/updates/check", json={})
    assert response.status_code == 403
    service.check_for_updates.assert_not_called()
