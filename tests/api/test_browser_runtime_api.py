"""Cold-start and internal proxy RPC must authenticate before allocating actors."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import browser_panel as panel
from app.core import browser_runtime as runtime, browser_panel_registry as registry
from app.core import desktop_listener
from app.middleware.desktop_listener import DesktopListenerGate


@pytest.fixture
def desktop_rpc_app(monkeypatch):
    """Use the token-gated local listener, without any desktop credential."""
    monkeypatch.setattr(desktop_listener, "_configured", True)
    monkeypatch.setattr(desktop_listener, "_token", "a" * 64)
    app = FastAPI()
    app.include_router(panel.router)
    app.add_middleware(DesktopListenerGate)

    async def local_listener(scope, receive, send):
        scope["shuffle.ingress"] = "local"
        await app(scope, receive, send)

    return local_listener


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
async def test_rpc_token_session_and_payload_validation_before_spawn(monkeypatch, desktop_rpc_app):
    app = desktop_rpc_app
    spawn = AsyncMock()
    monkeypatch.setattr(runtime, "start", spawn)
    monkeypatch.setattr(runtime, "get", spawn)
    monkeypatch.setattr(panel.database, "get_session", lambda sid: {"id": sid} if sid == "rpc-session" else None)
    token = registry.issue_token("rpc-session")
    body = {"session_id": "rpc-session", "token": token, "tool": "browser_status", "arguments": {}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for updates, code in [({"token": "wrong"}, 403), ({"token": None}, 422), ({"session_id": "other"}, 403), ({"tool": "shell"}, 422), ({"arguments": []}, 422)]:
            response = await client.post("/api/v1/browser/panel/rpc", json={**body, **updates})
            assert response.status_code == code
        assert (await client.post("/api/v1/browser/panel/rpc", content="x" * 262145)).status_code == 413
        monkeypatch.setattr(panel.database, "get_session", lambda sid: None)
        assert (await client.post("/api/v1/browser/panel/rpc", json=body)).status_code == 404
    spawn.assert_not_called()
    registry.clear("rpc-session")


@pytest.mark.asyncio
async def test_start_failure_detail_reaches_the_panel(monkeypatch):
    """503s carry the reason. A bare "could not start" is unactionable."""
    app = FastAPI()
    app.include_router(panel.router)
    app.dependency_overrides[panel.require_auth] = lambda: "auth"
    monkeypatch.setattr(panel, "require_session", AsyncMock(return_value={"id": "s"}))
    monkeypatch.setattr(
        runtime,
        "start",
        AsyncMock(side_effect=runtime.RuntimeUnavailable(runtime.STALE_BUILD_DETAIL)),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/sessions/s/browser/panel/start")
    assert response.status_code == 503
    assert response.json()["detail"] == runtime.STALE_BUILD_DETAIL


@pytest.mark.asyncio
async def test_rpc_session_token_passes_desktop_gate(monkeypatch, desktop_rpc_app):
    monkeypatch.setattr(panel.database, "get_session", lambda sid: {"id": sid})
    token = registry.issue_token("rpc-desktop")
    result = {"content": [{"type": "text", "text": '{"ok":true}'}]}
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(runtime, "call", call)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=desktop_rpc_app), base_url="http://test") as client:
            response = await client.post("/api/v1/browser/panel/rpc", json={
                "session_id": "rpc-desktop", "token": token,
                "tool": "browser_status", "arguments": {},
            })
        assert response.status_code == 200
        assert response.json() == result
        call.assert_awaited_once_with("rpc-desktop", "browser_status", {})
    finally:
        registry.clear("rpc-desktop")


@pytest.mark.asyncio
@pytest.mark.parametrize("head", ["detached", "switched"])
@pytest.mark.parametrize("native", [False, True])
async def test_checkout_head_mismatch_reaches_rpc_and_native_proxy(
    monkeypatch, desktop_rpc_app, tmp_path, head, native,
):
    """Real git HEAD changes drive the resolver -> actor -> RPC -> proxy path."""
    import subprocess
    from app.core import checkout_manager, native_browser_proxy, native_chrome, profiles
    from app.db import checkouts, projects_v2

    def git(path, *args):
        return subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, text=True,
        ).stdout.strip()

    base = tmp_path / "repo"
    base.mkdir()
    git(base, "init")
    git(base, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "Initial")
    checkout = tmp_path / "checkout"
    git(base, "worktree", "add", "-b", "session-branch", str(checkout))
    if head == "detached":
        # Behind its branch: a detached HEAD at the branch tip (or ahead of
        # it) is reattached on send, so only a lossy one is still refused.
        git(checkout, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", "Branch work")
        git(checkout, "switch", "--detach", "HEAD~1")
    else:
        git(checkout, "switch", "-c", "other-branch")
    session_id = f"rpc-{head}-{native}"
    session = {"id": session_id, "profile_id": "p", "project_v2_id": "project",
               "branch": "session-branch"}
    monkeypatch.setattr(panel.database, "get_session", lambda sid: session)
    monkeypatch.setattr(profiles, "get_profile", lambda pid: {"config": {}})
    monkeypatch.setattr(projects_v2, "get_project_v2", lambda pid: {"id": "project"})
    branch_row = {"path": str(checkout), "status": "active"}
    monkeypatch.setattr(checkouts, "get_checkout", lambda *a, **kw: branch_row)
    def local_checkouts(*args, branch=None):
        return [branch_row] if branch else [{"path": str(base), "status": "active"}]

    monkeypatch.setattr(checkouts, "list_checkouts_with_legacy_fallback", local_checkouts)
    chrome = AsyncMock()
    monkeypatch.setattr(native_chrome, "ensure_started", chrome)
    token = registry.issue_token(session_id)
    original_client = httpx.AsyncClient
    transport = httpx.ASGITransport(app=desktop_rpc_app)
    try:
        if native:
            monkeypatch.setattr(native_browser_proxy.httpx, "AsyncClient",
                                lambda **kw: original_client(transport=transport, **kw))
            result = await native_browser_proxy.forward_tool("browser_status", {}, {
                "AIS_SESSION_ID": session_id, "AIS_PANEL_TOKEN": token,
                "AIS_PANEL_REGISTER_URL": "http://test/api/v1/browser/panel/register",
            })
            assert result["isError"] is True
            text = result["content"][0]["text"]
            assert "checkout is not on its branch" in text
            assert "Switch the worktree back" in text
            assert str(checkout) not in text
        else:
            async with original_client(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/browser/panel/rpc", json={
                    "session_id": session_id, "token": token,
                    "tool": "browser_status", "arguments": {},
                })
            assert response.status_code == 503
            assert response.json() == {
                "reason": "checkout_branch_mismatch", "detail": runtime.CHECKOUT_MISMATCH_DETAIL,
            }
        # Diagnosis never switches HEAD or changes the recorded branch binding.
        if head == "switched":
            assert git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD") == "other-branch"
        else:
            assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert checkout_manager.ensure_checkout({"id": "project"}, "session-branch") is None
    finally:
        await runtime.close_session(session_id)
        registry.clear(session_id)


@pytest.mark.asyncio
async def test_rpc_local_owner_requires_origin_and_session_token(monkeypatch):
    from app.core import owner_auth
    from app.middleware.csrf import CSRFProtectionMiddleware

    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    monkeypatch.setattr(desktop_listener, "configured", lambda: False)
    app = FastAPI()
    app.include_router(panel.router)
    app.add_middleware(CSRFProtectionMiddleware)
    monkeypatch.setattr(panel.database, "get_session", lambda sid: {"id": sid})
    call = AsyncMock(return_value={"content": []})
    monkeypatch.setattr(runtime, "call", call)
    token = registry.issue_token("rpc-origin")
    body = {"session_id": "rpc-origin", "token": token, "tool": "browser_status", "arguments": {}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            for headers in ({}, {"origin": "https://evil.example"}):
                response = await client.post("/api/v1/browser/panel/rpc", json=body, headers=headers)
                assert response.status_code == 403
                assert response.json()["detail"] == "Same-origin request required"
            call.assert_not_called()
            headers = {"origin": "http://127.0.0.1:8000"}
            response = await client.post("/api/v1/browser/panel/rpc", json={**body, "token": "wrong"}, headers=headers)
            assert response.status_code == 403
            assert response.json()["detail"] == "Invalid panel token"
            call.assert_not_called()
            response = await client.post("/api/v1/browser/panel/rpc", json=body, headers=headers)
            assert response.status_code == 200
            call.assert_awaited_once_with("rpc-origin", "browser_status", {})
    finally:
        registry.clear("rpc-origin")


@pytest.mark.asyncio
async def test_rpc_exception_does_not_open_other_browser_routes(monkeypatch, desktop_rpc_app):
    call = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr(runtime, "call", call)
    monkeypatch.setattr(runtime, "start", start)
    token = registry.issue_token("rpc-desktop")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=desktop_rpc_app), base_url="http://test") as client:
            for path in (
                "/api/v1/browser/panel/rpc/extra",
                "/api/v1/sessions/rpc-desktop/browser/panel/start",
                "/api/v1/sessions/rpc-desktop/browser/panel/upload",
                "/api/v1/settings",
            ):
                response = await client.post(path, json={"session_id": "rpc-desktop", "token": token})
                assert response.status_code == 403
                assert response.json() == {"detail": "Desktop listener token required"}
        call.assert_not_called()
        start.assert_not_called()
    finally:
        registry.clear("rpc-desktop")
