"""The four M6 seams, wired: nested-instruction injection, skill progressive
loading (Skill builtin), slash-command execution, /model + permission-mode ops
on NativeLoop."""

import io
from typing import AsyncIterator

from omni.cli.repl import Repl
from omni.engine.native import NativeLoop
from omni.events import SetModel, SetPermissionMode, UserTurn
from omni.permissions.engine import PermissionEngine
from omni.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from omni.store.base import MemoryStore
from omni.tools.registry import ToolCtx, ToolOutcome, ToolRegistry
from omni.tools.builtins import register_builtins
from omni.tools.builtins.skill import handler as skill_handler


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _tool_call(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _all_text(message) -> str:
    """Every Text block in the message, including those nested inside
    ToolResultBlocks (where the injected reminders ride)."""
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        for inner in getattr(block, "content", None) or []:
            inner_text = getattr(inner, "text", None)
            if isinstance(inner_text, str):
                parts.append(inner_text)
    return "\n".join(parts)


# ─── 1. /model + permission-mode ops on NativeLoop ───────────────────────────


async def test_set_model_and_permission_mode_ops() -> None:
    perms = PermissionEngine("on_write")
    loop = NativeLoop(
        provider=ScriptedProvider([_text("hi")]), store=MemoryStore(),
        model="model-a", permissions=perms,
    )
    await loop.submit(SetModel(model="model-b"))
    assert loop.model == "model-b"

    await loop.submit(SetPermissionMode(mode="plan"))
    assert perms.approval_policy == "always_ask" and loop.plan_mode is True

    await loop.submit(SetPermissionMode(mode="bypassPermissions"))
    assert perms.approval_policy == "never_ask" and loop.plan_mode is False

    # omni-native policy names pass straight through.
    await loop.submit(SetPermissionMode(mode="on_write"))
    assert perms.approval_policy == "on_write"


# ─── 2. Nested-instruction injection ─────────────────────────────────────────


async def test_nested_instructions_ride_tool_result_once(tmp_path) -> None:
    project = tmp_path / "proj"
    nested = project / "sub"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()  # git root = project boundary
    (nested / "CLAUDE.md").write_text("Nested rule: use tabs.")
    (nested / "f.txt").write_text("data")

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool_call("t1", "Read", f'{{"file_path": "{nested / "f.txt"}"}}'),
        _tool_call("t2", "Read", f'{{"file_path": "{nested / "f.txt"}"}}'),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(cwd=str(project)),
    )
    await loop.submit(UserTurn(content="read it twice"))
    await loop.wait_turn()

    # The nested CLAUDE.md rides the FIRST tool result only (once per session).
    path = await store.load_path(sid)
    joined = "\n".join(_all_text(n.message) for n in path)
    assert joined.count("Nested rule: use tabs.") == 1
    assert "<system-reminder>" in joined


async def test_project_root_instructions_not_reinjected(tmp_path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "CLAUDE.md").write_text("Root rule.")  # in the project stack
    (project / "f.txt").write_text("data")

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool_call("t1", "Read", f'{{"file_path": "{project / "f.txt"}"}}'),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(cwd=str(project)),
    )
    await loop.submit(UserTurn(content="read"))
    await loop.wait_turn()

    path = await store.load_path(sid)
    joined = "\n".join(_all_text(n.message) for n in path)
    assert "Root rule." not in joined  # covered by the system-prompt stack


# ─── 3. Skill builtin (progressive loading) ──────────────────────────────────


async def test_skill_tool_loads_body_on_demand(tmp_path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship it\n---\nStep 1: build. Step 2: ship."
    )
    ctx = ToolCtx(cwd=str(tmp_path))
    outcome = await skill_handler({"name": "deploy"}, ctx)
    assert not outcome.is_error
    assert "Step 1: build. Step 2: ship." in outcome.text
    assert "# Skill: deploy" in outcome.text

    missing = await skill_handler({"name": "nope"}, ctx)
    assert missing.is_error and "deploy" in missing.text  # lists what exists


def test_skill_index_advertised_in_system_prompt(tmp_path, monkeypatch) -> None:
    from omni.cli.config import resolved_system
    from omni.config.model import HarnessConfig

    skill_dir = tmp_path / ".claude" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship it\n---\nBODY NOT IN PREFIX"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))  # isolate globals
    config = HarnessConfig.from_dict({"cwd": str(tmp_path)})
    system = resolved_system(config) or ""
    assert "- deploy: Ship it" in system
    assert "BODY NOT IN PREFIX" not in system  # progressive disclosure


# ─── 4. Slash-command execution ──────────────────────────────────────────────


async def test_user_slash_command_runs_as_turn(tmp_path) -> None:
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "greet.md").write_text("---\ndescription: greet\n---\nSay hello to $ARGUMENTS!")

    provider = ScriptedProvider([_text("Hello, World!")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        ctx=ToolCtx(cwd=str(tmp_path)),
    )
    out = io.StringIO()
    repl = Repl(loop, sid, input_fn=lambda _: "", out=out)
    assert await repl.dispatch_slash("/greet World") is True
    await loop.wait_turn()

    # The command body (with $ARGUMENTS substituted) became the user turn.
    path = await store.load_path(sid)
    assert path[0].message.role == "user"
    assert path[0].message.text() == "Say hello to World!"
    assert "Hello, World!" in out.getvalue()


async def test_unknown_slash_command_reports(tmp_path) -> None:
    provider = ScriptedProvider([_text("x")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid,
                      ctx=ToolCtx(cwd=str(tmp_path)))
    out = io.StringIO()
    repl = Repl(loop, sid, input_fn=lambda _: "", out=out)
    assert await repl.dispatch_slash("/nope") is True
    assert "unknown command" in out.getvalue()
