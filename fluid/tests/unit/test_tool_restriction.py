"""Ruling CE12 — the enforced tool-restriction seam.

``NativeLoop(allowed_tools=..., disallowed_tools=...)`` is a wall, not advice:
a filtered tool's schema never reaches the provider, it is neither listed in
the deferred catalog nor loadable through ToolSearch, and a call naming it
comes back as an in-context error instead of running. These tests pin all
three enforcement points plus the predicate semantics, the config threading,
and the delegated leg's ``--allowed-tools`` emission.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fluid.config.loader import build_engine, load_config
from fluid.engine.native import NativeLoop
from fluid.events import ToolResult, UserTurn
from fluid.messages import Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.tool_search import handler as tool_search_handler
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps()

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


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


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


def _loop(provider: ScriptedProvider | None = None, **kw: Any) -> NativeLoop:
    """A NativeLoop with the filesystem-discovery toggles off — these tests are
    about the restriction predicate, not about whatever ~/.claude holds."""
    return NativeLoop(
        provider=provider or ScriptedProvider([_text("ok")]),
        store=kw.pop("store", None) or MemoryStore(),
        model="m",
        claude_settings=False, claude_plugins=False, claude_agents=False,
        **kw,
    )


def _deferred_registry() -> ToolRegistry:
    """Builtins plus two known-but-unloaded MCP tools (the ToolSearch tier)."""
    from fluid.tools.registry import _Entry

    reg = ToolRegistry()
    register_builtins(reg)
    for server, tool in (("browser", "click"), ("secrets", "read_vault")):
        spec = ToolSpec(
            name=f"mcp__{server}__{tool}", description=f"{tool} via {server}",
            input_schema={"type": "object", "properties": {}}, source="mcp",
        )
        reg._entries[spec.name] = _Entry(spec=spec, server=server)
    return reg


def _turn_texts(request: ProviderRequest) -> str:
    return "\n".join(
        b.text for m in request.messages for b in m.content if isinstance(b, Text)
    )


# ─── 1. Predicate semantics ──────────────────────────────────────────────────


def test_no_restriction_permits_everything() -> None:
    loop = _loop()
    assert loop.allowed_tools is None and loop.disallowed_tools == []
    for name in ("Read", "Bash", "mcp__anything__at_all"):
        assert loop._tool_permitted(name) is True
    # An unrestricted session still publishes the capability half of the
    # predicate — ToolSearch must not list a tool whose host injection is
    # missing — and that half says yes to every plain builtin.
    assert loop.ctx.extra["tool_filter"]("Read") is True


def test_allowlist_is_exact_or_fnmatch() -> None:
    loop = _loop(allowed_tools=["Read", "mcp__browser__*"])
    assert loop._tool_permitted("Read") is True
    assert loop._tool_permitted("mcp__browser__click") is True
    assert loop._tool_permitted("Write") is False
    assert loop._tool_permitted("ReadFile") is False  # exact, not prefix
    assert loop._tool_permitted("mcp__secrets__read_vault") is False
    assert loop.ctx.extra["tool_filter"]("Read") is True


def test_deny_beats_allow() -> None:
    loop = _loop(allowed_tools=["*"], disallowed_tools=["Bash", "mcp__secrets__*"])
    assert loop._tool_permitted("Read") is True
    assert loop._tool_permitted("Bash") is False
    assert loop._tool_permitted("mcp__secrets__read_vault") is False

    # Deny applies without an allowlist too.
    only_deny = _loop(disallowed_tools=["Bash"])
    assert only_deny._tool_permitted("Read") is True
    assert only_deny._tool_permitted("Bash") is False


def test_empty_allowlist_permits_nothing() -> None:
    # The list is the list: an empty allowlist is a real answer, not "unset",
    # and nothing is silently re-added.
    loop = _loop(allowed_tools=[])
    assert loop._tool_permitted("Read") is False
    assert loop._tool_permitted("ToolSearch") is False


def test_restriction_is_session_static() -> None:
    allowed = ["Read"]
    loop = _loop(allowed_tools=allowed)
    allowed.append("Bash")  # caller mutates its own list afterwards
    assert loop._tool_permitted("Bash") is False


# ─── 2. Enforcement point 1: request schemas ─────────────────────────────────


async def test_filtered_schemas_never_reach_the_provider() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([_text("hi")])
    loop = _loop(
        provider, registry=registry, allowed_tools=["Read", "Glob"],
        disallowed_tools=["Glob"],
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    assert [t["name"] for t in loop._tools_api()] == ["Read"]
    assert [t["name"] for t in provider.requests[0].tools] == ["Read"]
    await loop.aclose()


async def test_unrestricted_session_sends_every_loaded_schema() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([_text("hi")])
    loop = _loop(provider, registry=registry)
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    # Every loaded schema goes out EXCEPT tools whose host capability is not
    # wired (requires_extra): with no dispatcher/spawner/store-dir injected,
    # Task, Monitor, and SessionSearch must be invisible rather than
    # advertised-and-erroring.
    sent = {t["name"] for t in provider.requests[0].tools}
    expected = {
        s.name
        for s in registry.list_tools(loaded_only=True)
        if all(key in loop.ctx.extra for key in s.requires_extra)
    }
    assert sent == expected
    assert {"Read", "Write", "Bash"} <= sent
    assert {"Task", "Monitor", "SessionSearch"}.isdisjoint(sent)
    await loop.aclose()


async def test_capability_wired_tools_are_sent() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([_text("hi")])
    loop = _loop(provider, registry=registry)
    loop.ctx.extra["task_dispatch"] = lambda input, ctx: "ok"
    loop.ctx.extra["monitor_spawn"] = lambda input, ctx: "ok"
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    sent = {t["name"] for t in provider.requests[0].tools}
    assert {"Task", "Monitor"} <= sent
    await loop.aclose()


# ─── 3. Enforcement point 2: deferred catalog + ToolSearch ───────────────────


async def test_deferred_catalog_omits_filtered_tools() -> None:
    registry = _deferred_registry()
    provider = ScriptedProvider([_text("hi")])
    loop = _loop(provider, registry=registry, disallowed_tools=["mcp__secrets__*"])
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    announced = _turn_texts(provider.requests[0])
    assert "mcp__browser__click" in announced
    assert "mcp__secrets__read_vault" not in announced
    await loop.aclose()


async def test_tool_search_will_not_load_a_filtered_tool() -> None:
    registry = _deferred_registry()
    loop = _loop(registry=registry, disallowed_tools=["mcp__secrets__*"])

    blocked = await tool_search_handler(
        {"query": "select:mcp__secrets__read_vault"}, loop.ctx
    )
    assert blocked.is_error and "mcp__secrets__read_vault" in blocked.text
    assert registry.get("mcp__secrets__read_vault") is not None  # registry untouched
    assert "mcp__secrets__read_vault" not in {
        s.name for s in registry.list_tools(loaded_only=True)
    }

    # Keyword search cannot surface it either.
    keyword = await tool_search_handler({"query": "read_vault secrets"}, loop.ctx)
    assert "mcp__secrets__read_vault" not in keyword.text

    # A permitted deferred tool still loads normally.
    ok = await tool_search_handler({"query": "select:mcp__browser__click"}, loop.ctx)
    assert not ok.is_error
    assert "mcp__browser__click" in {
        s.name for s in registry.list_tools(loaded_only=True)
    }


async def test_tool_search_unfiltered_when_session_unrestricted() -> None:
    registry = _deferred_registry()
    loop = _loop(registry=registry)
    out = await tool_search_handler(
        {"query": "select:mcp__secrets__read_vault"}, loop.ctx
    )
    assert not out.is_error


# ─── 4. Enforcement point 3: dispatch ────────────────────────────────────────


async def test_dispatch_of_a_filtered_tool_errors_without_running_it() -> None:
    ran: list[dict[str, Any]] = []

    async def danger(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        ran.append(input)
        return ToolOutcome(text="boom")

    registry = ToolRegistry()
    register_builtins(registry)
    registry.register_builtin(
        ToolSpec(name="Danger", description="do not", input_schema={"type": "object"}),
        danger,
    )
    provider = ScriptedProvider([
        _tool_call("t1", "Danger", json.dumps({"x": 1})),
        _text("fine"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(
        provider, store=store, session_id=sid, registry=registry,
        permissions=PermissionEngine("never_ask"), allowed_tools=["Read"],
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    assert ran == []  # never dispatched
    result = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult))
    assert result.name == "Danger" and result.is_error
    assert "not available in this session" in result.output
    # The turn survives the hallucinated call and reaches the next model call.
    assert provider.calls == 2
    await loop.aclose()


# ─── 5. Config + delegated leg ───────────────────────────────────────────────


def test_config_tools_section_threads_into_the_native_loop(tmp_path) -> None:
    config = load_config(
        cwd=tmp_path,
        overrides={"tools": {"allowed": ["Read", "mcp__browser__*"],
                             "disallowed": ["Bash"]}},
        include_system=False, include_user=False,
    )
    assert config.tools.allowed == ("Read", "mcp__browser__*")
    assert config.tools.disallowed == ("Bash",)

    loop = build_engine(
        config, MemoryStore(), provider=ScriptedProvider([_text("hi")]),
    )
    assert loop.allowed_tools == ["Read", "mcp__browser__*"]
    assert loop.disallowed_tools == ["Bash"]
    assert loop._tool_permitted("mcp__browser__click") is True
    assert loop._tool_permitted("Bash") is False


def test_config_without_tools_section_is_unrestricted(tmp_path) -> None:
    config = load_config(cwd=tmp_path, include_system=False, include_user=False)
    assert config.tools.allowed is None
    loop = build_engine(
        config, MemoryStore(), provider=ScriptedProvider([_text("hi")]),
    )
    assert loop.allowed_tools is None and loop._tool_permitted("anything") is True


def test_delegated_argv_emits_allowed_tools() -> None:
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop(
        store=MemoryStore(), model="m",
        allowed_tools=["Read", "Grep"], disallowed_tools=["Bash"],
    )
    argv = loop._build_argv()
    assert argv[argv.index("--allowed-tools") + 1] == "Read,Grep"
    assert argv[argv.index("--disallowed-tools") + 1] == "Bash,EnterPlanMode,ExitPlanMode"

    unrestricted = DelegatedLoop(store=MemoryStore(), model="m")
    unrestricted_argv = unrestricted._build_argv()
    assert "--allowed-tools" not in unrestricted_argv
    assert (
        unrestricted_argv[unrestricted_argv.index("--disallowed-tools") + 1]
        == "EnterPlanMode,ExitPlanMode"
    )
