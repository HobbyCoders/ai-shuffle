"""The Antigravity (ACP) leg defers app and external MCP tools through CallTool.

ACP has no deferral of its own, so before this the bundled bridge listed every
app tool with its schema (~98 tools, ~26.5k tokens per request on the code
lane) and the session's external MCP servers went to the agent whole. Now the
bridge lists ``ACP_LOADED_TOOLS`` plus fluid's ``LoadTools`` (the rest by name)
and ``CallTool`` (runs one by its real name), and external servers connect
host-side so they ride the same proxy.

The end-to-end case drives the real ``_connect_antigravity`` with the scripted
agent (fluid/tests/fixtures/fake_acp_agent.py) behind the binary override and
a real stdio MCP server (fluid/tests/fixtures/fake_mcp_server.py).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.fluid_tool_bridge import ACP_LOADED_TOOLS, BRIDGE_DEFER_ENV
from app.core.provider.types import SessionOptions
from tests.core.test_antigravity_provider import _create, client, fake_agy  # noqa: F401
from tests.core.test_delegated_bridge_tiers import _isolated, _servers  # noqa: F401
from tests.core.test_delegated_leg import SEARCH_SURFACES

ROOT = Path(__file__).resolve().parents[2]
FAKE_MCP = str(ROOT / "fluid" / "tests" / "fixtures" / "fake_mcp_server.py")


def _options(tmp_path, lane="code", **kw):
    options = SessionOptions(model="m", cwd=str(tmp_path), permission_mode="default",
                             mcp_servers=_servers(str(tmp_path)), **kw)
    options.extra["session_mode"] = lane
    if lane == "chat":
        from app.core.session_modes import CHAT_MODE_TOOL_SURFACE
        options.tool_allowlist = list(CHAT_MODE_TOOL_SURFACE)
    return options


async def _listing(tmp_path, options, call_through):
    from fluid.tools.mcp.bundled_server import _answer
    from fluid.tools.registry import ToolCtx

    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    blocked = ha._delegated_disallowed(options, registry)
    surface = ha._delegated_allowed(options)
    reply = await _answer(
        registry, ToolCtx(cwd=str(tmp_path)), {"op": "list"},
        tool_filter=lambda n: ha._bridged_tool_permitted(n, surface, blocked),
        call_through=call_through,
    )
    return {tool["name"]: tool for tool in reply["tools"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("lane, core", [
    ("code", ["mcp__builtins__Task", "mcp__builtins__Monitor"]),
    ("chat", ["mcp__builtins__Task", "mcp__attachments__Read"]),
])
async def test_the_bridge_lists_the_core_and_announces_the_rest(tmp_path, lane, core):
    options = _options(tmp_path, lane)
    deferral = ha._acp_bridge_deferral(options)
    assert deferral is not None
    eager = await _listing(tmp_path, options, None)
    tools = await _listing(tmp_path, options, deferral)
    assert list(tools) == [*core, "LoadTools", "CallTool"]
    assert set(core) <= ACP_LOADED_TOOLS
    catalog = tools["LoadTools"]["description"]
    for name in eager:
        if name not in ACP_LOADED_TOOLS:
            _server, _, tool = name.removeprefix("mcp__").partition("__")
            # Every other served tool is announced by name under its server.
            assert tool in catalog if tool else name in catalog, name
    assert "app-config (mcp__app-config__<tool>)" in catalog
    assert len(json.dumps(list(tools.values()))) < len(json.dumps(list(eager.values()))) / 4
    if lane == "chat":
        # The chat ceiling still applies: a code-lane-only tool is not announced.
        code_only = set(await _listing(tmp_path, _options(tmp_path), None)) - set(eager)
        assert code_only
        for name in code_only:
            assert f" {name.rsplit('__', 1)[-1]}," not in catalog.replace("\n", ",") + ","


def test_no_deferral_when_toolsearch_is_off_the_surface_or_switched_off(tmp_path, monkeypatch):
    assert ha._acp_bridge_deferral(_options(tmp_path)) is not None
    # A deferred tool must never become unreachable.
    assert ha._acp_bridge_deferral(_options(tmp_path, disallowed_tools=["ToolSearch"])) is None
    assert ha._acp_bridge_deferral(_options(tmp_path, tool_allowlist=["Read", "mcp__builtins__Task"])) is None
    proactive = _options(tmp_path)
    proactive.extra["assistant_proactive"] = {"kind": "cron"}
    assert ha._acp_bridge_deferral(proactive) is None
    monkeypatch.setenv(BRIDGE_DEFER_ENV, "0")
    assert ha._acp_bridge_deferral(_options(tmp_path)) is None


async def _run_turn(engine, text, timeout=60, answer=None):
    """One turn; ``answer`` replies to every question card the turn raises."""
    from fluid.events.model import (
        ErrorEvent,
        Interrupted,
        QuestionRequest,
        TextDelta,
        TurnComplete,
    )
    from fluid.events.ops import RespondQuestion, UserTurn

    events = []
    await engine._ensure_session()

    async def pump():
        async for event in engine.events():
            events.append(event)
            if isinstance(event, QuestionRequest) and answer is not None:
                await engine.submit(RespondQuestion(request_id=event.request_id, answer=answer))
            if isinstance(event, (TurnComplete, ErrorEvent, Interrupted)):
                return

    task = asyncio.create_task(pump())
    await asyncio.sleep(0)
    await engine.submit(UserTurn(content=text))
    await asyncio.wait_for(task, timeout)
    return "".join(e.text for e in events if isinstance(e, TextDelta)), events


async def _connected(tmp_path, client, monkeypatch, *, defer=True, mode="bypassPermissions",
                     register=None, **restrictions):  # noqa: F811
    """``register(registry)`` adds tools to the session registry before the
    bridge is built, as the app's own in-proc servers are."""
    from fluid.store.base import MemoryStore

    if not defer:
        monkeypatch.setenv(BRIDGE_DEFER_ENV, "0")
    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    servers = _servers(str(tmp_path))
    servers["weather"] = {"type": "stdio", "command": sys.executable, "args": ["-I", FAKE_MCP],
                          "env": {"FLUID_FAKE_INSTRUCTIONS": "Weather server rules: cite the city."}}
    options = SessionOptions(model="gemini-3.1-pro", cwd=str(tmp_path), permission_mode=mode,
                             mcp_servers=servers, extra={"provider_id": row["id"], "app_session_id": "s-acp"},
                             **restrictions)
    adapter = ha.HarnessAdapter()
    adapter._options = options
    # connect()'s Antigravity steps, in order.
    adapter._provider_kind = ha._ANTIGRAVITY_KIND
    adapter._acp_deferred = ha._acp_bridge_deferral(options)
    adapter._registry = await adapter._build_registry(
        options, native=False, host_mcp=adapter._acp_deferred is not None)
    if register is not None:
        register(adapter._registry)
    await adapter._connect_antigravity(options, MemoryStore(), "fs-acp", None, "Be brief.", False)
    return adapter


