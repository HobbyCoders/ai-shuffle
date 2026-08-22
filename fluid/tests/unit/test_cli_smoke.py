"""M6 — CLI smoke tests (offline): one-shot ``run`` and the REPL permission path.

A scripted fake provider replays StreamChunks so the whole engine + CLI runs
with no network. The REPL's permission handler is driven with a scripted stdin
function (unit-level) rather than a pty.
"""

import asyncio
import io
import json
from collections.abc import AsyncIterator

import pytest

from fluid.cli.__main__ import _version, build_parser, main, run_once
from fluid.cli.config import list_sessions, make_engine, make_store
from fluid.cli.repl import Repl, stream_turn
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
from fluid.config.model import HarnessConfig
from fluid.events.model import ToolResult
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _tool(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _config() -> HarnessConfig:
    return HarnessConfig.from_dict(BUILTIN_DEFAULTS)


def test_run_once_streams_text_and_returns_zero() -> None:
    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_text("hello from fluid")])

    rc = asyncio.run(run_once(_config(), store, "hi", provider=provider, out=out))
    assert rc == 0
    assert "hello from fluid" in out.getvalue()
    assert "[done]" in out.getvalue()


def test_run_once_persists_session(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = ScriptedProvider([_text("persisted reply")])
    rc = asyncio.run(run_once(_config(), store, "hi", provider=provider, out=io.StringIO()))
    assert rc == 0
    sessions = list_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0]["nodes"] >= 2  # user node + assistant node


def _weather_registry() -> ToolRegistry:
    @tool("touch", "Touch marker", {"type": "object", "properties": {}}, risk="write")
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [touch]))
    return registry


def test_repl_permission_allow_path() -> None:
    """The REPL's scripted-stdin permission handler allows a write tool."""
    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_tool("t1", "mcp__m__touch", "{}"), _text("done")])
    config = _config()  # default approval_policy on_write → the write tool asks

    async def scenario() -> None:
        meta = await store.create_session()
        engine = make_engine(
            config, store, provider=provider, session_id=meta.session_id,
            registry=_weather_registry(),
        )
        repl = Repl(engine, meta.session_id, input_fn=lambda prompt: "y", out=out)
        await repl.run_turn("please touch")
        await engine.aclose()

    asyncio.run(scenario())
    text = out.getvalue()
    assert "touched" in text  # allowed → tool ran
    assert "[done]" in text


def test_repl_permission_deny_path() -> None:
    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_tool("t1", "mcp__m__touch", "{}"), _text("done")])

    async def scenario() -> None:
        meta = await store.create_session()
        engine = make_engine(
            _config(), store, provider=provider, session_id=meta.session_id,
            registry=_weather_registry(),
        )
        # scripted stdin answers "n" → deny
        repl = Repl(engine, meta.session_id, input_fn=lambda prompt: "n", out=out)
        await repl.run_turn("please touch")
        result = next(e for e in engine.bus.log(meta.session_id) if isinstance(e, ToolResult))
        assert result.is_error
        assert "permission denied" in result.output
        await engine.aclose()

    asyncio.run(scenario())


def test_repl_slash_model_and_exit() -> None:
    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_text("x")])

    async def scenario() -> None:
        meta = await store.create_session()
        engine = make_engine(_config(), store, provider=provider, session_id=meta.session_id)
        repl = Repl(engine, meta.session_id, out=out)
        cont = await repl.dispatch_slash("/model claude-opus-9")
        assert cont is True
        assert engine.model == "claude-opus-9"
        assert await repl.dispatch_slash("/exit") is False
        await engine.aclose()

    asyncio.run(scenario())


def test_stream_turn_reports_stop_reason() -> None:
    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_text("ok")])

    async def scenario() -> str:
        config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {}))
        meta = await store.create_session()
        engine = make_engine(config, store, provider=provider, session_id=meta.session_id)
        stop = await stream_turn(engine, meta.session_id, "hi", out=out)
        await engine.aclose()
        return stop

    assert asyncio.run(scenario()) == "end_turn"


def test_import_settings_cli_previews_then_applies_selected_item(tmp_path, capsys) -> None:
    source = tmp_path / "source home"
    destination = tmp_path / "destination home"
    instruction = source / ".claude" / "CLAUDE.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text("imported guidance", encoding="utf-8")

    assert main([
        "import-settings",
        "--source", "claude",
        "--source-home", str(source),
        "--destination-home", str(destination),
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    item = next(entry for entry in preview["items"] if entry["kind"] == "global_instruction")
    assert item["status"] == "ready"
    assert not (destination / ".fluid" / "AGENTS.md").exists()

    assert main([
        "import-settings",
        "--source", "claude",
        "--source-home", str(source),
        "--destination-home", str(destination),
        "--select", item["id"],
        "--apply",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    outcome = next(entry for entry in result["items"] if entry["id"] == item["id"])
    assert outcome["status"] == "imported"
    assert (destination / ".fluid" / "AGENTS.md").read_text() == "imported guidance"


def test_version_flag_prints_and_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"fluid {_version()}"


def test_import_settings_cli_aliases_choose_one_provider() -> None:
    parser = build_parser()
    assert parser.parse_args(["import-claude"]).command == "import-claude"
    assert parser.parse_args(["import-codex"]).command == "import-codex"
