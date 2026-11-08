"""Explicit refresh is owner-only; passive checks stay off the event loop."""

import threading
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import plugins
from app.api.auth import require_admin, require_auth
from tests.core.test_plugin_source_updates import commit, setup, sources, write  # noqa: F401


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


@pytest.mark.parametrize("path", ["updates/check", "updates/status/check"])
def test_refresh_does_not_accept_read_only_auth(monkeypatch, path):
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_auth] = lambda: "read-only"

    def refuse_admin():
        raise HTTPException(403, "Owner required")

    app.dependency_overrides[require_admin] = refuse_admin
    service = MagicMock()
    monkeypatch.setattr(plugins, "get_plugin_service", lambda: service)
    response = TestClient(app).post(f"/api/v1/plugins/{path}", json={})
    assert response.status_code == 403
    service.check_for_updates.assert_not_called()
    service.get_update_report.assert_not_called()


@pytest.mark.parametrize("kind", ["implicit", "url"])
def test_owner_route_updates_real_git_then_native_session_loads_that_revision(
    sources, monkeypatch, tmp_path, kind,
):
    import asyncio
    from fluid.context.compiler import PromptInputs, compile_prompt
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    service, upstream, skill, installed = setup(sources, kind)
    write(skill, skill.read_text().replace("First revision", "Second revision").replace(
        "Update test", "Second revision description",
    ))
    commit(upstream)
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_auth] = lambda: "owner-test"
    app.dependency_overrides[require_admin] = lambda: "owner-test"
    monkeypatch.setattr(plugins, "get_plugin_service", lambda: service)
    with TestClient(app) as client:
        assert client.get("/api/v1/plugins/updates").json() == []
        unchecked = client.get("/api/v1/plugins/updates/status").json()
        if kind == "url":
            assert unchecked["checks"][0]["status"] == "unchecked"
        checked = client.post("/api/v1/plugins/updates/status/check", json={})
        assert checked.status_code == 200
        assert [p["plugin_id"] for p in checked.json()["updates"]] == [installed.id]
        assert checked.json()["checks"][0]["status"] == "update"
        updated = client.post("/api/v1/plugins/update", json={"plugin_id": installed.id})
        assert updated.status_code == 200, updated.text
        assert updated.json()["enabled"] is True
        assert client.get("/api/v1/plugins/updates").json() == []

    # Exercise the native producing/consuming contract, not just a seeded
    # serializer or file assertion: fresh session prompt discovery and Skill
    # load both resolve the revision installed by the real owner route.
    cwd = tmp_path / "session"
    cwd.mkdir()
    prompt = compile_prompt(PromptInputs(
        cwd=str(cwd), tools=frozenset({"Skill"}), setting_sources=("user",),
        include_env=False, include_global_instructions=False,
        include_project_instructions=False,
    ))
    assert "sample:sample" in prompt.text
    assert "Second revision description" in prompt.text
    assert "First revision description" not in prompt.text
    outcome = asyncio.run(handler(
        {"name": "sample:sample"}, ToolCtx(cwd=str(cwd), extra={"setting_sources": ("user",)}),
    ))
    assert not outcome.is_error
    assert "Second revision." in outcome.text
    assert "First revision" not in outcome.text
    assert updated.json()["install_path"] in outcome.text
