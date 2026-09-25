"""``get_context_usage()`` — the Claude-Code-parity context breakdown.

Two legs, one shape: the native loop computes the breakdown from its own
compiled prompt / tool schemas / trimmed history, and the delegated loop proxies
the CLI's answer over the control-frame round-trip. Covered here: the
control_response correlation (success, error subtype, timeout, dead process),
the native category maths (section attribution, deferred exclusion, trimmed
history basis, known/unknown context window), snapshot purity, and the caps bit.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from fluid.context.compiler import (
    SECTION_KINDS,
    PromptInputs,
    PromptSection,
    compile_prompt,
    compile_system_prompt,
)
from fluid.context.trim import TrimConfig, estimate_tokens_of_text
from fluid.context.usage import (
    CUSTOM_AGENTS,
    FREE_SPACE,
    MCP_TOOLS,
    MCP_TOOLS_DEFERRED,
    MEMORY_FILES,
    MESSAGES,
    SKILLS,
    SYSTEM_PROMPT,
    SYSTEM_TOOLS,
    SYSTEM_TOOLS_DEFERRED,
    build_context_usage,
    tool_schema_tokens,
)
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, parse_control_response
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.events.model import Usage, event_from_dict, event_to_dict
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode, new_id
from fluid.tools.registry import ToolCtx, ToolRegistry, ToolSpec

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


def _cmd() -> list[str]:
    return [sys.executable, FAKE]


# ─── Capability bit ──────────────────────────────────────────────────────────


def test_caps_advertise_context_usage() -> None:
    assert NativeLoop.caps.context_usage is True
    assert DelegatedLoop.caps.context_usage is True


def test_native_caps_advertise_code_mode_and_subagents() -> None:
    assert NativeLoop.caps.code_mode is True
    assert NativeLoop.caps.subagents is True


# ─── Compiler: sections without changing the joined string ───────────────────


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agents" / "skills" / "deploy").mkdir(parents=True)
    (proj / ".git").mkdir()
    (proj / "AGENT.md").write_text("Project rule: be terse.", encoding="utf-8")
    (proj / "AGENTS.md").write_text("Agents rule: run tests.", encoding="utf-8")
    (proj / ".agents" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship it\n---\nbody", encoding="utf-8"
    )
    return proj


def _inputs(proj: Path) -> PromptInputs:
    return PromptInputs(
        base="I am the agent.", extra_blocks=["App block."], cwd=proj,
        include_global_instructions=False,
    )


def test_compiled_sections_join_to_the_same_prompt(tmp_path) -> None:
    proj = _project(tmp_path)
    compiled = compile_prompt(_inputs(proj))
    # The joined view is exactly what callers have always received.
    assert compiled.text == compile_system_prompt(_inputs(proj))
    assert compiled.text == "\n\n".join(s.text for s in compiled.sections)


def test_compiled_sections_carry_per_file_attribution(tmp_path) -> None:
    proj = _project(tmp_path)
    compiled = compile_prompt(_inputs(proj))
    kinds = [s.kind for s in compiled.sections]
    assert kinds[0] == "base" and kinds[-1] == "extra"
    memory = compiled.of_kind("memory")
    # One section per instruction file, each naming its own path.
    assert {Path(s.path or "").name for s in memory} == {"AGENT.md", "AGENTS.md"}
    assert all(s.text.startswith("# From: ") for s in memory)
    assert len(compiled.of_kind("skills")) == 1
    assert len(compiled.of_kind("environment")) == 1


# ─── Pure breakdown maths ────────────────────────────────────────────────────


def _sections(tmp_path) -> tuple:
    return compile_prompt(_inputs(_project(tmp_path))).sections


def _by_name(usage: dict) -> dict[str, dict]:
    return {c["name"]: c for c in usage["categories"]}


def test_categories_sum_to_total_and_carry_colors(tmp_path) -> None:
    usage = build_context_usage(
        sections=_sections(tmp_path),
        history=[Message(role="user", content=[Text("x" * 400)])],
        tools_api=[{"name": "Read", "description": "d", "input_schema": {}}],
        model="claude-sonnet-5",
    )
    cats = _by_name(usage)
    assert set(cats) == {SYSTEM_PROMPT, MEMORY_FILES, SKILLS, SYSTEM_TOOLS, MESSAGES}
    assert sum(c["tokens"] for c in cats.values()) == usage["totalTokens"]
    assert all(c["color"].startswith("#") and len(c["color"]) == 7 for c in cats.values())
    assert cats[MESSAGES]["tokens"] == 100  # 400 chars / 4
    assert usage["model"] == "claude-sonnet-5"
    assert usage["isAutoCompactEnabled"] is False


def test_empty_categories_are_omitted() -> None:
    usage = build_context_usage(model="m")
    assert usage["categories"] == []
    assert usage["totalTokens"] == 0


def test_unknown_context_window_leaves_max_and_percentage_none(tmp_path) -> None:
    usage = build_context_usage(sections=_sections(tmp_path), model="m")
    assert usage["maxTokens"] is None
    assert usage["rawMaxTokens"] is None
    assert usage["percentage"] is None
    assert FREE_SPACE not in _by_name(usage)  # nothing to subtract from


def test_known_context_window_adds_free_space_and_percentage() -> None:
    usage = build_context_usage(
        history=[Message(role="user", content=[Text("x" * 4000)])],
        model="m", context_window=10_000,
    )
    assert usage["totalTokens"] == 1000
    assert usage["maxTokens"] == 10_000 and usage["rawMaxTokens"] == 10_000
    assert usage["percentage"] == 10.0
    assert _by_name(usage)[FREE_SPACE]["tokens"] == 9_000


def test_free_space_floors_at_zero() -> None:
    usage = build_context_usage(
        history=[Message(role="user", content=[Text("x" * 40_000)])],
        model="m", context_window=1_000,
    )
    assert _by_name(usage)[FREE_SPACE]["tokens"] == 0
    assert usage["percentage"] == 1000.0  # over budget is reported, not clamped


def test_deferred_tools_are_flagged_split_and_excluded_from_the_total() -> None:
    deferred = [
        {"name": "mcp__x__big", "description": "d" * 400, "input_schema": {}},
        {"name": "Hidden", "description": "h" * 400, "input_schema": {}},
    ]
    with_deferred = build_context_usage(
        deferred_tools_api=deferred, model="m", context_window=10_000,
    )
    cats = _by_name(with_deferred)
    # Split by the naming convention, exactly like Claude Code's two rows.
    assert cats[MCP_TOOLS_DEFERRED]["isDeferred"] is True
    assert cats[SYSTEM_TOOLS_DEFERRED]["isDeferred"] is True
    assert cats[MCP_TOOLS_DEFERRED]["tokens"] > 0
    assert cats[SYSTEM_TOOLS_DEFERRED]["tokens"] > 0
    # Not in the prompt → not in the total, and free space is untouched by it.
    assert with_deferred["totalTokens"] == 0
    assert cats[FREE_SPACE]["tokens"] == 10_000
    assert "isDeferred" not in cats[FREE_SPACE]
    # Deferred MCP tools ride in mcpTools with isLoaded False; deferred
    # builtins get their own detail list.
    assert with_deferred["mcpTools"] == [
        {"name": "mcp__x__big", "serverName": "x",
         "tokens": cats[MCP_TOOLS_DEFERRED]["tokens"], "isLoaded": False},
    ]
    assert with_deferred["deferredBuiltinTools"] == [
        {"name": "Hidden", "tokens": cats[SYSTEM_TOOLS_DEFERRED]["tokens"]},
    ]


def test_loaded_tools_split_into_system_and_mcp_with_per_tool_detail() -> None:
    read = {"name": "Read", "description": "r", "input_schema": {}}
    slack = {"name": "mcp__slack__send", "description": "s" * 100, "input_schema": {}}
    usage = build_context_usage(tools_api=[read, slack], model="m")
    cats = _by_name(usage)
    assert cats[SYSTEM_TOOLS]["tokens"] == tool_schema_tokens(read)
    assert cats[MCP_TOOLS]["tokens"] == tool_schema_tokens(slack)
    # Category rows equal the sum of their own detail lists.
    assert usage["systemTools"] == [
        {"name": "Read", "tokens": tool_schema_tokens(read)},
    ]
    assert usage["mcpTools"] == [
        {"name": "mcp__slack__send", "serverName": "slack",
         "tokens": tool_schema_tokens(slack), "isLoaded": True},
    ]


def test_host_tool_sections_price_as_tools_not_system_prompt() -> None:
    """Code mode: the prompt-carried ``# Tools`` documentation rides as
    ``tools`` sections with ``tool_name`` set. Each prices under the SAME
    category the equivalent JSON schema would — never "System prompt"."""
    read_text = "# Tools\n\n## Read\n`Read(file_path: str)`"
    slack_text = "## Slack\n`slack(text: str)`"
    usage = build_context_usage(
        sections=(
            PromptSection(kind="base", text="base"),
            PromptSection(kind="tools", text=read_text, tool_name="Read"),
            PromptSection(kind="tools", text=slack_text,
                          tool_name="mcp__slack__send"),
        ),
        model="m",
    )
    cats = _by_name(usage)
    assert cats[SYSTEM_TOOLS]["tokens"] == estimate_tokens_of_text(read_text)
    assert cats[MCP_TOOLS]["tokens"] == estimate_tokens_of_text(slack_text)
    assert cats[SYSTEM_PROMPT]["tokens"] == estimate_tokens_of_text("base")
    assert usage["systemTools"] == [
        {"name": "Read", "tokens": estimate_tokens_of_text(read_text)},
    ]
    assert usage["mcpTools"] == [
        {"name": "mcp__slack__send", "serverName": "slack",
         "tokens": estimate_tokens_of_text(slack_text), "isLoaded": True},
    ]


def test_unattributed_tool_sections_fall_back_to_system_prompt() -> None:
    """A ``tools`` section without a ``tool_name`` is prose the breakdown
    cannot attribute — it stays in the accounting, under System prompt, rather
    than being mispriced as a tool or silently dropped."""
    orphan = "# Tools\nsome unattributed catalog text"
    usage = build_context_usage(
        sections=(
            PromptSection(kind="base", text="base"),
            PromptSection(kind="tools", text=orphan),
        ),
        model="m",
    )
    cats = _by_name(usage)
    assert SYSTEM_TOOLS not in cats and MCP_TOOLS not in cats
    assert cats[SYSTEM_PROMPT]["tokens"] == (
        estimate_tokens_of_text("base") + estimate_tokens_of_text(orphan)
    )
    assert usage["systemTools"] == [] and usage["mcpTools"] == []


def test_agents_are_priced_as_custom_agents() -> None:
    """Host-declared agent definitions become the "Custom agents" row, counted
    in the total (they represent text the host injected into the context)."""
    usage = build_context_usage(
        agents=[
            {"agentType": "reviewer", "source": "project", "tokens": 60},
            {"name": "writer", "text": "w" * 80},  # estimated: 20 tokens
        ],
        model="m",
    )
    cats = _by_name(usage)
    assert cats[CUSTOM_AGENTS]["tokens"] == 80
    assert usage["totalTokens"] == 80
    assert usage["agents"] == [
        {"agentType": "reviewer", "source": "project", "tokens": 60},
        {"agentType": "writer", "source": "app", "tokens": 20},
    ]


def test_detail_lists_are_always_present_even_when_empty() -> None:
    """SDK-required keys: an empty source is [], never a missing key."""
    usage = build_context_usage(model="m")
    for key in ("memoryFiles", "systemTools", "mcpTools",
                "deferredBuiltinTools", "agents"):
        assert usage[key] == []


def test_memory_files_carry_the_sdk_per_file_shape(tmp_path) -> None:
    """``memoryFiles`` mirrors the SDK/CLI entry schema exactly:
    ``{path: str, type: str, tokens: int}``, one per instruction file."""
    usage = build_context_usage(sections=_sections(tmp_path), model="m")
    files = usage["memoryFiles"]
    assert len(files) == 2
    for entry in files:
        assert set(entry) == {"path", "type", "tokens"}
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["type"], str)
        assert isinstance(entry["tokens"], int) and entry["tokens"] > 0
    assert {Path(e["path"]).name for e in files} == {"AGENT.md", "AGENTS.md"}
    # The category row is exactly the sum of the per-file detail.
    assert sum(e["tokens"] for e in files) == _by_name(usage)[MEMORY_FILES]["tokens"]


def test_memory_file_type_follows_the_discovery_scope() -> None:
    """Scope maps onto the CLI's type vocabulary: home config is "User",
    anything in the tree is "Project"."""
    usage = build_context_usage(
        sections=(
            PromptSection(kind="memory", text="g" * 40, path="/home/u/.claude/CLAUDE.md",
                          scope="global"),
            PromptSection(kind="memory", text="p" * 40, path="/proj/CLAUDE.md",
                          scope="project"),
        ),
        model="m",
    )
    assert [e["type"] for e in usage["memoryFiles"]] == ["User", "Project"]


def test_memory_files_is_always_present_even_when_empty() -> None:
    """Required key in the CLI's schema — an empty stack is [], never missing."""
    assert build_context_usage(model="m")["memoryFiles"] == []
    assert build_context_usage(system_text="s" * 800, model="m")["memoryFiles"] == []


