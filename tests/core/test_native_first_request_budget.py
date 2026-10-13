"""First-request context overhead of a default native session, and how well the
context-usage breakdown describes it.

A real HarnessAdapter connects a native leg over a recording provider with the
app's bundled in-proc servers (builtins, app-config, chat-access, ai-tools), the
default-profile preset prompt and private-access guidance. The first request is
priced with the app's own estimator (chars/4) and compared with the breakdown
``get_context_usage`` reported before the turn.

Budgets are set from the measurement taken when the rarely used tools moved to
the deferred tier (Oct 2026): tool rows were ~20.8k tokens in code mode and
~22.2k on the JSON-tool path before, ~17.0k and ~16.9k after. They catch a
regression that puts a big schema or doc block back on every first request.
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
# docs or schemas). Measured ~17.0k (code mode) / ~16.9k (JSON path).
TOOL_ROWS_BUDGET = 18_000
# Whole first request, prompt + schemas + first user message, with an empty
# cwd and home. Measured ~28.2k (code mode) / ~28.1k (JSON path) before, ~24.6k
# / ~23.3k after; the margin absorbs host-dependent sections (workspace facts,
# environment).
FIRST_REQUEST_BUDGET = 26_500
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


async def _first_request(monkeypatch, tmp_path, *, code_mode: bool):
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
        },
    ))
    try:
        engine = adapter._engine
        assert (engine._codemode is not None) is code_mode
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
async def test_rarely_used_tools_are_deferred_but_announced(
    monkeypatch, tmp_path, code_mode
):
    from app.core.provider.fluid_tool_bridge import NATIVE_DEFERRED_TOOLS

    engine, request, reported = await _first_request(
        monkeypatch, tmp_path, code_mode=code_mode
    )
    deferred = {s.name for s in engine.registry.deferred_tools()}
    registered = {s.name for s in engine.registry.list_tools(loaded_only=False)}
    expected = NATIVE_DEFERRED_TOOLS & registered
    # The default servers carry every one of them; a rename must not silently
    # put a tool back on the eager tier.
    assert expected == NATIVE_DEFERRED_TOOLS
    assert expected <= deferred
    # Kept eager: the operating surface every turn relies on.
    for name in ("Bash", "Read", "Monitor", "Task", "mcp__builtins__TaskOutput",
                 "mcp__chat-access__RequestChatAccess"):
        assert name in registered and name not in deferred, name
    sent_schemas = {t["name"] for t in request.tools}
    assert not (expected & sent_schemas)
    if code_mode:
        # Collapsed to a capped one-liner each in the code-mode catalog.
        catalog = request.system[request.system.index("## Deferred tools"):]
        for name in expected:
            leaf = name.rsplit("__", 1)[-1]
            line = next(ln for ln in catalog.splitlines() if ln.startswith(f"- {leaf}:"))
            assert len(line) < 200, line
            assert f"## {leaf}\n" not in request.system
    else:
        # Announced by name in the first turn's catalog reminder.
        text = "\n".join(
            getattr(b, "text", "") for m in request.messages for b in m.content
        )
        for name in expected:
            assert f"- {name}:" in text
