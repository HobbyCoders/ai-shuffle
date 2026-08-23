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


def test_shadowed_builtins_are_greyed_not_dropped(client):
    """Native legs run fluid's own Task/Monitor/ToolSearch, so the bridged
    ``mcp__builtins__`` copies can never be attached — the picker must show
    them as unavailable with a reason rather than silently omit them."""
    from app.core.provider.fluid_tool_bridge import NATIVE_SHADOWED_BUILTINS

    rows = {t["name"]: t for t in _group(_surface(client), "app")["tools"]}
    for bare in NATIVE_SHADOWED_BUILTINS:
        row = rows[f"mcp__builtins__{bare}"]
        assert row["available"] is False, bare
        assert row["unavailable_reason"], bare
        assert bare in row["unavailable_reason"], bare

    # A control row from the same group is untouched.
    assert rows["mcp__builtins__Memory"].get("available") is not False


def test_gateway_reasons_name_only_the_types_pinned_elsewhere(monkeypatch):
    """A connector type whose every active row runs on another install cannot
    be reached from here; one that is ours or unclaimed still can."""
    import app.db.database as database
    from app.core.config import Settings, settings
    from app.mcp import connector_gateway_sdk

    monkeypatch.setattr(
        connector_gateway_sdk,
        "get_gateway_tool_names",
        lambda: [
            "mcp__builtins__discord_actions",
            "mcp__builtins__discord_invoke",
            "mcp__builtins__homeassistant_actions",
            "mcp__builtins__homeassistant_invoke",
            "mcp__builtins__gmail_actions",
            "mcp__builtins__gmail_invoke",
        ],
    )
    monkeypatch.setattr(
        database,
        "get_active_connectors",
        lambda: [
            {"id": "1", "type": "discord", "runner_id": "other-install"},
            {"id": "2", "type": "homeassistant", "runner_id": None},
            {"id": "3", "type": "gmail", "runner_id": "this-install"},
        ],
    )
    monkeypatch.setattr(
        Settings, "effective_instance_id", property(lambda self: "this-install")
    )
    assert settings.effective_instance_id == "this-install"

    reasons = tool_catalog._gateway_unavailable_reasons()
    assert set(reasons) == {
        "mcp__builtins__discord_actions",
        "mcp__builtins__discord_invoke",
    }
    assert all(r == "The discord connector runs on another install." for r in reasons.values())


def test_gateway_reasons_are_empty_when_the_lookup_fails(monkeypatch):
    """Availability marking is best-effort: a broken DB must not break
    discovery."""
    import app.db.database as database

    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(database, "get_active_connectors", _boom)
    assert tool_catalog._gateway_unavailable_reasons() == {}


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