def test_plain_system_string_prices_as_system_prompt() -> None:
    """An app that never compiled a prompt still gets a breakdown — just with no
    memory/skills split to be had."""
    usage = build_context_usage(system_text="s" * 800, model="m")
    cats = _by_name(usage)
    assert cats[SYSTEM_PROMPT]["tokens"] == 200
    assert MEMORY_FILES not in cats


def test_shared_estimator_is_chars_over_four() -> None:
    assert estimate_tokens_of_text("x" * 400) == 100
    assert estimate_tokens_of_text(None) == 0


# ─── NativeLoop wiring ───────────────────────────────────────────────────────


class _IdleProvider:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request: ProviderRequest):
        for chunk in [
            StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": "ok"}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
        ]:
            yield chunk


def _registry_with_deferred() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="Read", description="Read a file",
                 input_schema={"type": "object"}), _noop,
    )
    registry.register_builtin(
        ToolSpec(name="Hidden", description="h" * 300,
                 input_schema={"type": "object"}), _noop,
    )
    # Unload one so it becomes known-but-deferred (the ToolSearch tier).
    registry._loaded.discard("Hidden")
    return registry


async def _noop(input: dict, ctx) -> str:
    return ""


def _loop(tmp_path, store, *, provider=None, **kw) -> NativeLoop:
    compiled = compile_prompt(_inputs(_project(tmp_path)))
    return NativeLoop(
        provider=provider or _IdleProvider(), store=store, model="claude-sonnet-5",
        system=compiled.text, system_sections=compiled,
        registry=_registry_with_deferred(), **kw,
    )


