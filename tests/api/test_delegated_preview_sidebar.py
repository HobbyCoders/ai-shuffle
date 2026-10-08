"""Exercise #1789 from the harness-issued URL to the sidebar's real API reads.

No model/CLI is called: engine construction and provider creation are replaced.
Signing, workspace-card persistence and sidebar reads use production code.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agents, auth, sessions
from app.core import environment, owner_auth, preview_access
from app.core.config import settings
from app.core.provider import harness_adapter as ha
from app.db import database
from app.serve import IngressApplication
from fluid.tools.registry import ToolRegistry


class CapturingLoop:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.agents = []

    async def _ensure_session(self):
        return SimpleNamespace(session_id="ses-preview-fixture")

    async def aclose(self):
        pass


@pytest.fixture
def preview_app(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    monkeypatch.setattr(settings, "fluid_code_mode", False)
    monkeypatch.setattr(settings, "fluid_continual", False)
    monkeypatch.setattr(settings, "fluid_memory_enabled", False)
    monkeypatch.setattr(preview_access, "_listener_port", 8002)
    monkeypatch.setattr(preview_access, "_listener_host", "127.0.0.1")
    monkeypatch.setenv("BROWSER_LOOPBACK_HOST", "10.0.0.135")
    monkeypatch.setattr(environment, "_PYTEST_PROBE", {"value": None})
    monkeypatch.setattr(ha, "DelegatedLoop", CapturingLoop)
    monkeypatch.setattr(ha, "NativeLoop", CapturingLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["unused-test-cli"])

    async def provider(*args):
        return SimpleNamespace(), None

    monkeypatch.setattr(ha, "_build_provider_client", provider)
    # This is an ai-shuffle workspace, the same detection used on a worker.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "preview.py").touch()
    (tmp_path / "fluid").mkdir()
    api = FastAPI()
    for router in (auth.router, agents.router, sessions.router):
        api.include_router(router)
    return IngressApplication(api, 8000, 8001, 8002), tmp_path


async def connect(cwd, session_id, delegated, shape="preset"):
    adapter = ha.HarnessAdapter()
    extra = {"app_session_id": session_id, "fluid_registry": ToolRegistry()}
    if not delegated:
        extra["fluid_provider"] = "openai_responses"
    prompt = {"type": "preset", "append": "Test profile"} if shape == "preset" else "Test profile"
    await adapter.connect(ha.SessionOptions(
        model="fable" if delegated else "gpt-4o", cwd=str(cwd),
        system_prompt=prompt, extra=extra,
    ))
    prompt = "\n".join(adapter._engine.kwargs.get(k) or ""
                       for k in ("system", "append_system_prompt"))
    return adapter, prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [True, False], ids=["claude-cli", "native"])
@pytest.mark.parametrize("stale_cookie", [None, "shr_obsolete-install-session"])
@pytest.mark.parametrize("subject", ["preview-owner", "preview-worker"])
@pytest.mark.parametrize("shape", ["preset", "plain", "full_replace"])
async def test_issued_link_authenticates_sidebar_reads(
    preview_app, monkeypatch, delegated, stale_cookie, subject, shape,
):
    app, cwd = preview_app
    database.create_session("preview-owner", "default", is_active=True)
    database.create_session("preview-worker", "default", parent_session_id="preview-owner",
                            hidden=True, is_worker=True)
    monkeypatch.setattr(settings, "prompt_full_replace", shape == "full_replace")
    adapter, prompt = await connect(cwd, subject, delegated, shape)
    try:
        # Extract the URL the engine actually received, not a token seeded in a client.
        links = re.findall(r"http://127\.0\.0\.1:8002/api/v1/auth/preview-access\?[^\s]+", prompt)
        assert len(links) == 1
        assert database.get_session_workspace_facts(subject)["card"] == adapter._workspace_card
        parsed = urlsplit(links[0])
        client = TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 12345))
        if stale_cookie:
            client.cookies.set("session", stale_cookie)
        landing = client.get(parsed.path + "?" + parsed.query, follow_redirects=False)
        assert landing.status_code == 303
        status = client.get("/api/v1/auth/status")
        assert status.status_code == 200
        assert status.json()["authenticated"] and status.json()["preview_access"]
        # These are the exact endpoints/filters used by appData for the sidebar.
        roster = client.get("/api/v1/agents")
        assert roster.status_code == 200
        assert "default" in {row["id"] for row in roster.json()}
        base = "/api/v1/sessions/cursor?limit=100&admin_only=true&exclude_archived=true"
        for path in (base, base + "&active_only=true"):
            listed = client.get(path)
            assert listed.status_code == 200
            rows = listed.json()["items"]
            assert {row["id"] for row in rows} == {"preview-owner"}
            assert sum(bool(row["is_active"]) for row in rows) == 1
            assert not rows[0]["hidden"] and not rows[0]["is_remote"]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [True, False], ids=["claude-cli", "native"])
@pytest.mark.parametrize("subject", ["preview-key", "preview-key-worker"])
async def test_key_session_and_its_worker_receive_no_preview(preview_app, delegated, subject):
    app, cwd = preview_app
    database.create_api_user("preview-key-user", "Restricted key", "unused-fixture-hash")
    database.create_session("preview-key", "default", api_user_id="preview-key-user", is_active=True)
    # Legacy worker rows inherited the key through their parent, not their own column.
    database.create_session("preview-key-worker", "default", parent_session_id="preview-key",
                            hidden=True, is_worker=True)
    adapter, prompt = await connect(cwd, subject, delegated)
    try:
        assert preview_access.REDIRECT_PATH not in prompt
        assert "this session has no preview link" in prompt
        assert database.get_session_workspace_facts(subject)["preview_expires_at"] is None
        client = TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 12345))
        # Defense in depth: even a correctly signed token for a key is refused.
        parsed = urlsplit(preview_access.preview_url(subject, "127.0.0.1", 8002))
        assert client.get(parsed.path + "?" + parsed.query, follow_redirects=False).status_code == 403
        assert client.get("/api/v1/agents").status_code == 403
        assert client.get("/api/v1/sessions/cursor").status_code == 403
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [True, False], ids=["claude-cli", "native"])
async def test_reconnect_recovers_preview_when_listener_becomes_available(
    preview_app, monkeypatch, delegated,
):
    app, cwd = preview_app
    database.create_session("preview-owner", "default", is_active=True)
    monkeypatch.setattr(preview_access, "_listener_port", None)
    first, prompt = await connect(cwd, "preview-owner", delegated)
    assert preview_access.REDIRECT_PATH not in prompt
    assert database.get_session_workspace_facts("preview-owner")["preview_expires_at"] is None
    await first.disconnect()

    # A later connect must not keep the no-link card forever after the listener starts.
    monkeypatch.setattr(preview_access, "_listener_port", 8002)
    second, prompt = await connect(cwd, "preview-owner", delegated)
    try:
        assert preview_access.REDIRECT_PATH in prompt
        assert database.get_session_workspace_facts("preview-owner")["preview_expires_at"] is not None
        link = re.search(
            r"http://127\.0\.0\.1:8002/api/v1/auth/preview-access\?[^\s]+", prompt,
        )
        assert link is not None
        parsed = urlsplit(link.group())
        client = TestClient(app, base_url="http://127.0.0.1:8002", client=("127.0.0.1", 12345))
        assert client.get(parsed.path + "?" + parsed.query, follow_redirects=False).status_code == 303
        listed = client.get("/api/v1/sessions/cursor?admin_only=true&active_only=true")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["items"]] == ["preview-owner"]
        recovered = second._workspace_card
    finally:
        await second.disconnect()
    third, _ = await connect(cwd, "preview-owner", delegated)
    try:
        assert third._workspace_card == recovered
    finally:
        await third.disconnect()
