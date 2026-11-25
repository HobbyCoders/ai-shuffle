"""The four M6 seams, wired: nested-instruction injection, skill progressive
loading (Skill builtin), slash-command execution, /model + permission-mode ops
on NativeLoop."""

import io
import json
from collections.abc import AsyncIterator

from fluid.cli.repl import Repl
from fluid.context.compaction import CompactionConfig
from fluid.context.trim import TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import SetModel, SetPermissionMode, UserTurn
from fluid.messages import ToolResultBlock
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.skill import handler as skill_handler
from fluid.tools.registry import ToolCtx, ToolRegistry


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

    await loop.submit(SetPermissionMode(mode="always_ask"))
    assert perms.approval_policy == "always_ask"

    await loop.submit(SetPermissionMode(mode="bypassPermissions"))
    assert perms.approval_policy == "never_ask"

    # fluid-native policy names pass straight through.
    await loop.submit(SetPermissionMode(mode="on_write"))
    assert perms.approval_policy == "on_write"


# ─── 2. Nested-instruction injection ─────────────────────────────────────────


async def test_nested_instructions_ride_tool_result_once(tmp_path) -> None:
    project = tmp_path / "proj"
    nested = project / "sub"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()  # git root = project boundary
    (nested / "AGENTS.md").write_text("Nested rule: use tabs.")
    (nested / "f.txt").write_text("data")

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool_call("t1", "Read", json.dumps({"file_path": str(nested / "f.txt")})),
        _tool_call("t2", "Read", json.dumps({"file_path": str(nested / "f.txt")})),
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

    # The nested AGENTS.md rides the FIRST tool result only (once per session).
    path = await store.load_path(sid)
    joined = "\n".join(_all_text(n.message) for n in path)
    assert joined.count("Nested rule: use tabs.") == 1
    assert "<system-reminder>" in joined


def _result_texts(path) -> dict[str, str]:
    """Each stored tool result's text, by tool_use_id."""
    return {
        block.tool_use_id: "\n".join(getattr(b, "text", "") for b in block.content)
        for node in path for block in node.message.content
        if isinstance(block, ToolResultBlock)
    }


def _nested_project(tmp_path):
    project = tmp_path / "proj"
    nested = project / "sub"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    (nested / "AGENTS.md").write_text("Nested rule: use tabs.")
    (nested / "f.txt").write_text("data")
    return project, nested


async def _run_nested(project, calls, **loop_kwargs):
    """One user turn making the (tool_use_id, tool, args) calls in order."""
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        *[_tool_call(tuid, name, json.dumps(args)) for tuid, name, args in calls],
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop_kwargs.setdefault("permissions", PermissionEngine("never_ask"))
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, ctx=ToolCtx(cwd=str(project)), **loop_kwargs,
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    return _result_texts(await store.load_path(sid))


async def test_denied_or_failed_calls_never_pull_nested_instructions(tmp_path) -> None:
    """Only an allowed call that succeeded touched the directory; a denied or
    failed one leaves its instructions out, and the next real visit gets them."""
    project, nested = _nested_project(tmp_path)
    results = await _run_nested(project, [
        ("t1", "Grep", {"pattern": "x", "path": str(nested)}),       # denied by rule
        ("t2", "Read", {"file_path": str(nested / "missing.txt")}),  # fails
        ("t3", "Read", {"file_path": str(nested / "f.txt")}),        # succeeds
    ], permissions=PermissionEngine("never_ask", rules=[Rule(tool="Grep", decision="deny")]))
    assert "permission denied" in results["t1"]
    assert "Nested rule" not in results["t1"]
    assert "Nested rule" not in results["t2"]
    assert results["t3"].count("Nested rule: use tabs.") == 1


async def test_an_empty_nested_file_is_delivered_once_it_has_content(tmp_path) -> None:
    """An empty AGENTS.md carries nothing, so visiting it records nothing:
    when the file gains content, the next visit delivers it."""
    project, nested = _nested_project(tmp_path)
    agents = nested / "AGENTS.md"
    agents.write_text("   \n")

    class FillingProvider(ScriptedProvider):
        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
            if self.calls == 1:  # after t1 ran on the empty file
                agents.write_text("Nested rule: use tabs.")
            async for chunk in super().stream(request):
                yield chunk

    provider = FillingProvider([
        _tool_call("t1", "Read", json.dumps({"file_path": str(nested / "f.txt")})),
        _tool_call("t2", "Read", json.dumps({"file_path": str(nested / "f.txt")})),
        _text("done"),
    ])
    registry = ToolRegistry()
    register_builtins(registry)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid, registry=registry,
        ctx=ToolCtx(cwd=str(project)), permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    results = _result_texts(await store.load_path(sid))
    assert "Instructions apply" not in results["t1"]
    assert results["t2"].count("Nested rule: use tabs.") == 1