async def test_native_breakdown_matches_the_compiled_prompt(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, store, session_id=sid, context_window=200_000)
    try:
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    cats = _by_name(usage)
    assert cats[SYSTEM_PROMPT]["tokens"] > 0
    assert cats[MEMORY_FILES]["tokens"] == sum(
        m.tokens for m in loop.memory_file_usage()
    )
    assert {Path(m.path).name for m in loop.memory_file_usage()} == {
        "AGENT.md", "AGENTS.md",
    }
    # Full SDK parity: the per-file detail ships in the response, not just in
    # the helper — same list, same order, in the CLI's entry shape.
    assert usage["memoryFiles"] == [m.to_dict() for m in loop.memory_file_usage()]
    assert all(
        set(e) == {"path", "type", "tokens"} and e["type"] == "Project"
        for e in usage["memoryFiles"]
    )
    assert cats[SYSTEM_TOOLS]["tokens"] == tool_schema_tokens(
        {"name": "Read", "description": "Read a file",
         "input_schema": {"type": "object"}}
    )
    # "Hidden" is a deferred BUILTIN — it lands in the system-tools deferred
    # row (and its detail list), not the MCP one.
    assert cats[SYSTEM_TOOLS_DEFERRED]["isDeferred"] is True
    assert [t["name"] for t in usage["deferredBuiltinTools"]] == ["Hidden"]
    assert MCP_TOOLS_DEFERRED not in cats
    assert usage["maxTokens"] == 200_000 and usage["percentage"] is not None
    assert usage["model"] == "claude-sonnet-5"


