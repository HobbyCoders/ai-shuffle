"""tool_catalog: row shaping, MCP config translation, the tools/list cache,
gateway availability marking and the surface's per-group error isolation.

Every group builder that would spawn or import heavy runtimes is replaced with a
stub, so these tests never start an MCP server, the browser or the fal client.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import tool_catalog as tc
from app.core.mcp_service import McpServerConfig, McpServerType

# Captured at import, before conftest's autouse guard replaces it with a stub.
_REAL_LIST_MCP_TOOLS = tc._list_mcp_tools


@pytest.fixture(autouse=True)
def _fresh_cache():
    tc.clear_mcp_cache()
    yield
    tc.clear_mcp_cache()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (None, None),
    ("", None),
    ("   \n\t ", None),
    ("Reads a file.  Then more.", "Reads a file."),
    ("Line one\n\n  continues here! Next", "Line one continues here!"),
    ("No terminal punctuation", "No terminal punctuation"),
    (123, "123"),
])
def test_first_sentence(text, expected):
    assert tc.first_sentence(text) == expected


def test_tool_row_normalizes_risk():
    assert tc._tool_row("a", "Do it. More", "safe") == {"name": "a", "description": "Do it.", "risk": "read"}
    assert tc._tool_row("b", None, "destructive")["risk"] == "exec"
    assert tc._tool_row("c", None, "write")["risk"] == "write"
    assert tc._tool_row("d", None, "bogus")["risk"] is None
    assert tc._tool_row("e", None, 3)["risk"] is None


# ---------------------------------------------------------------------------
# MCP config translation
# ---------------------------------------------------------------------------


def test_fluid_mcp_config_stdio_and_http():
    stdio = McpServerConfig(name="s", command="node", args=["x.js"], env={"K": "V"})
    assert tc._fluid_mcp_config(stdio) == {
        "transport": "stdio", "command": "node", "args": ["x.js"], "env": {"K": "V"},
    }
    bare = McpServerConfig(name="s", command="node")
    assert tc._fluid_mcp_config(bare) == {"transport": "stdio", "command": "node", "args": [], "env": None}
    assert tc._fluid_mcp_config(McpServerConfig(name="s")) is None

    http = McpServerConfig(name="h", type=McpServerType.HTTP, url="https://mcp.example/")
    assert tc._fluid_mcp_config(http) == {"transport": "http", "url": "https://mcp.example/"}
    assert tc._fluid_mcp_config(McpServerConfig(name="h", type=McpServerType.HTTP)) is None
    # Plain-string transports (not the enum) and duck-typed objects.
    assert tc._fluid_mcp_config(SimpleNamespace(type="streamable_http", url="u")) == {
        "transport": "http", "url": "u",
    }
    assert tc._fluid_mcp_config(McpServerConfig(name="e", type=McpServerType.SSE, url="u")) is None


# ---------------------------------------------------------------------------
# tools/list cache
# ---------------------------------------------------------------------------


async def test_mcp_server_tools_caches_and_refreshes(monkeypatch):
    calls = []

    async def fake_list(name, config):
        calls.append(name)
        return [{"name": f"mcp__{name}__t{len(calls)}", "description": None, "risk": None}]

    monkeypatch.setattr(tc, "_list_mcp_tools", fake_list)
    server = McpServerConfig(name="srv", command="node")

    rows, err = await tc.mcp_server_tools("srv", server)
    assert err is None and rows[0]["name"] == "mcp__srv__t1"
    # A cached answer is a copy: mutating it must not poison the cache.
    rows.clear()
    rows2, _ = await tc.mcp_server_tools("srv", server)
    assert rows2[0]["name"] == "mcp__srv__t1" and calls == ["srv"]

    rows3, _ = await tc.mcp_server_tools("srv", server, refresh=True)
    assert rows3[0]["name"] == "mcp__srv__t2"

    # An expired entry is re-fetched.
    tc._MCP_CACHE["srv"] = (0.0, [], None)
    rows4, _ = await tc.mcp_server_tools("srv", server)
    assert rows4[0]["name"] == "mcp__srv__t3"


async def test_mcp_server_tools_error_paths(monkeypatch):
    unsupported = McpServerConfig(name="sse", type=McpServerType.SSE, url="u")
    rows, err = await tc.mcp_server_tools("sse", unsupported)
    assert rows == [] and "transport not supported" in err

    async def slow(name, config):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(tc, "_list_mcp_tools", slow)
    rows, err = await tc.mcp_server_tools("slow", McpServerConfig(name="slow", command="x"))
    assert rows == [] and "timed out" in err

    async def broken(name, config):
        raise RuntimeError("handshake failed")

    monkeypatch.setattr(tc, "_list_mcp_tools", broken)
    rows, err = await tc.mcp_server_tools("bad", McpServerConfig(name="bad", command="x"))
    assert rows == [] and err == "bad: handshake failed"


async def test_list_mcp_tools_closes_client_even_on_failure(monkeypatch):
    import fluid.tools.mcp as fmcp

    closed = []

    class FakeClient:
        def __init__(self, name, config):
            self.name = name

        async def start(self):
            return None

        async def list_tools(self):
            return [
                SimpleNamespace(name="zeta", description="Z. more", risk="safe"),
                SimpleNamespace(name="alpha", description=None, risk=None),
            ]

        async def aclose(self):
            closed.append(self.name)
            raise RuntimeError("close noise is swallowed")

    monkeypatch.setattr(fmcp, "McpClient", FakeClient)
    rows = await _REAL_LIST_MCP_TOOLS("s", {"transport": "stdio"})
    assert [r["name"] for r in rows] == ["mcp__s__alpha", "mcp__s__zeta"]
    assert rows[1]["risk"] == "read"

    class Failing(FakeClient):
        async def start(self):
            raise RuntimeError("spawn failed")

    monkeypatch.setattr(fmcp, "McpClient", Failing)
    with pytest.raises(RuntimeError, match="spawn failed"):
        await _REAL_LIST_MCP_TOOLS("f", {"transport": "stdio"})
    assert closed == ["s", "f"]


# ---------------------------------------------------------------------------
# Gateway availability
# ---------------------------------------------------------------------------


def test_gateway_unavailable_reasons(monkeypatch):
    from app.core.config import settings
    from app.db import database
    import app.mcp.connector_gateway_sdk as gw

    me = settings.effective_instance_id
    monkeypatch.setattr(database, "get_active_connectors", lambda: [
        {"type": "slack", "runner_id": "other-install"},
        {"type": "discord", "runner_id": "other-install"},
        {"type": "discord", "runner_id": me},
        {"type": "telegram", "runner_id": None},
        {"type": None, "runner_id": "x"},
    ])
    monkeypatch.setattr(gw, "get_gateway_tool_names", lambda: [
        "mcp__builtins__slack_actions", "mcp__builtins__slack_invoke",
        "mcp__builtins__discord_actions", "mcp__builtins__telegram_actions",
        "mcp__builtins__hue_actions", "mcp__builtins__misc", "bare_invoke",
    ])
    reasons = tc._gateway_unavailable_reasons()
    assert set(reasons) == {"mcp__builtins__slack_actions", "mcp__builtins__slack_invoke"}
    assert "another install" in reasons["mcp__builtins__slack_actions"]


def test_gateway_unavailable_reasons_is_best_effort(monkeypatch):
    from app.db import database

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_active_connectors", boom)
    assert tc._gateway_unavailable_reasons() == {}


# ---------------------------------------------------------------------------
# AI tool config mapping
# ---------------------------------------------------------------------------


def test_ai_tool_names_for_config():
    every = tc.ai_tool_names_for_config(None, every=True)
    assert "mcp__ai-tools__generateImage" in every and "mcp__ai-tools__runFalModel" in every
    assert tc.ai_tool_names_for_config(None) == []
    only_images = tc.ai_tool_names_for_config({"image_generation": True})
    # The single category plus the two catch-alls that ride on any category.
    assert only_images == [
        "mcp__ai-tools__generateImage", "mcp__ai-tools__runFalModel", "mcp__ai-tools__searchModels",
    ]


def test_ai_tools_config_from_names_skips_catch_all():
    assert tc.ai_tools_config_from_names(["mcp__ai-tools__runFalModel", "mcp__ai-tools__searchModels"]) == {}
    cfg = tc.ai_tools_config_from_names(["mcp__ai-tools__generateVideo", "mcp__ai-tools__textTo3D"])
    assert cfg == {"video_generation": True, "video_with_audio": True, "text_to_3d": True}
    assert tc.ai_tools_config_from_names([]) == {}


def test_ai_tool_definitions_skips_a_failing_builder(monkeypatch):
    from app.mcp import ai_tools_sdk

    def boom(_worker=None):
        raise RuntimeError("builder broke")

    monkeypatch.setattr(ai_tools_sdk, "_create_image_tools", boom)
    monkeypatch.setattr(ai_tools_sdk, "_create_video_tools", lambda w: [SimpleNamespace(name="v", description="V.")])
    monkeypatch.setattr(ai_tools_sdk, "_create_3d_tools", lambda w: None)
    monkeypatch.setattr(ai_tools_sdk, "_create_audio_gen_tools", lambda w: [])
    monkeypatch.setattr(ai_tools_sdk, "_create_catalog_tools", lambda: [SimpleNamespace(name="c", description=None)])
    monkeypatch.setattr(ai_tools_sdk, "_create_generic_tools", lambda w: [])
    assert tc._ai_tool_definitions() == {"v": "V.", "c": None}


def test_ai_tools_key_lookup_failure_is_unavailable(monkeypatch):
    from app.mcp import ai_tools_sdk

    def boom():
        raise RuntimeError("no key store")

    monkeypatch.setattr(ai_tools_sdk, "_get_fal_api_key", boom)
    monkeypatch.setattr(tc, "_ai_tool_definitions", lambda: {})
    rows, available = tc.ai_tools()
    assert available is False and rows and all(r["available"] is False for r in rows)
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)


def test_ai_tools_are_unavailable_while_fal_is_off(mock_db, _api_unit_credentials, monkeypatch):
    from app.api.settings_crypto import set_encrypted_api_key
    from app.core import fal_credential

    set_encrypted_api_key("fal_api_key", "fal-owner-0123456789abcdefghijklmnop")
    monkeypatch.setenv("FAL_KEY", "fal-from-env")
    monkeypatch.setattr(tc, "_ai_tool_definitions", lambda: {})
    _rows, available = tc.ai_tools()
    assert available is True
    fal_credential.set_enabled(False)
    rows, available = tc.ai_tools()
    assert available is False and rows and all(r["available"] is False for r in rows)


def test_computer_use_tools_availability_error(monkeypatch):
    from app.mcp.computer_use import scaling

    def boom():
        raise RuntimeError("no display")

    monkeypatch.setattr(scaling, "computer_use_available", boom)
    rows, ok, reason = tc.computer_use_tools()
    assert ok is False and reason == "no display"
    assert all(r["description"] is None for r in rows)

    monkeypatch.setattr(scaling, "computer_use_available", lambda: (True, "ignored"))
    _rows, ok, reason = tc.computer_use_tools()
    assert ok is True and reason is None


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


def _stub_groups(monkeypatch, *, cu=(True, None)):
    monkeypatch.setattr(tc, "harness_tools", lambda: [tc._tool_row("Read", "Reads.")])
    monkeypatch.setattr(tc, "app_tools", lambda: (_ for _ in ()).throw(RuntimeError("app broke")))
    monkeypatch.setattr(tc, "ai_tools", lambda: ([tc._tool_row("mcp__ai-tools__x", None)], True))
    monkeypatch.setattr(tc, "computer_use_tools", lambda: ([], cu[0], cu[1]))
    monkeypatch.setattr(tc, "app_config_tools", lambda: [])


async def test_build_tool_surface_isolates_group_failures(monkeypatch):
    _stub_groups(monkeypatch)
    import app.api.computer_use as cu_api

    monkeypatch.setattr(cu_api, "get_computer_use_enabled", lambda: False)

    class Svc:
        def get_disabled_servers(self):
            return ["off"]

        def get_all_servers_with_builtins(self):
            return [
                McpServerConfig(name="browser", command="node"),
                McpServerConfig(name="zeta", command="z"),
                McpServerConfig(name="alpha", type=McpServerType.SSE, url="u"),
                McpServerConfig(name="off", command="o"),
            ]

    import app.core.mcp_service as ms

    monkeypatch.setattr(ms, "get_mcp_service", lambda: Svc())

    async def fake_list(name, config):
        return [tc._tool_row(f"mcp__{name}__t", None)]

    monkeypatch.setattr(tc, "_list_mcp_tools", fake_list)

    surface = await tc.build_tool_surface()
    ids = [g["id"] for g in surface["groups"]]
    assert ids == ["harness", "app", "ai", "browser", "computer_use", "app_config", "mcp:alpha", "mcp:zeta"]
    groups = {g["id"]: g for g in surface["groups"]}
    assert groups["app"]["tools"] == []
    assert groups["ai"]["available"] is True
    assert groups["computer_use"]["available"] is False
    assert groups["app_config"]["admin_only"] is True
    assert groups["mcp:zeta"]["tools"][0]["name"] == "mcp__zeta__t"
    errors = {e["group"]: e["message"] for e in surface["errors"]}
    assert errors["app"] == "app broke"
    assert "Settings" in errors["computer_use"]
    assert "transport not supported" in errors["mcp:alpha"]
    assert surface["discovered_at"].endswith("+00:00")


async def test_build_tool_surface_toggle_read_failure_and_no_servers(monkeypatch):
    _stub_groups(monkeypatch)
    import app.api.computer_use as cu_api
    import app.core.mcp_service as ms

    def boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(cu_api, "get_computer_use_enabled", boom)
    monkeypatch.setattr(ms, "get_mcp_service", boom)
    surface = await tc.build_tool_surface()
    ids = [g["id"] for g in surface["groups"]]
    assert "browser" not in ids and not any(i.startswith("mcp:") for i in ids)
    errors = {e["group"]: e["message"] for e in surface["errors"]}
    assert errors["computer_use"] == "could not read the computer-use toggle"
    assert errors["mcp"] == "settings unreadable"


async def test_build_tool_surface_all_groups_failing(monkeypatch):
    def boom():
        raise RuntimeError("x")

    for fn in ("harness_tools", "app_tools", "ai_tools", "computer_use_tools", "app_config_tools"):
        monkeypatch.setattr(tc, fn, boom)
    monkeypatch.setattr(tc, "_configured_mcp_servers", lambda errors: (None, {}))
    surface = await tc.build_tool_surface()
    assert all(g["tools"] == [] for g in surface["groups"])
    groups = {g["id"]: g for g in surface["groups"]}
    assert groups["ai"]["available"] is False and groups["computer_use"]["available"] is False
    assert {e["group"] for e in surface["errors"]} == {
        "harness", "app", "ai", "computer_use", "app_config",
    }


def test_app_tools_marks_shadowed_and_survives_server_build_failure(monkeypatch):
    import app.mcp.builtin_tools as bt
    from app.core.provider import fluid_tool_bridge

    def boom(**_kw):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(bt, "create_builtin_tools_server", boom)
    shadowed = sorted(fluid_tool_bridge.NATIVE_SHADOWED_BUILTINS)
    names = [f"mcp__builtins__{n}" for n in shadowed] + ["mcp__builtins__zz_plain"]
    monkeypatch.setattr(bt, "get_builtin_tool_names", lambda: names)
    monkeypatch.setattr(tc, "_gateway_unavailable_reasons", lambda: {"mcp__builtins__zz_plain": "elsewhere"})
    rows = {r["name"]: r for r in tc.app_tools()}
    assert rows["mcp__builtins__zz_plain"]["unavailable_reason"] == "elsewhere"
    if shadowed:
        first = rows[f"mcp__builtins__{shadowed[0]}"]
        assert first["available"] is False and "harness" in first["unavailable_reason"]


def test_app_config_tools_survives_server_build_failure(monkeypatch):
    import app.mcp.app_config_sdk as acs

    def boom(**_kw):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(acs, "create_app_config_server", boom)
    monkeypatch.setattr(acs, "get_app_config_tool_names", lambda: ["mcp__app-config__b", "mcp__app-config__a"])
    assert [r["name"] for r in tc.app_config_tools()] == ["mcp__app-config__a", "mcp__app-config__b"]