async def test_nested_instructions_respect_setting_sources(tmp_path) -> None:
    """A session whose setting_sources leave out "project" never loads
    project-scope instructions, nested ones included."""
    project, nested = _nested_project(tmp_path)
    results = await _run_nested(
        project, [("t1", "Read", {"file_path": str(nested / "f.txt")})],
        setting_sources=["user"],
    )
    # The same session with project sources on does load it.
    results_on = await _run_nested(
        project, [("t1", "Read", {"file_path": str(nested / "f.txt")})],
        setting_sources=["user", "project"],
    )
    assert "Nested rule" not in results["t1"]
    assert "Nested rule: use tabs." in results_on["t1"]


async def test_trimmed_nested_instructions_return_on_the_next_visit(tmp_path) -> None:
    """Once trim replaces the result that carried a nested AGENTS.md, the
    rules are out of context: the next visit to that directory brings them
    back. While the carrier is still visible, a revisit does not repeat them."""
    project, nested = _nested_project(tmp_path)
    (nested / "big.txt").write_text("line of data\n" * 400)
    # Every request is over the trigger and only the newest result is kept,
    # so the request after t2 shows t1 as a trim placeholder.
    compaction = CompactionConfig(
        trigger_tokens=1, trigger_window_ratio=0.0,
        trim=TrimConfig(
            trigger_tokens=1, trigger_window_ratio=0.0,
            keep_recent_tool_results=1, min_result_chars=100,
        ),
    )
    results = await _run_nested(project, [
        ("t1", "Read", {"file_path": str(nested / "big.txt")}),
        ("t2", "Read", {"file_path": str(nested / "f.txt")}),  # t1 still shown
        ("t3", "Read", {"file_path": str(nested / "f.txt")}),  # t1 trimmed by now
    ], compaction=compaction)
    assert "Nested rule: use tabs." in results["t1"]
    assert "Nested rule" not in results["t2"]
    assert "Nested rule: use tabs." in results["t3"]


async def test_reading_home_and_fluid_never_reinjects_global_instructions(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    config = home / ".fluid"
    config.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (config / "AGENTS.md").write_text("Global rule must not be reinjected.")
    (config / "AGENTS.local.md").write_text("Home local project rule.")
    (home / "AGENTS.md").write_text("Home root project rule.")
    targets = [home / "plain.txt", config / "plain.txt"]
    for target in targets:
        target.write_text("data")
    project = tmp_path / "outside-home"
    project.mkdir()
    (project / ".git").mkdir()
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        *[_tool_call(f"t{i}", "Read", json.dumps({"file_path": str(target)}))
          for i, target in enumerate(targets)],
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(cwd=str(project)),
    )
    await loop.submit(UserTurn(content="read both"))
    await loop.wait_turn()
    joined = "\n".join(_all_text(n.message) for n in await store.load_path(sid))
    assert "Global rule must not be reinjected." not in joined
    assert joined.count("Home root project rule.") == 1
    assert joined.count("Home local project rule.") == 1


async def test_project_root_instructions_not_reinjected(tmp_path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("Root rule.")  # in the project stack
    (project / "f.txt").write_text("data")

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool_call("t1", "Read", json.dumps({"file_path": str(project / "f.txt")})),
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


async def test_project_root_instructions_not_reinjected_for_relative_path(tmp_path) -> None:
    """A relative tool path resolves against the tool cwd, so the cwd's own
    AGENTS.md (already in the project stack) does not ride the result."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("Root rule.")
    (project / "f.txt").write_text("data")

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool_call("t1", "Read", json.dumps({"file_path": "f.txt"})),
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
    assert "Root rule." not in joined


# ─── 3. Skill builtin (progressive loading) ──────────────────────────────────


async def test_skill_tool_loads_body_on_demand(tmp_path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "deploy"
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
    from fluid.cli.config import resolved_system
    from fluid.config.model import HarnessConfig

    skill_dir = tmp_path / ".agents" / "skills" / "deploy"
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
    cmds = tmp_path / ".agents" / "commands"
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