async def test_code_mode_still_reports_the_deferred_tier(tmp_path) -> None:
    """Code mode used to report NO deferred tools at all, which hid the whole
    point of deferring them: the schemas are known, they are simply not in the
    prompt (read_tool_docs fetches them instead of ToolSearch)."""
    from fluid.codemode.session import CodeModeSession

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = _registry_with_deferred()
    loop = _loop(tmp_path, store, session_id=sid, context_window=200_000)
    loop.registry = registry
    loop._codemode = CodeModeSession(registry)
    try:
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert [t["name"] for t in usage["deferredBuiltinTools"]] == ["Hidden"]
    assert _by_name(usage)[SYSTEM_TOOLS_DEFERRED]["isDeferred"] is True


async def test_native_messages_use_the_trimmed_history_view(tmp_path) -> None:
    """The Messages category prices what a request would actually send — the
    trimmed view — not the full stored tree."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    parent = None
    for i in range(4):
        call = SessionNode(
            node_id=new_id("node"), parent_id=parent,
            message=Message(role="assistant", content=[
                ToolUseBlock(id=f"t{i}", name="Read", input={}),
            ]),
        )
        await store.append(sid, call)
        node = SessionNode(
            node_id=new_id("node"), parent_id=call.node_id,
            message=Message(role="user", content=[
                ToolResultBlock(tool_use_id=f"t{i}", content=[Text("x" * 40_000)]),
            ]),
        )
        await store.append(sid, node)
        parent = node.node_id

    loop = _loop(
        tmp_path, store, session_id=sid,
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=1,
                        min_result_chars=100),
    )
    try:
        untrimmed = (await loop.get_context_usage())["categories"]
        untrimmed_messages = {c["name"]: c for c in untrimmed}[MESSAGES]["tokens"]
        # Arm the trimmer the way a real turn would, then re-read the snapshot.
        nodes = await store.load_path(sid)
        assert loop._trimmer.force_trim(nodes)
        after = await loop.get_context_usage()
        trimmed = _by_name(after)[MESSAGES]["tokens"]
    finally:
        await loop.aclose()
    assert trimmed < untrimmed_messages
    # The eviction-ladder stats field reports what was reclaimed.
    stats = after["compaction"]
    assert stats["trimmedToolResults"] == 3 and stats["reclaimedTokens"] > 0
    assert stats["digestedSpans"] == 0 and stats["epochStubs"] == 0


async def test_native_snapshot_has_no_side_effects(tmp_path) -> None:
    """Callable between turns: no request, no history mutation, no trim armed."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(
        tmp_path, store, session_id=sid,
        trim=TrimConfig(trigger_tokens=1, keep_recent_tool_results=0,
                        min_result_chars=1),
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        before = await store.load_path(sid)
        first = await loop.get_context_usage()
        second = await loop.get_context_usage()
        after = await store.load_path(sid)
    finally:
        await loop.aclose()
    assert first == second
    assert [n.node_id for n in before] == [n.node_id for n in after]
    # A trigger this low would arm a batch under apply(); view() must not —
    # neither tier 1 trims nor tier 2/3 span promotions.
    assert loop._trimmer._trimmed == set()
    assert loop._compactor._digested == {}


async def test_native_without_a_session_prices_an_empty_history(tmp_path) -> None:
    """No turn submitted yet: report the static context instead of creating a
    session as a side effect of asking."""
    store = MemoryStore()
    loop = _loop(tmp_path, store)
    try:
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert MESSAGES not in _by_name(usage)
    assert loop._session is None


# ─── Delegated: control_response correlation ─────────────────────────────────


def _success_frame(request_id: str, payload: dict) -> dict:
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": request_id, "response": payload,
    }}


def test_parse_control_response_success_and_error() -> None:
    ok = parse_control_response(_success_frame("req_1", {"totalTokens": 7}))
    assert ok is not None and ok.ok and ok.request_id == "req_1"
    assert ok.payload == {"totalTokens": 7}

    bad = parse_control_response({"type": "control_response", "response": {
        "subtype": "error", "request_id": "req_2", "error": "nope",
    }})
    assert bad is not None and bad.ok is False and bad.error == "nope"
    assert bad.payload is None

    # Unattributable frames resolve nothing.
    assert parse_control_response({"type": "control_response", "response": {}}) is None


def test_mapper_surfaces_control_replies_instead_of_dropping_them() -> None:
    """During a turn the frame reader owns stdout, so the mapper is what hands
    a control reply back to the loop — it must no longer drop the frame."""
    mapper = StreamJsonMapper(
        session_id="s1", turn_id="t1", model="m", user_node_id="node_user",
    )
    mapped = mapper.feed(_success_frame("req_9", {"totalTokens": 3}))
    assert mapped.events == [] and mapped.message is None
    assert mapped.control_reply is not None
    assert mapped.control_reply.payload == {"totalTokens": 3}


def _delegated(scenario: str, store=None) -> DelegatedLoop:
    return DelegatedLoop(
        store=store or MemoryStore(), model="claude-fake", claude_cmd=_cmd(),
        env={"FAKE_CLAUDE_SCENARIO": scenario},
    )


