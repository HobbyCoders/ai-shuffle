"""GET /api/v1/tools/surface — the one live inventory (rulings T1, T8).

The harness assertion iterates fluid's own registry, so adding a builtin
upstream is caught here without anyone editing this file.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.tools import router
from app.core import tool_catalog
from app.core.mcp_service import McpServerConfig, McpServerType


@pytest.fixture(autouse=True)
def _clean_cache():
    tool_catalog.clear_mcp_cache()
    yield
    tool_catalog.clear_mcp_cache()


def _stdio(name):
    """A spawnable stdio server config — enough for the fluid MCP client config
    translation to accept it, so the ``tools/list`` call is actually attempted."""
    return McpServerConfig(
        name=name, type=McpServerType.STDIO, command="node", args=["server.js"]
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    with TestClient(app) as test_client:
        yield test_client


def _surface(client, **params):
    response = client.get("/api/v1/tools/surface", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _group(surface, gid):
    for group in surface["groups"]:
        if group["id"] == gid:
            return group
    raise AssertionError(f"no {gid!r} group in {[g['id'] for g in surface['groups']]}")


def test_response_shape(client):
    surface = _surface(client)
    assert set(surface) >= {"groups", "discovered_at", "errors"}
    assert isinstance(surface["errors"], list)
    for group in surface["groups"]:
        assert set(group) >= {
            "id", "label", "kind", "server", "available", "admin_only", "tools",
        }
        assert group["kind"] in (
            "harness", "app", "ai", "computer_use", "browser", "app_config", "mcp",
        )
        for entry in group["tools"]:
            assert set(entry) >= {"name", "description", "risk"}
            assert entry["risk"] in (None, "read", "write", "exec", "network")


def test_every_fluid_builtin_is_present(client):
    """Iterates ``register_builtins`` rather than naming tools, so a builtin
    added upstream is caught with no edit here."""
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)
    expected = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert expected  # the registry is not empty, or this test proves nothing

    harness = _group(_surface(client), "harness")
    assert {t["name"] for t in harness["tools"]} == expected


def test_app_and_app_config_groups(client):
    surface = _surface(client)

    app_group = _group(surface, "app")
    names = {t["name"] for t in app_group["tools"]}
    assert "mcp__builtins__Task" in names
    assert all(n.startswith("mcp__builtins__") for n in names)

    cfg = _group(surface, "app_config")
    assert cfg["admin_only"] is True
    assert "mcp__app-config__listHooks" in {t["name"] for t in cfg["tools"]}


def test_ai_group_carries_availability(client):
    ai = _group(_surface(client), "ai")
    assert ai["kind"] == "ai"
    for entry in ai["tools"]:
        assert entry["name"].startswith("mcp__ai-tools__")
        assert "available" in entry


def test_computer_use_group_names_every_tool(client):
    from app.mcp.computer_use_sdk import _TOOL_NAMES

    group = _group(_surface(client), "computer_use")
    assert {t["name"] for t in group["tools"]} == {
        f"mcp__computer-use__{n}" for n in _TOOL_NAMES
    }


def test_a_failing_mcp_server_becomes_an_error_group_not_a_silent_drop(client):
    with patch.object(
        tool_catalog,
        "_configured_mcp_servers",
        new=lambda errors: (None, {"broken": _stdio("broken")}),
    ), patch.object(
        tool_catalog,
        "_list_mcp_tools",
        new=AsyncMock(side_effect=RuntimeError("spawn failed")),
    ):
        surface = _surface(client)

    group = _group(surface, "mcp:broken")
    assert group["tools"] == []
    assert any(e["group"] == "mcp:broken" for e in surface["errors"])


def test_mcp_enumeration_is_cached_until_refresh(client):
    lister = AsyncMock(
        return_value=[{"name": "mcp__srv__go", "description": "Go.", "risk": "write"}]
    )
    with patch.object(
        tool_catalog,
        "_configured_mcp_servers",
        new=lambda errors: (None, {"srv": _stdio("srv")}),
    ), patch.object(tool_catalog, "_list_mcp_tools", new=lister):
        first = _surface(client)
        assert lister.await_count == 1
        _surface(client)
        assert lister.await_count == 1, "second call must come from the cache"
        _surface(client, refresh="true")
        assert lister.await_count == 2, "?refresh=true must bust the cache"

    assert _group(first, "mcp:srv")["tools"][0]["name"] == "mcp__srv__go"


def test_first_sentence_trims_multi_paragraph_descriptions():
    assert tool_catalog.first_sentence("Do a thing. Then another.") == "Do a thing."
    assert tool_catalog.first_sentence("  spread\n over lines  ") == "spread over lines"
    assert tool_catalog.first_sentence("") is None
    assert tool_catalog.first_sentence(None) is None