@pytest.mark.asyncio
async def test_external_servers_connect_host_side_and_run_through_call_tool(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch)
    engine = adapter._engine
    try:
        assert adapter._host_mcp_servers == {"weather"}
        # The agent is not handed the server; its tools are announced instead.
        assert "weather" not in engine.mcp_servers
        assert engine.bridged_tools() == ["mcp__builtins__Task", "mcp__builtins__Monitor",
                                          "LoadTools", "CallTool"]
        assert "mcp__weather__get_weather" in engine.deferred_tools()
        # Its own instructions still reach the agent, with the proxy note.
        assert "Weather server rules: cite the city." in engine.system_append
        assert "mcp__fluid_bundled__CallTool" in engine.system_append
        text, events = await _run_turn(
            engine, 'mcp CallTool {"name": "mcp__weather__get_weather", "input": {"city": "Oslo"}}')
        assert "Oslo" in text, text
        from fluid.events.model import ToolUse
        assert [e.name for e in events if isinstance(e, ToolUse)] == [
            "mcp__fluid_bundled__mcp__weather__get_weather"]
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_the_append_carries_acp_app_guidance_once(
        tmp_path, client, monkeypatch):  # noqa: F811
    """ACP keeps the agent's prompt; only app rules for its actual surface ride the append."""
    from fluid.context.steering import NOTICE_OPEN, STEER_OPEN

    adapter = await _connected(tmp_path, client, monkeypatch)
    engine = adapter._engine
    try:
        append = engine.system_append
        registry = adapter._registry
        options = adapter._options
        ours = ha._antigravity_guidance_blocks(options, registry)
        claude = "\n\n".join(ha._app_guidance_blocks(options, leg="claude", registry=registry))
        assert STEER_OPEN in claude
        assert STEER_OPEN not in append
        assert NOTICE_OPEN in append
        for block in (*ours, ha.PEER_MESSAGE_BLOCK):
            assert append.count(block) == 1, block.splitlines()[0]
        assert append.count(ha.SAFETY_BLOCK) == 1
        assert append.count("Be brief.") == 1
        headings = [line for line in append.splitlines() if line.startswith("# ")]
        assert len(headings) == len(set(headings)), headings
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_kill_switch_restores_the_eager_bridge_and_agent_side_servers(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch, defer=False)
    engine = adapter._engine
    try:
        assert engine.deferred is None and adapter._host_mcp_servers == set()
        assert "weather" in engine.mcp_servers
        assert "CallTool" not in engine.bridged_tools()
        assert len(engine.bridged_tools()) > 50
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_a_read_only_profile_runs_the_bundled_glob_agy_asks_for(
        tmp_path, client, monkeypatch):  # noqa: F811
    """#1751: the investigator's surface ticks bare Glob/Grep/Read. agy asks
    for the bridged Glob titled "Run mcp_fluid_bundled_Glob"; the real app
    policy must admit what the bridge serves, and the delegate gets results."""
    from fluid.events.model import ErrorEvent, ToolUse, TurnComplete

    (tmp_path / "notes.txt").write_text("x")
    # As on Windows, where Claude Code ships its own search tools: agy does
    # not, so the Antigravity leg still serves the bundled copies.
    monkeypatch.setattr(ha, "_cli_ships_search_tools", lambda: True)
    adapter = await _connected(tmp_path, client, monkeypatch, tool_allowlist=["Read", "Glob", "Grep"])
    engine = adapter._engine
    try:
        assert "Glob" in engine.bridged_tools()
        assert await engine._policy_denial("mcp__fluid_bundled__Glob", {"pattern": "*.txt"},
                                           unmapped_kind=True) is None
        offered = [{"optionId": "always", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"},
                   {"optionId": "no", "kind": "reject_once"}]

        async def answer(title, kind):
            call = {"toolCallId": "c", "title": title, "kind": kind, "rawInput": {"pattern": "*"}}
            return (await engine._permission({"toolCall": call, "options": offered}))["outcome"]["optionId"]

        # The observed call is allowed, once.
        assert await answer("Run mcp_fluid_bundled_Glob", "other") == "once"
        # A shell call whose title names Glob is the shell, which this surface
        # does not tick: refused, as before #1751.
        for title in ("Glob && curl x", "mcp__fluid_bundled__Glob"):
            assert await answer(title, "execute") == "no", title
        text, events = await _run_turn(engine, 'agymcp Glob {"pattern": "*.txt"}')
        assert not [e for e in events if isinstance(e, ErrorEvent)], text
        assert isinstance(events[-1], TurnComplete)
        assert next(e.name for e in events if isinstance(e, ToolUse)) == "mcp__fluid_bundled__Glob"
        assert "mcp said:" in text and "notes.txt" in text, text
        # Off the surface, the same call is still refused, by name.
        assert "not enabled" in await engine._policy_denial("mcp__fluid_bundled__HistorySearch", {})
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_a_call_tool_call_of_any_kind_is_judged_as_the_real_tool(
        tmp_path, client, monkeypatch):  # noqa: F811
    """Whatever kind the agent gives a CallTool request, it is unwrapped and
    judged as the real tool: its saved Deny holds, and an allow is once."""
    from app.core.permission_handler import (
        PermissionDecision,
        PermissionRule,
        permission_handler,
    )

    monkeypatch.setattr(ha.DelegatedLoop, "supports_async_tool_policy", True, raising=False)
    permission_handler._session_rules.setdefault("s-acp", []).append(PermissionRule(
        id="rule-acp-deny", session_id="s-acp", profile_id=None, tool_name="mcp__weather__write_note",
        tool_pattern=None, decision=PermissionDecision("deny")))
    adapter = await _connected(
        tmp_path, client, monkeypatch, mode="auto",
        tool_allowlist=["ToolSearch", "mcp__weather__get_weather", "mcp__weather__write_note"])
    engine = adapter._engine
    # The loop answers without an owner card; the host still reads saved rules.
    engine.permission_mode = "bypassPermissions"
    offered = [{"optionId": "always", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"},
               {"optionId": "no", "kind": "reject_once"}]
    try:
        for kind in ("other", "fetch", "execute"):
            for tool, args, want in (("mcp__weather__get_weather", {"city": "Oslo"}, "once"),
                                     ("mcp__weather__write_note", {"text": "x"}, "no")):
                call = {"toolCallId": "c", "title": "mcp__fluid_bundled__CallTool", "kind": kind,
                        "rawInput": {"name": tool, "input": args}}
                answer = await engine._permission({"toolCall": call, "options": offered})
                assert answer["outcome"]["optionId"] == want, (kind, tool)
    finally:
        permission_handler.clear_session_rules("s-acp")
        await engine.aclose()
        await adapter._registry.aclose()


def _call(tool, ask=False, **args):
    """The fake agent's CallTool turn; ``ask`` sends the permission request
    a real agent sends before an MCP call."""
    return ("mcpask" if ask else "mcp") + " CallTool " + json.dumps({"name": tool, "input": args})


@pytest.mark.asyncio
@pytest.mark.parametrize("restrictions, refused", [
    # The tool is on the profile's list in the spelling the owner ticks.
    ({"tool_allowlist": ["ToolSearch", "mcp__weather__get_weather"]}, None),
    ({"tool_allowlist": ["ToolSearch", "mcp__builtins__Task"]}, "not enabled"),
    ({"disallowed_tools": ["mcp__weather__*"]}, "disabled"),
    ({"disallowed_tools": ["mcp__fluid_bundled__mcp__weather__get_weather"]}, "disabled"),
], ids=["allowlisted", "not-allowlisted", "blocked", "blocked-bundled-spelling"])
async def test_the_session_policy_judges_call_tool_by_the_real_tool(
        tmp_path, client, monkeypatch, restrictions, refused):  # noqa: F811
    """The real app policy (``_delegated_tool_policy`` with saved rules) on
    the AcpLoop permission path, in bypass mode where nothing else asks."""
    adapter = await _connected(tmp_path, client, monkeypatch, **restrictions)
    engine = adapter._engine
    try:
        policy = await engine._policy_denial("mcp__fluid_bundled__mcp__weather__get_weather", {"city": "Oslo"})
        text, _events = await _run_turn(engine, _call("mcp__weather__get_weather", ask=True, city="Oslo"))
        if refused is None:
            assert policy is None and "Oslo: sunny" in text, text
        else:
            assert refused in (policy or ""), policy
            # Withheld by the bridge filter too: the card stays on the proxy
            # and the call is refused by the policy or the bridge, never run.
            assert "Oslo: sunny" not in text, text
            assert "mcp denied" in text or "tool not available in this session" in text, text
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


def _register_asking_tool(registry):
    """``Colour`` pauses on input_required and finishes only with the answer
    it is re-run with, so a lost answer cannot pass."""
    from fluid.tools.registry import InputRequired, ToolCtx, ToolOutcome, ToolSpec

    async def run(args: dict, ctx: ToolCtx) -> ToolOutcome:
        answers = args.get("__input_responses__")
        if answers is not None:
            return ToolOutcome(text=f"painted {answers['colour']}")
        return ToolOutcome(text="", input_required=InputRequired(
            server="", tool="Colour", request_state="s1",
            requests={"colour": {"method": "elicitation/create", "message": "Which colour?",
                                 "schema": {"type": "string"}}},
        ))

    registry.register_builtin(ToolSpec(name="Colour", description="Paint in a colour.",
                                       input_schema={"type": "object", "properties": {}}), run)


async def _turn_through_app_questions(adapter, text, can_use_tool, timeout=60):
    """One turn, its question cards routed the way ``receive_events`` routes
    them: ``_translate`` then ``_bridge_question`` into ``can_use_tool``."""
    from fluid.events.model import (
        ErrorEvent,
        Interrupted,
        QuestionRequest,
        TextDelta,
        TurnComplete,
    )
    from fluid.events.ops import UserTurn

    engine = adapter._engine
    adapter._can_use_tool = can_use_tool
    events = []
    await engine._ensure_session()

    async def pump():
        async for event in engine.events():
            events.append(event)
            if isinstance(event, QuestionRequest):
                for translated in adapter._translate(event):
                    asyncio.ensure_future(adapter._bridge_question(translated))
            if isinstance(event, (TurnComplete, ErrorEvent, Interrupted)):
                return

    task = asyncio.create_task(pump())
    await asyncio.sleep(0)
    await engine.submit(UserTurn(content=text))
    await asyncio.wait_for(task, timeout)
    return "".join(e.text for e in events if isinstance(e, TextDelta)), events


class PermissionResultAllow:
    """The adapter recognises an allow by its class name."""

    def __init__(self, updated_input):
        self.updated_input = updated_input


def _app_question_answers(answer):
    """The approver's contract (query_engine.can_use_tool_callback): a
    question tool gets the owner's answer back under ``FLUID_ANSWER_KEY``; any
    other name is a permission card, whose approval carries no answer."""
    from app.core.user_question_handler import (
        FLUID_ANSWER_KEY,
        HOST_USER_ANSWERED_KEY,
        QUESTION_TOOLS,
        questions_from_tool_input,
    )

    seen = []

    async def can_use_tool(name, tool_input, ctx):
        seen.append((name, questions_from_tool_input(tool_input)))
        if name in QUESTION_TOOLS:
            return PermissionResultAllow(updated_input={**tool_input, FLUID_ANSWER_KEY: answer,
                                                        HOST_USER_ANSWERED_KEY: True})
        return PermissionResultAllow(updated_input=tool_input)

    return can_use_tool, seen


@pytest.mark.asyncio
async def test_a_bridged_tool_that_asks_gets_the_owners_answer_through_the_app(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch, register=_register_asking_tool)
    engine = adapter._engine
    try:
        from fluid.events.model import QuestionAnswered

        assert "Colour" in engine.deferred_tools()
        can_use_tool, seen = _app_question_answers("teal")
        text, events = await _turn_through_app_questions(
            adapter, 'mcp CallTool {"name": "Colour", "input": {}}', can_use_tool)
        # The card reaches the app as a question, naming the tool that asks.
        [(name, questions)] = seen
        assert name == "AskUser"
        assert questions and "Colour asks: Which colour?" in json.dumps(questions)
        answered = [e for e in events if isinstance(e, QuestionAnswered)]
        assert [(e.answer, e.user_answered) for e in answered] == [("teal", True)]
        assert "painted teal" in text, text
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_an_external_tool_that_asks_the_owner_finishes_through_call_tool(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch)
    engine = adapter._engine
    try:
        can_use_tool, seen = _app_question_answers("yes")
        text, _events = await _turn_through_app_questions(
            adapter, _call("mcp__weather__need_input"), can_use_tool)
        assert seen and seen[0][0] == "AskUser"
        assert "confirmed" in text, text
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_a_saved_deny_on_the_real_tool_refuses_its_call_tool_call(
        tmp_path, client, monkeypatch):  # noqa: F811
    """Saved rules key on the registry name the card shows; a CallTool call
    reaches the policy in the bridge's spelling and must still match."""
    from app.core.permission_handler import (
        PermissionDecision,
        PermissionRule,
        permission_handler,
    )
    from app.core.provider.delegated_saved_rules import SAVED_RULE_DENIAL

    monkeypatch.setattr(ha.DelegatedLoop, "supports_async_tool_policy", True, raising=False)
    permission_handler._session_rules.setdefault("s-acp", []).append(PermissionRule(
        id="rule-acp-deny", session_id="s-acp", profile_id=None, tool_name="mcp__weather__write_note",
        tool_pattern=None, decision=PermissionDecision("deny")))
    adapter = await _connected(
        tmp_path, client, monkeypatch, mode="auto",
        tool_allowlist=["ToolSearch", "mcp__weather__get_weather", "mcp__weather__write_note"])
    engine = adapter._engine
    try:
        bundled = "mcp__fluid_bundled__mcp__weather__write_note"
        assert adapter._permission_tool_name(bundled) == "mcp__weather__write_note"
        # Names the bridge does not resolve stay as they are.
        for name in ("mcp__fluid_bundled__CallTool", "mcp__fluid_bundled__mcp__weather__nope", "read_file"):
            assert adapter._permission_tool_name(name) == name
        assert await engine._policy_denial(bundled, {"text": "x"}) == SAVED_RULE_DENIAL
        assert await engine._policy_denial("mcp__fluid_bundled__mcp__weather__get_weather", {"city": "Oslo"}) is None
        text, _events = await _run_turn(engine, _call("mcp__weather__write_note", ask=True, text="x"))
        assert "mcp denied" in text and "note saved" not in text, text
        # A Deny saved on the bridge's spelling (before names resolved) holds.
        permission_handler._session_rules["s-acp"].append(PermissionRule(
            id="rule-acp-old", session_id="s-acp", profile_id=None,
            tool_name="mcp__fluid_bundled__mcp__weather__get_weather", tool_pattern=None,
            decision=PermissionDecision("deny")))
        assert await engine._policy_denial(
            "mcp__fluid_bundled__mcp__weather__get_weather", {"city": "Oslo"}) == SAVED_RULE_DENIAL
    finally:
        permission_handler.clear_session_rules("s-acp")
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False], ids=["native", "antigravity"])
async def test_host_connected_stdio_servers_start_in_the_session_folder(tmp_path, native):
    """A relative path in a server's args means the session's file, as when
    the agent or the claude CLI spawns it: here the server's script itself."""
    fixtures = Path(FAKE_MCP).parent
    servers = _servers(str(tmp_path))
    relative = {"command": sys.executable, "args": ["-I", Path(FAKE_MCP).name]}
    servers["rel"] = relative
    options = SessionOptions(model="m", cwd=str(fixtures), permission_mode="default", mcp_servers=servers)
    adapter = ha.HarnessAdapter()
    registry = await adapter._build_registry(options, native=native, host_mcp=not native)
    try:
        assert "rel" in adapter._host_mcp_servers
        from fluid.tools.registry import ToolCtx
        out = await registry.dispatch("mcp__rel__get_weather", {"city": "Rome"}, ToolCtx())
        assert "Rome: sunny" in out.text
        # The delegated leg serializes the original map: it is untouched.
        assert "cwd" not in relative
    finally:
        await registry.aclose()