async def test_delegated_proxies_the_cli_breakdown_verbatim() -> None:
    from tests.fixtures.fake_claude import CONTEXT_USAGE_PAYLOAD

    loop = _delegated("f")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    # Verbatim: the CLI already speaks the target shape, including values fluid
    # would never report for itself (isAutoCompactEnabled).
    assert usage == CONTEXT_USAGE_PAYLOAD
    assert usage["isAutoCompactEnabled"] is True
    assert not loop._pending_controls  # the pending entry is always cleaned up


async def test_delegated_error_subtype_reports_unavailable() -> None:
    loop = _delegated("g")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        assert await loop.get_context_usage() is None
    finally:
        await loop.aclose()
    assert not loop._pending_controls


async def test_delegated_timeout_reports_unavailable() -> None:
    loop = _delegated("h")  # the fake never answers a control_request
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        answer = await loop._request_control(
            {"subtype": "get_context_usage"}, timeout=0.3,
        )
    finally:
        await loop.aclose()
    assert answer is None
    assert not loop._pending_controls


async def test_delegated_without_a_process_reports_unavailable() -> None:
    loop = _delegated("f")  # never started — no turn submitted
    try:
        assert await loop.get_context_usage() is None
    finally:
        await loop.aclose()


async def test_delegated_control_reply_survives_a_concurrent_turn() -> None:
    """Asked while a turn is streaming, the answer arrives through the turn's
    own frame reader — the two must not fight over stdout."""
    loop = _delegated("f")
    try:
        await loop.submit(UserTurn(content="hi"))  # boots the CLI process
        await loop.wait_turn()
        await loop.submit(UserTurn(content="again"))
        usage, _ = await asyncio.gather(loop.get_context_usage(), loop.wait_turn())
    finally:
        await loop.aclose()
    assert usage is not None and usage["totalTokens"] == 12_000


async def test_delegated_pump_yields_stdout_to_a_turn_that_starts_mid_wait() -> None:
    """A between-turns pump must hand stdout back the moment a turn appears.

    The pump owns ``_stdout_lock`` while it waits for a control reply, and a
    turn cannot even write its user frame until it holds that lock. A pump that
    only stopped on reply/EOF/deadline would therefore park the turn behind a
    snapshot query for the whole ``CONTROL_TIMEOUT`` — and any turn frame it did
    see it would discard as "between-turn". Here the fake parks the reply and
    ships it *inside* the next turn, after that turn's assistant frame: the
    reply can only land if the pump yielded and the turn's own reader took over.
    """
    from tests.fixtures.fake_claude import CONTEXT_USAGE_PAYLOAD

    loop = _delegated("i")
    try:
        await loop.submit(UserTurn(content="hi"))  # boots the CLI process
        await loop.wait_turn()

        asking = asyncio.create_task(loop.get_context_usage())
        await asyncio.sleep(0.1)  # let the pump take stdout
        assert loop._stdout_lock.locked(), "precondition: the pump owns stdout"

        await loop.submit(UserTurn(content="again"))
        # Well inside CONTROL_TIMEOUT: an unyielding pump would stall the turn
        # until its deadline instead of releasing on submit.
        await asyncio.wait_for(loop.wait_turn(), timeout=2)
        usage = await asyncio.wait_for(asking, timeout=2)
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    assert usage == CONTEXT_USAGE_PAYLOAD  # delivered via the turn's reader
    # Both turns are intact — nothing the CLI emitted for turn 2 was discarded.
    # DelegatedSurface rides the init frame, so it appears in turn 1 only.
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "DelegatedSurface",
        "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
    ]
    assert not loop._pending_controls


async def test_delegated_close_settles_a_parked_control_call() -> None:
    """aclose() must not leave a caller waiting out the full timeout."""
    loop = _delegated("h")
    await loop.submit(UserTurn(content="hi"))
    await loop.wait_turn()
    asking = asyncio.create_task(loop.get_context_usage())
    await asyncio.sleep(0.1)
    await loop.aclose()
    assert await asyncio.wait_for(asking, timeout=2) is None


