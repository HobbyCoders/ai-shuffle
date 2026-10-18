"""First-request context overhead of a default native session, and how well the
context-usage breakdown describes it.

A real HarnessAdapter connects a native leg over a recording provider with the
app's bundled in-proc servers (builtins, app-config, chat-access, ai-tools), the
default-profile preset prompt and private-access guidance. The first request is
priced with the app's own estimator (chars/4) and compared with the breakdown
``get_context_usage`` reported before the turn.

Budgets are set from the measurement taken when the native leg's eager tier
became an allowlist of the turn-by-turn core (Oct 2026): tool rows were ~17.0k
tokens in code mode and ~16.9k on the JSON-tool path before, ~8.4k and ~7.7k
after. They catch a regression that puts a big schema or doc block back on
every first request.
"""
from __future__ import annotations

import json

import pytest

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.context.trim import estimate_tokens_of_messages, estimate_tokens_of_text
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore

# Reported "System tools" + "MCP tools" (what every first request pays for tool
# docs or schemas). Measured ~8.4k (code mode) / ~9.6k (JSON path, which
# also keeps the frequently used tools loaded: NATIVE_JSON_EAGER_TOOLS).
TOOL_ROWS_BUDGET = 10_000
# Whole first request, prompt + schemas + first user message, with an empty
# cwd and home. Measured ~24.6k (code mode) / ~23.3k (JSON path) before the
# core-only eager tier, ~14.8k / ~14.7k after it, ~13.5k / ~13.6k after the
# wording pass; the margin absorbs host-dependent sections (workspace facts,
# environment).
FIRST_REQUEST_BUDGET = 14_300
# The breakdown before the first turn may miss only the per-turn reminders the
# host adds while sending (memory snapshot, environment note): ~1.7% measured.
REPORT_TOLERANCE = 0.03

_CHUNKS = [
    StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
    StreamChunk("block_start", {"index": 0, "block_type": "text"}),
    StreamChunk("text_delta", {"index": 0, "text": "ok"}),
    StreamChunk("block_stop", {"index": 0}),
    StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
]


class _RecordingProvider:
    name = "fake"
    caps = ProviderCaps()

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in _CHUNKS:
            yield chunk


def _default_servers(session_id: str, cwd: str) -> dict:
    from app.mcp.ai_tools_sdk import create_ai_tools_server
    from app.mcp.app_config_sdk import create_app_config_server
    from app.mcp.builtin_tools import create_builtin_tools_server
    from app.mcp.chat_access_sdk import create_chat_access_server

    servers = {
        "builtins": create_builtin_tools_server(
            session_id=session_id, profile_id="p", project_id=None, is_admin=True,
            model="gpt-5.4", provider_id=None, cwd=cwd,
        ),
        "app-config": create_app_config_server(is_admin=True, project_dir=cwd),
        "chat-access": create_chat_access_server(session_id),
        "ai-tools": create_ai_tools_server(
            {key: True for key in (
                "image_generation", "image_editing", "video_generation",
                "image_to_video", "audio_generation", "text_to_3d", "image_to_3d",
                "image_reference", "fal_model", "video_extend", "audio_to_audio",
            )},
            tools_dir=cwd,
        ),
    }
    return {name: cfg for name, cfg in servers.items() if cfg}


async def _first_request(monkeypatch, tmp_path, *, code_mode: bool,
                         session_mode: str | None = None, **restrictions):
    import app.core.provider.harness_adapter as ha
    import fluid.context.plugins as fluid_plugins
    from app.core.config import settings
    from app.core.environment import get_security_instructions
    from app.mcp.chat_access_sdk import GUIDANCE

    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    # Instruction and skill discovery walks up from cwd to the git root, else
    # the drive root. Windows temp folders sit inside the user profile, so
    # without a boundary the walk picks up the real profile's .fluid files.
    (cwd / ".git").mkdir()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "fluid_code_mode", code_mode)
    provider = _RecordingProvider()

    async def fake_client(_kind, _options):
        return provider, None

    monkeypatch.setattr(ha, "_build_provider_client", fake_client)
    monkeypatch.setattr(HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore()))
    monkeypatch.setattr(fluid_plugins, "discover_plugins", lambda *a, **kw: [])

    adapter = ha.HarnessAdapter()
    await adapter.connect(SessionOptions(
        model="gpt-5.4",
        cwd=str(cwd),
        system_prompt={
            "type": "preset", "preset": "claude_code",
            "append": get_security_instructions() + "\n\n" + GUIDANCE,
        },
        mcp_servers=_default_servers("app-sess", str(cwd)),
        include_partial_messages=True,
        extra={
            "fluid_provider": "openai_responses",
            "app_session_id": "app-sess",
            "github_connected": True,
            **({"session_mode": session_mode} if session_mode else {}),
        },
        **restrictions,
    ))
    try:
        engine = adapter._engine
        # The chat lane never builds code mode, whatever the setting says.
        assert (engine._codemode is not None) is (code_mode and session_mode != "chat")
        reported = await engine.get_context_usage()
        await adapter.send_query("hello")
        async for _ in adapter.receive_events():
            pass
    finally:
        await adapter.disconnect()
    return engine, provider.requests[0], reported


