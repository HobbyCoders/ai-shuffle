import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import internal
from app.serve import IngressApplication


@pytest.mark.parametrize("port,token,configured,expected", [
    (8000, "secret", "secret", 200),
    (8001, "secret", "secret", 403),
    (8000, "wrong", "secret", 403),
    (8000, "secret", "", 403),
])
async def test_desktop_commands_require_child_secret_and_local_ingress(monkeypatch, port, token, configured, expected):
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", configured)
    app = FastAPI()
    app.include_router(internal.router)
    wrapped = IngressApplication(app, 8000)
    with patch("app.core.desktop_lifecycle.active_session_ids", return_value={"a", "b"}),          patch("app.core.desktop_lifecycle.stop_running_sessions", new=AsyncMock()) as stop:
        monkeypatch.setattr(internal, "_shutdown_task", None)
        async with AsyncClient(transport=ASGITransport(app=wrapped), base_url=f"http://localhost:{port}") as client:
            response = await client.get("/api/v1/internal/desktop/active-sessions", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == expected
            if expected == 200:
                assert response.json() == {"active_sessions": 2}
            response = await client.post("/api/v1/internal/desktop/stop-sessions", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == expected
            assert stop.await_count == (1 if expected == 200 else 0)


async def test_duplicate_stop_requests_join_cleanup_and_disconnect_does_not_cancel(monkeypatch):
    from starlette.requests import Request
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    monkeypatch.setattr(internal, "_shutdown_task", None)
    request = Request({"type": "http", "shuffle.ingress": "local", "headers": [(b"authorization", b"Bearer secret")]})
    started, finish = asyncio.Event(), asyncio.Event()
    async def cleanup():
        started.set()
        await finish.wait()
    with patch("app.core.desktop_lifecycle.stop_running_sessions", side_effect=cleanup) as stop:
        first = asyncio.create_task(internal.desktop_stop_sessions(request))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(internal.desktop_stop_sessions(request))
        finish.set()
        assert await second == {"stopped": True}
        stop.assert_called_once()

@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_or_cancelled_cleanup_can_be_retried(monkeypatch, cancelled):
    from starlette.requests import Request
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    request = Request({"type": "http", "shuffle.ingress": "local", "headers": [(b"authorization", b"Bearer secret")]})
    async def fail():
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("cleanup failed")
    previous = asyncio.create_task(fail())
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await previous
    monkeypatch.setattr(internal, "_shutdown_task", previous)
    with patch("app.core.desktop_lifecycle.stop_running_sessions", new=AsyncMock()) as stop:
        assert await internal.desktop_stop_sessions(request) == {"stopped": True}
        stop.assert_awaited_once()

async def test_lifecycle_routes_work_through_full_app_middleware(monkeypatch):
    from app.main import app
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    monkeypatch.setattr(internal, "_shutdown_task", None)
    with patch("app.core.desktop_lifecycle.active_session_ids", return_value={"owned"}), \
         patch("app.core.desktop_lifecycle.stop_running_sessions", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)),
                               base_url="http://localhost:8000") as client:
            headers = {"Authorization": "Bearer secret"}
            active = await client.get("/api/v1/internal/desktop/active-sessions", headers=headers)
            assert active.status_code == 200
            assert active.json() == {"active_sessions": 1}
            stopped = await client.post("/api/v1/internal/desktop/stop-sessions", headers=headers)
            assert stopped.status_code == 200
            assert stopped.json() == {"stopped": True}