@pytest.mark.parametrize("scenario", ["f", "g"])
async def test_control_round_trip_leaves_the_turn_stream_intact(scenario) -> None:
    """A control call between turns must not eat the next turn's frames."""
    loop = _delegated(scenario)
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        await loop.get_context_usage()
        await loop.submit(UserTurn(content="again"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "DelegatedSurface",
        "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
    ]


# ─── Provider-measured totals ────────────────────────────────────────────────
#
# chars/4 is a stand-in for a number the provider knows exactly. Once a call has
# been made, the breakdown reports the prompt the provider billed for and lets
# "Messages" absorb the difference — the category the estimator misprices.


def _measured(sections, prompt_tokens: int | None, **kw) -> dict:
    return build_context_usage(
        sections=sections,
        history=[Message(role="user", content=[Text("x" * 4_000)])],
        model="m", measured_prompt_tokens=prompt_tokens, **kw,
    )


def test_measured_total_is_the_providers_number(tmp_path) -> None:
    sections = _sections(tmp_path)
    estimated = _measured(sections, None)
    measured = _measured(sections, 38_572)
    assert estimated["measured"] is False
    assert measured["measured"] is True
    assert measured["totalTokens"] == 38_572
    # Only Messages moved; every other row kept its own estimate.
    est_rows, meas_rows = _by_name(estimated), _by_name(measured)
    for name in (SYSTEM_PROMPT, MEMORY_FILES, SKILLS):
        assert meas_rows[name]["tokens"] == est_rows[name]["tokens"]
    others = sum(row["tokens"] for name, row in meas_rows.items() if name != MESSAGES)
    assert meas_rows[MESSAGES]["tokens"] == 38_572 - others
    # And the rows still add up to the total a host would render.
    assert others + meas_rows[MESSAGES]["tokens"] == measured["totalTokens"]


def test_measured_messages_never_go_negative(tmp_path) -> None:
    """A prompt smaller than the static rows (a fully cached prefix reported as
    a handful of tokens) must not push Messages below zero."""
    usage = _measured(_sections(tmp_path), 5)
    assert usage["totalTokens"] == 5
    # Every row scales down to the billed total; nothing goes negative and the
    # rows never add up to more than what was billed.
    rows = [c["tokens"] for c in usage["categories"] if not c.get("isDeferred")]
    assert all(t >= 0 for t in rows)
    assert sum(rows) <= 5 + len(rows)  # rounding slack


def test_measured_zero_is_still_measured(tmp_path) -> None:
    usage = _measured(_sections(tmp_path), 0)
    assert usage["measured"] is True and usage["totalTokens"] == 0


def test_measured_total_drives_free_space_and_percentage(tmp_path) -> None:
    usage = _measured(_sections(tmp_path), 40_000, context_window=200_000)
    assert _by_name(usage)[FREE_SPACE]["tokens"] == 160_000
    assert usage["percentage"] == 20.0


def test_measured_adds_no_other_keys_to_the_sdk_shape(tmp_path) -> None:
    """SDK-shape parity: ``measured`` is the one addition, present either way."""
    sections = _sections(tmp_path)
    estimated = _measured(sections, None)
    measured = _measured(sections, 40_000)
    assert set(measured) == set(estimated)
    assert "measured" in estimated


# ─── NativeLoop: measured totals from the last call ──────────────────────────


class _MeasuredProvider(_IdleProvider):
    """Reports a real prompt: 1,000 fresh + 2,000 cache-read + 500 cache-write."""

    PROMPT_TOKENS = 3_500

    async def stream(self, request: ProviderRequest):
        for chunk in [
            StreamChunk("message_start", {"usage": {
                "input_tokens": 1_000,
                "cache_read_input_tokens": 2_000,
                "cache_creation_input_tokens": 500,
            }}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": "ok"}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 7}, "stop_reason": "end_turn"}),
        ]:
            yield chunk


async def test_native_snapshot_is_an_estimate_before_any_call(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode(
        node_id=new_id("node"), parent_id=None,
        message=Message(role="user", content=[Text("x" * 4_000)]),
    ))
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert usage["measured"] is False
    # The model table supplies a window for claude-sonnet-5, so a Free
    # space row is present; it is not part of the total.
    assert usage["totalTokens"] == sum(
        c["tokens"] for c in usage["categories"]
        if not c.get("isDeferred") and c["name"] != FREE_SPACE
    )


async def test_native_snapshot_prefers_the_last_calls_real_prompt(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider(),
                 context_window=200_000)
    try:
        await loop.submit(UserTurn(content="x" * 4_000))
        await loop.wait_turn()
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert usage["measured"] is True
    assert usage["totalTokens"] == _MeasuredProvider.PROMPT_TOKENS
    assert _by_name(usage)[FREE_SPACE]["tokens"] == 200_000 - 3_500


async def test_native_measured_total_drops_once_its_node_leaves_the_path(tmp_path) -> None:
    """Rewound or forked past the call that produced it, the measurement
    describes a prompt this session no longer sends — fall back to the estimate."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        await loop.submit(UserTurn(content="x" * 4_000))
        await loop.wait_turn()
        node_id, raw = loop._last_call_usage[sid]
        assert any(n.node_id == node_id for n in await store.load_path(sid))
        loop._last_call_usage[sid] = ("node_no_longer_on_the_path", raw)
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert usage["measured"] is False


async def test_native_per_call_usage_event_carries_the_snapshot(tmp_path) -> None:
    """Hosts get the truthful breakdown per model call, not only on request."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        await loop.submit(UserTurn(content="x" * 4_000))
        await loop.wait_turn()
        events = loop.bus.log(sid)
    finally:
        await loop.aclose()
    usages = [e for e in events if isinstance(e, Usage)]
    assert len(usages) == 1
    snapshot = usages[0].context_usage
    assert snapshot is not None
    assert snapshot["measured"] is True
    assert snapshot["totalTokens"] == _MeasuredProvider.PROMPT_TOKENS
    assert usages[0].input_tokens == 1_000 and usages[0].cache_read_tokens == 2_000


# ─── Delegated: the CLI's per-call breakdown ─────────────────────────────────


async def test_delegated_usage_carries_the_clis_context_snapshot() -> None:
    """The CLI answers the per-call query mid-turn; its payload rides a second
    Usage event for the same node (token fields 0 — the first event billed them)."""
    from tests.fixtures.fake_claude import CONTEXT_USAGE_PAYLOAD

    loop = _delegated("j")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "DelegatedSurface", "BlockStart", "TextDelta", "BlockStop",
        "Usage", "Usage", "TurnComplete",
    ]
    billed, snapshot = [e for e in events if isinstance(e, Usage)]
    assert billed.context_usage is None and billed.input_tokens == 12
    assert snapshot.context_usage == CONTEXT_USAGE_PAYLOAD
    assert snapshot.node_id == billed.node_id
    assert (snapshot.input_tokens, snapshot.output_tokens) == (0, 0)
    assert not loop._pending_usage_snapshots


async def test_delegated_unanswered_snapshot_adds_no_event() -> None:
    """A CLI that never answers costs nothing: the billed Usage already said
    context_usage=None, so the turn's stream is unchanged."""
    loop = _delegated("h")  # never answers a control_request
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "DelegatedSurface",
        "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
    ]
    assert [e.context_usage for e in events if isinstance(e, Usage)] == [None]
    assert not loop._pending_usage_snapshots