def _request_tokens(request) -> int:
    return (
        estimate_tokens_of_text(request.system or "")
        + sum(estimate_tokens_of_text(json.dumps(t)) for t in request.tools)
        + estimate_tokens_of_messages(request.messages)
    )


def _rows(reported) -> dict[str, int]:
    return {c["name"]: c["tokens"] for c in reported["categories"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code_mode", "json_tools"])
async def test_default_profile_first_request_stays_under_budget(
    monkeypatch, tmp_path, code_mode
):
    _engine, request, reported = await _first_request(
        monkeypatch, tmp_path, code_mode=code_mode
    )
    rows = _rows(reported)
    tool_rows = rows.get("System tools", 0) + rows.get("MCP tools", 0)
    assert tool_rows <= TOOL_ROWS_BUDGET, rows
    assert _request_tokens(request) <= FIRST_REQUEST_BUDGET, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code_mode", "json_tools"])
async def test_reported_breakdown_matches_the_first_request(
    monkeypatch, tmp_path, code_mode
):
    _engine, request, reported = await _first_request(
        monkeypatch, tmp_path, code_mode=code_mode
    )
    # Counted rows sum to the total; deferred rows and free space stay outside.
    counted = sum(
        c["tokens"] for c in reported["categories"]
        if not c.get("isDeferred") and c["name"] != "Free space"
    )
    assert counted == reported["totalTokens"]
    actual = _request_tokens(request)
    assert abs(reported["totalTokens"] - actual) <= actual * REPORT_TOLERANCE, (
        reported["totalTokens"], actual, _rows(reported),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code_mode", "json_tools"])
async def test_only_the_core_is_eager_and_the_rest_is_announced(
    monkeypatch, tmp_path, code_mode
):
    from app.core.provider.fluid_tool_bridge import (
        NATIVE_EAGER_TOOLS, NATIVE_JSON_EAGER_TOOLS,
    )

    engine, request, reported = await _first_request(
        monkeypatch, tmp_path, code_mode=code_mode
    )
    # The JSON-tool path also keeps the frequently used tools loaded: there a
    # deferred tool costs a ToolSearch round trip and a prompt-cache rebuild.
    eager = NATIVE_EAGER_TOOLS if code_mode else NATIVE_EAGER_TOOLS | NATIVE_JSON_EAGER_TOOLS
    deferred = {s.name for s in engine.registry.deferred_tools()}
    registered = {s.name for s in engine.registry.list_tools(loaded_only=False)}
    # Code mode's own tools register after the session build and stay eager.
    host = registered - {"run_python", "list_tools", "search_tools",
                         "read_tool_docs", "save_skill", "delete_skill",
                         "save_plugin"}
    assert host - deferred == eager & registered
    # The default servers carry every eager tool; a rename must not silently
    # drop one from the core.
    assert eager <= registered
    # Memory is eager: its save and correction rules must be in front of the
    # model when it calls.
    assert {"Remember", "Recall"} <= registered - deferred
    # Deferred now, still callable: peer sessions, private access and the
    # rarely used owner-request tools (plus the task tools under code mode).
    for name in ("StartSession", "mcp__chat-access__RequestChatAccess",
                 "mcp__builtins__AndroidAction", "mcp__app-config__createAutomation"):
        assert name in deferred, name
    for name in ("SessionSearch", "mcp__builtins__TaskOutput"):
        assert (name in deferred) is code_mode, name
    sent_schemas = {t["name"] for t in request.tools}
    assert not (deferred & sent_schemas)
    if code_mode:
        # Names only, one line per server, in the code-mode catalog.
        catalog = request.system[request.system.index("## Deferred tools"):]
        rows = [ln for ln in catalog.splitlines() if ln.startswith("- ")]
        listed = {leaf for row in rows for leaf in row.split(": ", 1)[1].split(", ")}
        for name in deferred:
            leaf = name.rsplit("__", 1)[-1]
            # A tool whose host capability is not wired here leaves the catalog.
            assert (leaf in listed) == engine._tool_wired(name), leaf
            assert f"## {leaf}\n" not in request.system
    else:
        # Announced by name in the first turn's catalog reminder.
        from fluid.tools.builtins.tool_search import parse_deferred_catalog

        text = "\n".join(
            getattr(b, "text", "") for m in request.messages for b in m.content
        )
        # Tools whose host capability is not wired here are dropped, as the
        # engine drops them from the request.
        wired = {name for name in deferred if engine._tool_wired(name)}
        assert wired and wired <= parse_deferred_catalog(text)


def _tool_names(request) -> set[str]:
    return {t["name"] for t in request.tools}


@pytest.mark.asyncio
async def test_chat_lane_keeps_every_permitted_tool_loaded(monkeypatch, tmp_path):
    """The chat lane runs JSON tool calls and its ceiling leaves ToolSearch
    out, so a deferred tool there could never be loaded: nothing beyond the
    deferred servers may be deferred (review of the core-only eager tier)."""
    from app.core.session_modes import CHAT_MODE_DENIED_TOOLS, CHAT_MODE_TOOL_SURFACE

    engine, request, _reported = await _first_request(
        monkeypatch, tmp_path, code_mode=True, session_mode="chat",
        tool_allowlist=list(CHAT_MODE_TOOL_SURFACE),
        disallowed_tools=list(CHAT_MODE_DENIED_TOOLS),
    )
    assert not engine._tool_permitted("ToolSearch")
    sent = _tool_names(request)
    for name in ("Read", "WebSearch", "WebFetch", "Remember", "Recall", "Forget",
                 "mcp__chat-access__RequestChatAccess", "mcp__chat-access__ChatResources"):
        assert name in sent, (name, sorted(sent))
    unreachable = {s.name for s in engine.registry.deferred_tools()
                   if engine._tool_wired(s.name)}
    assert not unreachable, sorted(unreachable)


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code_mode", "json_tools"])
async def test_allowlist_without_tool_search_defers_nothing_it_permits(
    monkeypatch, tmp_path, code_mode
):
    """A profile's "allow only selected" list or a Task agent's tools: list
    that omits ToolSearch: every permitted tool stays loaded, because a
    code-mode session whose kernel fails falls back to JSON tool calls."""
    allow = ["Read", "Edit", "Bash", "SessionSearch", "Remember",
             "mcp__builtins__TaskOutput", "mcp__chat-access__ChatResources"]
    engine, request, _reported = await _first_request(
        monkeypatch, tmp_path, code_mode=code_mode, tool_allowlist=allow,
    )
    assert not engine._tool_permitted("ToolSearch")
    unreachable = {s.name for s in engine.registry.deferred_tools()
                   if engine._tool_wired(s.name)}
    assert not unreachable, sorted(unreachable)
    if not code_mode:
        # Every permitted tool whose host capability is wired here is sent.
        wired = {name for name in allow if engine._tool_wired(name)}
        assert wired and wired <= _tool_names(request), sorted(_tool_names(request))


@pytest.mark.asyncio
async def test_deferred_tools_stay_loadable_on_the_json_path(monkeypatch, tmp_path):
    """Code mode off: what is deferred is announced and ToolSearch loads it."""
    engine, request, _reported = await _first_request(
        monkeypatch, tmp_path, code_mode=False,
    )
    assert "ToolSearch" in _tool_names(request)
    deferred = {s.name for s in engine.registry.deferred_tools() if engine._tool_wired(s.name)}
    assert "StartSession" in deferred
    assert engine.registry.load_schema("StartSession")
    assert "StartSession" in {t["name"] for t in engine._tools_api()}