def test_session_cwd_defaults_only_a_stdio_server_without_one(tmp_path):
    stdio = {"command": "srv"}
    assert ha._with_session_cwd(stdio, str(tmp_path)) == {"command": "srv", "cwd": str(tmp_path)}
    assert stdio == {"command": "srv"}
    explicit = {"command": "srv", "cwd": "/elsewhere"}
    assert ha._with_session_cwd(explicit, str(tmp_path)) is explicit
    remote = {"transport": "streamable_http", "url": "https://example.test/mcp"}
    assert ha._with_session_cwd(remote, str(tmp_path)) is remote
    assert ha._with_session_cwd(stdio, None) is stdio
    assert ha._with_session_cwd(stdio, str(tmp_path / "missing")) is stdio


def test_the_policy_reads_a_bridged_server_tool_in_either_spelling(tmp_path):
    bundled = "mcp__fluid_bundled__mcp__weather__get_weather"
    for blocked in (["mcp__weather__*"], ["mcp__weather__get_weather"], [bundled]):
        assert "disabled" in ha._delegated_tool_policy(_options(tmp_path), None, blocked)(bundled, {})
    allow = ha._delegated_tool_policy(_options(tmp_path, tool_allowlist=["mcp__weather__get_weather"]), None, [])
    assert allow(bundled, {}) is None
    assert "not enabled" in allow("mcp__fluid_bundled__mcp__weather__write_note", {})
    # A bare name on the list is the CLI's own tool, not the bridged app copy.
    assert ha._delegated_tool_policy(_options(tmp_path), None, ["WebFetch"])("mcp__fluid_bundled__WebFetch", {}) is None