# ─── Durable codec ───────────────────────────────────────────────────────────


def test_usage_events_round_trip_with_the_snapshot() -> None:
    event = Usage(
        session_id="s1", node_id="n1", turn_id="t1", input_tokens=3, output_tokens=1,
        context_usage={"totalTokens": 9, "measured": True},
    )
    assert event_from_dict(event_to_dict(event)) == event


def test_legacy_usage_rows_deserialize_without_the_snapshot() -> None:
    """Rows persisted before the field existed still rehydrate."""
    legacy = {
        "_cls": "Usage", "session_id": "s1", "node_id": "n1", "turn_id": "t1",
        "seq": 4, "input_tokens": 3, "output_tokens": 1, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "raw": {"input_tokens": 3},
    }
    revived = event_from_dict(legacy)
    assert isinstance(revived, Usage)
    assert revived.context_usage is None and revived.input_tokens == 3


def test_measured_total_below_estimates_scales_rows_instead_of_dropping_messages() -> None:
    from fluid.context.usage import MESSAGES, SYSTEM_PROMPT, build_context_usage
    from fluid.messages.content import Message, Text

    history = [Message(role="user", content=[Text(text="x" * 400)])]
    usage = build_context_usage(
        system_text="s" * 4000, sections=(), tools_api=[], deferred_tools_api=[],
        history=history, measured_prompt_tokens=600, context_window=10_000, model="m",
    )
    names = {c["name"]: c["tokens"] for c in usage["categories"]}
    assert usage["totalTokens"] == 600
    assert names[MESSAGES] > 0
    assert names[SYSTEM_PROMPT] < 1000  # scaled down from the ~1000 estimate


# ─── Every section kind lands in a category ──────────────────────────────────


def _section(kind: str) -> PromptSection:
    # 400 chars → 100 tokens each, so a dropped kind is visible in the total.
    if kind == "tools":
        return PromptSection(kind=kind, text="t" * 400, tool_name="Read")
    if kind == "memory":
        return PromptSection(kind=kind, text="m" * 400, path="AGENTS.md", scope="project")
    return PromptSection(kind=kind, text=kind[0] * 400)


def test_every_compiler_section_kind_is_priced() -> None:
    """A kind the breakdown does not classify must still land in the total,
    or a prompt section contributes zero tokens without anyone noticing."""
    sections = [_section(kind) for kind in SECTION_KINDS]
    usage = build_context_usage(sections=sections, model="m")
    assert usage["totalTokens"] == 100 * len(SECTION_KINDS)
    counted = sum(
        c["tokens"] for c in usage["categories"] if not c.get("isDeferred")
    )
    assert counted == usage["totalTokens"]


def test_unknown_section_kind_prices_as_system_prompt() -> None:
    usage = build_context_usage(
        sections=[PromptSection(kind="base", text="b" * 400),
                  PromptSection(kind="not-a-kind-yet", text="?" * 800)],
        model="m",
    )
    assert _by_name(usage)[SYSTEM_PROMPT]["tokens"] == 300
    assert usage["totalTokens"] == 300


# ─── Discovered agents: the index line is in the prompt, the body is not ─────


