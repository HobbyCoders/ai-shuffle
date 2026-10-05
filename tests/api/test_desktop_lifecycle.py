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
                assert response.json() == {"active_sessions": 2, "remote_workers": []}
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
            assert active.json() == {"active_sessions": 1, "remote_workers": []}
            stopped = await client.post("/api/v1/internal/desktop/stop-sessions", headers=headers)
            assert stopped.status_code == 200
            assert stopped.json() == {"stopped": True}


@pytest.mark.parametrize("port,token,configured,expected", [
    (8000, "secret", "secret", 200),
    (8001, "secret", "secret", 403),
    (8000, "wrong", "secret", 403),
    (8000, "secret", "", 403),
])
async def test_source_lease_is_only_for_the_launching_desktop(monkeypatch, port, token, configured, expected):
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", configured)
    app = FastAPI()
    app.include_router(internal.router)
    with patch("app.core.license.lease_header", return_value="eyJsZWFzZSI6MX0") as lease:
        async with AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)),
                               base_url=f"http://localhost:{port}") as client:
            response = await client.get("/api/v1/internal/desktop/source-lease",
                                        headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == expected
    if expected == 200:
        assert response.json() == {"lease": "eyJsZWFzZSI6MX0"}
    else:
        lease.assert_not_called()


async def test_source_lease_is_null_without_a_lease(monkeypatch):
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    app = FastAPI()
    app.include_router(internal.router)
    with patch("app.core.license.lease_header", return_value=None):
        async with AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)),
                               base_url="http://localhost:8000") as client:
            response = await client.get("/api/v1/internal/desktop/source-lease",
                                        headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200 and response.json() == {"lease": None}




async def test_update_bridge_carries_release_notes_to_the_update_status(monkeypatch):
    from app.core import application_updates as updates
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES", raising=False)
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: None)
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    monkeypatch.setattr(updates, "_desktop_request", None)
    app = FastAPI()
    app.include_router(internal.router)
    headers = {"Authorization": "Bearer secret"}
    state = {"phase": "available", "version": "b" * 12, "running_version": "a" * 40, "percent": None,
             "error": None, "message": None, "checkedAt": "2026-09-23T00:00:00Z",
             "release": "0.9.0", "notes": "Faster startup.\n\n- Fixes the sidebar."}
    async with AsyncClient(transport=ASGITransport(app=IngressApplication(app, 8000)),
                           base_url="http://localhost:8000") as client:
        response = await client.post("/api/v1/internal/desktop/update-bridge", json=state, headers=headers)
        assert response.status_code == 200
        status = updates.status()
        assert status["kind"] == "desktop"
        assert (status["release"], status["notes"]) == ("0.9.0", "Faster startup.\n\n- Fixes the sidebar.")
        # Both stay optional: an older desktop that does not send them still reports.
        response = await client.post("/api/v1/internal/desktop/update-bridge", json={"phase": "idle"}, headers=headers)
        assert response.status_code == 200
        assert updates.status()["release"] is None and updates.status()["notes"] is None
        for field, value in (("notes", "x" * 4001), ("release", "9" * 81)):
            response = await client.post("/api/v1/internal/desktop/update-bridge",
                                         json={"phase": "ready", field: value}, headers=headers)
            assert response.status_code == 422, field


@pytest.mark.parametrize("port,token,expected", [(8000, "secret", 200), (8001, "secret", 403), (8000, "wrong", 403)])
async def test_native_update_check_uses_the_worker_dispatch(monkeypatch, port, token, expected):
    # #1623: the tray's Check for Updates must follow the same all-worker plan as the web button.
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "secret")
    app = FastAPI()
    app.include_router(internal.router)
    wrapped = IngressApplication(app, 8000)
    plan = {"plan": {"action": "download"}, "workers": []}
    receipt = {"scope": "all", "sent": "download", "results": []}
    with patch("app.core.update_workers.snapshot", new=AsyncMock(return_value=plan)) as snapshot, \
         patch("app.core.update_workers.dispatch", new=AsyncMock(return_value=receipt)) as dispatch:
        async with AsyncClient(transport=ASGITransport(app=wrapped), base_url=f"http://localhost:{port}") as client:
            headers = {"Authorization": f"Bearer {token}"}
            response = await client.get("/api/v1/internal/desktop/update-workers", headers=headers)
            assert response.status_code == expected
            response = await client.post("/api/v1/internal/desktop/update-action", headers=headers, json={"action": "download"})
            assert response.status_code == expected
            if expected == 200:
                assert response.json() == receipt
                dispatch.assert_awaited_once_with("download")
            else:
                snapshot.assert_not_called()
                dispatch.assert_not_called()
            if expected == 200:
                bad = await client.post("/api/v1/internal/desktop/update-action", headers=headers, json={"action": "rm -rf"})
                assert bad.status_code == 422
                dispatch.side_effect = ValueError("Waiting on workers to finish the previous update")
                refused = await client.post("/api/v1/internal/desktop/update-action", headers=headers, json={"action": "install"})
                assert refused.status_code == 409 and "Waiting on workers" in refused.json()["detail"]
