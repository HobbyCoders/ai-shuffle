"""CLI gaps closed against Claude Code / Codex expectations (offline):

* ``fluid sessions`` listing rows and their table rendering;
* ``--resume`` / ``--continue`` building an engine on the EXISTING session so
  the native loop's rehydration runs (deferred catalog, compaction events);
* ``/context`` (``/usage``), ``/tools`` and ``/skills`` REPL rendering.
"""

import asyncio
import io
import json
from collections.abc import AsyncIterator

import pytest

from fluid.cli.__main__ import SessionNotFound, build_parser, main, run_once
from fluid.cli.config import (
    format_sessions,
    latest_session_id,
    list_sessions,
    make_engine,
    make_store,
    resolved_prompt,
)
from fluid.cli.repl import Repl, format_context_usage, format_skills, format_tools
from fluid.config.loader import BUILTIN_DEFAULTS
from fluid.config.model import HarnessConfig
from fluid.context.instructions import Skill
from fluid.events.model import ContextCompaction, Usage
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


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _config() -> HarnessConfig:
    return HarnessConfig.from_dict(BUILTIN_DEFAULTS)


# ─── sessions listing ────────────────────────────────────────────────────────


def test_list_sessions_rows_carry_snippet_model_and_activity(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = ScriptedProvider([_text("reply one")])
    asyncio.run(run_once(_config(), store, "first question here", provider=provider,
                         out=io.StringIO()))
    rows = list_sessions(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["first_message"] == "first question here"
    assert row["nodes"] == 2
    assert row["last_active"] >= row["created_at"]
    assert row["model"]
    assert row["provider"] == "anthropic"


def test_list_sessions_newest_first_and_limit(tmp_path) -> None:
    store = make_store(tmp_path)
    ids = []
    for i in range(3):
        provider = ScriptedProvider([_text(f"r{i}")])
        meta = asyncio.run(store.create_session())
        ids.append(meta.session_id)
        asyncio.run(run_once(_config(), store, f"q{i}", provider=provider,
                             out=io.StringIO(), session_id=meta.session_id))
    # Rewrite timestamps so ordering is deterministic regardless of clock granularity.
    for i, sid in enumerate(ids):
        log = tmp_path / f"{sid}.jsonl"
        lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        for rec in lines:
            if rec["t"] == "meta":
                rec["created_at"] = 1000.0 + i
            elif rec["t"] == "node":
                rec["node"]["ts"] = 2000.0 + i
        log.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    rows = list_sessions(tmp_path)
    assert [r["session_id"] for r in rows] == list(reversed(ids))
    assert [r["session_id"] for r in list_sessions(tmp_path, limit=1)] == [ids[-1]]
    assert latest_session_id(tmp_path) == ids[-1]


def test_format_sessions_table() -> None:
    rows = [{
        "session_id": "ses_abc", "created_at": 1700000000.0, "last_active": 1700003600.0,
        "nodes": 4, "model": "claude-x", "provider": "anthropic",
        "first_message": "please   refactor the " + "x" * 100,
    }]
    text = format_sessions(rows, snippet_width=30)
    lines = text.splitlines()
    assert lines[0].startswith("SESSION")
    assert "ses_abc" in lines[1]
    assert "anthropic/claude-x" in lines[1]
    assert "please refactor the" in lines[1]
    assert lines[1].endswith("…")  # truncated with an ellipsis
    assert format_sessions([]) == "(no sessions)"


def test_sessions_command_prints_table(tmp_path, capsys) -> None:
    store = make_store(tmp_path)
    asyncio.run(run_once(_config(), store, "hello", provider=ScriptedProvider([_text("x")]),
                         out=io.StringIO()))
    assert main(["sessions", "--store", str(tmp_path), "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "SESSION" in out and "hello" in out


# ─── resume ──────────────────────────────────────────────────────────────────


def test_parser_accepts_resume_and_continue() -> None:
    parser = build_parser()
    assert parser.parse_args(["chat", "--resume", "ses_1"]).resume == "ses_1"
    assert parser.parse_args(["run", "--continue", "hi"]).continue_last is True


def test_run_once_resume_builds_on_existing_session(tmp_path) -> None:
    store = make_store(tmp_path)
    first = ScriptedProvider([_text("first reply")])
    asyncio.run(run_once(_config(), store, "remember me", provider=first, out=io.StringIO()))
    sid = list_sessions(tmp_path)[0]["session_id"]

    # A fresh store (new process) and a fresh engine against the stored session.
    store2 = make_store(tmp_path)
    second = ScriptedProvider([_text("second reply")])
    out = io.StringIO()
    rc = asyncio.run(run_once(_config(), store2, "and again", provider=second,
                              out=out, session_id=sid))
    assert rc == 0
    assert "second reply" in out.getvalue()
    # Still one session, now holding both turns, and the model saw the old history.
    rows = list_sessions(tmp_path)
    assert [r["session_id"] for r in rows] == [sid]
    assert rows[0]["nodes"] == 4
    history_text = " ".join(m.text() for m in second.requests[0].messages)
    assert "remember me" in history_text and "first reply" in history_text


def test_resume_rehydrates_compaction_and_deferred_catalog(tmp_path) -> None:
    @tool("touch", "Touch marker", {"type": "object", "properties": {}})
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    def registry() -> ToolRegistry:
        r = ToolRegistry()
        r.register_inproc(create_inproc_server("m", [touch]), defer=True)
        return r

    store = make_store(tmp_path)
    first = ScriptedProvider([_text("ok")])
    sid: str = ""

    async def first_run() -> None:
        nonlocal sid
        meta = await store.create_session()
        sid = meta.session_id
        engine = make_engine(_config(), store, provider=first, session_id=sid,
                             registry=registry())
        repl = Repl(engine, sid, out=io.StringIO())
        await repl.run_turn("hi")
        # A persisted compaction decision the resumed engine must replay.
        await store.append_event(sid, ContextCompaction(
            session_id=sid, seq=999, tier=1, reason="test", trimmed=[], spans=[],
        ))
        await engine.aclose()

    asyncio.run(first_run())
    assert "mcp__m__touch" in first.requests[0].messages[0].text()

    store2 = make_store(tmp_path)
    second = ScriptedProvider([_text("ok again")])

    async def resumed_run() -> None:
        engine = make_engine(_config(), store2, provider=second, session_id=sid,
                             registry=registry())
        repl = Repl(engine, sid, out=io.StringIO())
        await repl.run_turn("again")
        assert engine._deferred_rehydrated and engine._compaction_rehydrated
        assert "mcp__m__touch" in engine._deferred_announced
        await engine.aclose()

    asyncio.run(resumed_run())
    # The catalog was already in the stored history, so it is not announced again.
    last_user = second.requests[0].messages[-1].text()
    assert "mcp__m__touch" not in last_user


def test_resume_unknown_session_is_an_error(tmp_path, capsys) -> None:
    store = make_store(tmp_path)
    with pytest.raises(SessionNotFound):
        asyncio.run(run_once(_config(), store, "x", provider=ScriptedProvider([_text("x")]),
                             out=io.StringIO(), session_id="ses_missing"))
    assert main(["run", "--store", str(tmp_path), "--continue", "hi"]) == 2
    assert "no such session" in capsys.readouterr().err


# ─── environment block ───────────────────────────────────────────────────────


def test_cli_prompt_carries_environment_block_before_extra_blocks(tmp_path) -> None:
    # ``system`` is now the base prompt, so the extra block comes from a
    # per-run append instead.
    config = HarnessConfig.from_dict({**BUILTIN_DEFAULTS, "cwd": str(tmp_path)})
    compiled = resolved_prompt(config, extra_system="app block")
    kinds = [s.kind for s in compiled.sections]
    assert "environment" in kinds
    assert kinds.index("environment") < kinds.index("extra")
    assert "Working directory:" in compiled.text and "Today's date:" in compiled.text


# ─── /context, /tools, /skills ───────────────────────────────────────────────


def test_format_context_usage_from_synthetic_dict() -> None:
    usage = {
        "totalTokens": 20_000, "maxTokens": 200_000, "percentage": 10.0,
        "model": "claude-x", "measured": True,
        "categories": [
            {"name": "System prompt", "tokens": 4_000, "color": "#000"},
            {"name": "System tools", "tokens": 6_000, "color": "#000"},
            {"name": "Messages", "tokens": 10_000, "color": "#000"},
            {"name": "MCP tools (deferred)", "tokens": 500, "color": "#000", "isDeferred": True},
            {"name": "Free space", "tokens": 180_000, "color": "#000"},
        ],
        "compaction": {"digestedSpans": 2, "reclaimedTokens": 1234},
    }
    last = Usage(session_id="s", input_tokens=2_000, cache_read_tokens=8_000,
                 cache_creation_tokens=0, output_tokens=10)
    text = format_context_usage(usage, last_usage=last)
    lines = text.splitlines()
    assert lines[0] == "context: 20,000 / 200,000 tokens (10.0%)"
    assert lines[1] == "model: claude-x"
    assert any("System prompt" in l and "4,000" in l and "2.0%" in l for l in lines)
    assert any("Messages" in l and "10,000" in l and "5.0%" in l for l in lines)
    assert any("MCP tools (deferred)" in l and "not in prompt" in l for l in lines)
    assert any("Free space" in l and "180,000" in l for l in lines)
    assert any("cache hit rate 80%" in l for l in lines)
    assert lines[-1].startswith("compaction: digestedSpans=2")
    assert format_context_usage(None).startswith("context usage unavailable")


def test_repl_context_tools_skills_commands(tmp_path, monkeypatch) -> None:
    @tool("touch", "Touch marker", {"type": "object", "properties": {}}, risk="write")
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("m", [touch]), defer=True)
    skills_dir = tmp_path / ".agents" / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill\n---\nbody\n", encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")

    out = io.StringIO()
    store = MemoryStore()
    provider = ScriptedProvider([_text("hello")])
    config = HarnessConfig.from_dict({**BUILTIN_DEFAULTS, "cwd": str(tmp_path)})

    async def scenario() -> None:
        meta = await store.create_session()
        engine = make_engine(config, store, provider=provider, session_id=meta.session_id,
                             registry=reg)
        repl = Repl(engine, meta.session_id, out=out)
        await repl.run_turn("hi")
        assert await repl.dispatch_slash("/context")
        assert await repl.dispatch_slash("/usage")
        assert await repl.dispatch_slash("/tools")
        assert await repl.dispatch_slash("/skills")
        assert await repl.dispatch_slash("/help")
        await engine.aclose()

    asyncio.run(scenario())
    text = out.getvalue()
    assert text.count("context:") == 2
    assert "cache hit rate" in text
    assert "mcp__m__touch" in text and "deferred" in text and "risk=write" in text
    assert "demo" in text and "[project] A demo skill" in text
    assert "/context" in text and "--resume" in text


def test_format_tools_and_skills_empty() -> None:
    assert format_tools(None) == "(no tool registry)"
    assert format_tools(ToolRegistry()) == "(no tools registered)"
    assert format_skills([]) == "(no skills discovered)"
    text = format_skills([Skill(name="a", description="d " * 60, path="p", scope="global",
                                body_loaded=False)])
    assert text.splitlines()[1].endswith("…")