def _agent_md(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


async def test_discovered_agent_bodies_are_not_priced(tmp_path, monkeypatch) -> None:
    """Two agent files whose bodies dwarf the real prompt: the compiled prompt
    carries only their index lines, and the breakdown prices only that —
    no phantom "Custom agents" row, and the rows still sum to the prompt."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    proj = _project(tmp_path)
    agents_dir = proj / ".agents" / "agents"
    agents_dir.mkdir(parents=True)
    bodies = {
        "reviewer": "REVIEWER WORKER PROMPT " * 600,   # ~3,450 tokens each
        "tester": "TESTER WORKER PROMPT " * 700,
    }
    (agents_dir / "reviewer.md").write_text(
        _agent_md("reviewer", "Reviews code", bodies["reviewer"]), encoding="utf-8"
    )
    (agents_dir / "tester.md").write_text(
        _agent_md("tester", "Writes tests", bodies["tester"]), encoding="utf-8"
    )
    compiled = compile_prompt(_inputs(proj))
    assert "- reviewer: Reviews code" in compiled.text
    assert "- tester: Writes tests" in compiled.text
    for body in bodies.values():
        assert body[:60] not in compiled.text

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=_MeasuredProvider(), store=store, model="claude-sonnet-5",
        system=compiled.text, system_sections=compiled,
        registry=_registry_with_deferred(), ctx=ToolCtx(cwd=str(proj)),
        session_id=sid,
    )
    try:
        assert [d.name for d in loop.native_agents] == ["reviewer", "tester"]
        assert loop.agents == []
        usage = await loop.get_context_usage()
        prompt_tokens = sum(estimate_tokens_of_text(s.text) for s in compiled.sections)
        tools = sum(tool_schema_tokens(t) for t in loop._tools_api())
        assert CUSTOM_AGENTS not in _by_name(usage)
        assert usage["agents"] == []
        assert usage["totalTokens"] == prompt_tokens + tools
        assert usage["totalTokens"] < 2_000  # the bodies alone would be ~6,900

        # With a measured total the fixed rows keep their own estimates: no
        # phantom row pushes them over the measurement and scales them down.
        await loop.submit(UserTurn(content="x" * 4_000))
        await loop.wait_turn()
        measured = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert measured["measured"] is True
    assert _by_name(measured)[SYSTEM_PROMPT]["tokens"] == sum(
        estimate_tokens_of_text(s.text)
        for s in compiled.sections if s.kind not in ("memory", "skills")
    )


# ─── Measured total describes the NEXT request ───────────────────────────────


async def test_native_measured_total_carries_what_was_appended_since_the_call(
    tmp_path,
) -> None:
    """The provider measured the prompt BEFORE its reply; the next request also
    carries the reply and any user text written since, at the estimate."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        await loop.submit(UserTurn(content="x" * 4_000))
        await loop.wait_turn()
        before = await loop.get_context_usage()
        head = (await store.load_path(sid))[-1]
        assert head.message.role == "assistant"
        await store.append(sid, SessionNode(
            node_id=new_id("node"), parent_id=head.node_id,
            message=Message(role="user", content=[Text("y" * 4_000)]),
        ))
        after = await loop.get_context_usage()
    finally:
        await loop.aclose()
    reply_tokens = estimate_tokens_of_text(head.message.text())
    assert before["measured"] is True
    assert before["totalTokens"] == _MeasuredProvider.PROMPT_TOKENS + reply_tokens
    assert after["measured"] is True
    assert after["totalTokens"] == _MeasuredProvider.PROMPT_TOKENS + reply_tokens + 1_000


async def test_native_measured_total_survives_an_engine_rebuild(tmp_path) -> None:
    """A resumed engine recovers the last call's billed prompt from the
    persisted Usage event instead of guessing until its first call."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    first = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        await first.submit(UserTurn(content="x" * 4_000))
        await first.wait_turn()
        live = await first.get_context_usage()
    finally:
        await first.aclose()

    # A second project dir: the helper builds the fixture tree afresh.
    second = _loop(tmp_path / "rebuilt", store, session_id=sid,
                   provider=_MeasuredProvider())
    try:
        assert sid not in second._last_call_usage
        resumed = await second.get_context_usage()
        assert sid in second._last_call_usage
    finally:
        await second.aclose()
    assert resumed["measured"] is True
    assert resumed["totalTokens"] == live["totalTokens"]


async def test_native_rebuild_without_usage_events_stays_an_estimate(tmp_path) -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode(
        node_id=new_id("node"), parent_id=None,
        message=Message(role="user", content=[Text("x" * 4_000)]),
    ))
    loop = _loop(tmp_path, store, session_id=sid, provider=_MeasuredProvider())
    try:
        usage = await loop.get_context_usage()
    finally:
        await loop.aclose()
    assert usage["measured"] is False


# ─── billing only what this leg actually mounts ──────────────────────────────


def test_unmounted_tools_are_not_billed_as_tools() -> None:
    """A host that never wired agent messaging was still charged 546 tokens for
    SendMessage and 245 for ListAgents, and advertised both in the prompt, on a
    leg where the engine withheld them from every surface the model could
    call. ``mounted_tools`` is the capability-filtered surface; a name outside
    it gets no row."""
    read = {"name": "Read", "description": "r", "input_schema": {}}
    send = {"name": "SendMessage", "description": "s" * 400, "input_schema": {}}
    usage = build_context_usage(
        tools_api=[read, send], model="m", mounted_tools={"Read"}
    )
    assert usage["systemTools"] == [
        {"name": "Read", "tokens": tool_schema_tokens(read)},
    ]
    # The schema is not sent, so its tokens leave the total too.
    assert _by_name(usage)[SYSTEM_TOOLS]["tokens"] == tool_schema_tokens(read)


def test_unmounted_mcp_and_deferred_tools_drop_out_as_well() -> None:
    slack = {"name": "mcp__slack__send", "description": "s", "input_schema": {}}
    hidden = {"name": "ListAgents", "description": "l", "input_schema": {}}
    usage = build_context_usage(
        tools_api=[slack],
        deferred_tools_api=[hidden],
        model="m",
        mounted_tools={"Read"},
    )
    assert usage["mcpTools"] == []
    assert usage["deferredBuiltinTools"] == []


def test_an_unmounted_tools_section_falls_back_to_system_prompt() -> None:
    """Its text IS in the prompt, so the tokens must still land somewhere —
    just not billed as a tool the session cannot call."""
    send_text = "## SendMessage\n" + "s" * 400
    sections = (
        PromptSection(kind="base", text="base"),
        PromptSection(kind="tools", text="## Read\nr", tool_name="Read"),
        PromptSection(kind="tools", text=send_text, tool_name="SendMessage"),
    )
    everything = build_context_usage(sections=sections, model="m")
    filtered = build_context_usage(
        sections=sections, model="m", mounted_tools={"Read"}
    )
    assert [r["name"] for r in filtered["systemTools"]] == ["Read"]
    # Nothing lost: the same total, redistributed into "System prompt".
    assert filtered["totalTokens"] == everything["totalTokens"]
    assert (
        _by_name(filtered)[SYSTEM_PROMPT]["tokens"]
        > _by_name(everything)[SYSTEM_PROMPT]["tokens"]
    )


def test_no_mounted_set_bills_everything_as_before() -> None:
    read = {"name": "Read", "description": "r", "input_schema": {}}
    send = {"name": "SendMessage", "description": "s", "input_schema": {}}
    usage = build_context_usage(tools_api=[read, send], model="m")
    assert [r["name"] for r in usage["systemTools"]] == ["Read", "SendMessage"]