def test_only_a_tool_the_acp_bridge_serves_passes_by_its_bare_name(tmp_path):
    """#1751: the ACP leg judges a call to a tool its bridge serves by the
    bare name too, under both lists; the shared Claude Code policy does not,
    and a prefixed name the bridge does not serve gains nothing."""
    glob, read = "mcp__fluid_bundled__Glob", "mcp__fluid_bundled__Read"
    ticked = _options(tmp_path, tool_allowlist=["Read", "Glob"])
    assert "not enabled" in ha._delegated_tool_policy(ticked, None, [])(glob, {})
    acp = ha._delegated_tool_policy(ticked, None, [], bridged=lambda tool: tool == "Glob")
    assert acp(glob, {}, unmapped_kind=True) is None
    # Not for a call of one of the agent's own kinds, nor a caller that does
    # not say (the default is the strict reading).
    assert "not enabled" in acp(glob, {})
    assert "not enabled" in acp(read, {}, unmapped_kind=True)
    blocked = ha._delegated_tool_policy(_options(tmp_path), None, ["Glob"], bridged=lambda tool: True)
    assert "disabled" in blocked(glob, {}, unmapped_kind=True)
    chat = _options(tmp_path, lane="chat")
    chat.tool_allowlist.append("Read")
    chat_policy = ha._delegated_tool_policy(chat, None, [], bridged=lambda tool: False)
    assert "not enabled" in chat_policy(read, {}, unmapped_kind=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("defer", [True, False])
@pytest.mark.parametrize("surface", SEARCH_SURFACES)
async def test_search_guidance_names_only_what_the_acp_bridge_serves(
        tmp_path, client, monkeypatch, surface, defer):  # noqa: F811
    """The Antigravity leg registers the bundled Glob and Grep on every OS, and
    its bridge filter applies the CLI-shaped deny list, so a surface ticking
    ``mcp__fluid_bundled__Glob`` gets no search tool and one ticking ``Glob``
    gets Glob alone. The append must not promise more."""
    from app.core import cli_tool_surface

    cli_tool_surface.reset_cli_tools()
    # Deferral needs ToolSearch on the surface (_acp_bridge_deferral).
    tools = [*surface, "ToolSearch"] if defer else list(surface)
    adapter = await _connected(tmp_path, client, monkeypatch, tool_allowlist=tools)
    engine = adapter._engine
    try:
        assert (engine.deferred is not None) is defer
        bridged, deferred = set(engine.bridged_tools()), set(engine.deferred_tools())
        assert defer or not deferred
        served = {tool for tool in ("Glob", "Grep") if tool in bridged | deferred}
        # Each served search tool sits on the tier deferral puts it on.
        for tool in served:
            assert (tool in bridged) is (not defer or tool in ACP_LOADED_TOOLS), tool
        assert served == ({"Glob"} if "Glob" in surface else set())
        for tool in ("Glob", "Grep"):
            assert (ha._bundled_cli_name(tool) in engine.system_append) is (tool in served)
        assert "Claude Code's dedicated tools" not in engine.system_append
    finally:
        await engine.aclose()
        await adapter._registry.aclose()
        cli_tool_surface.reset_cli_tools()
