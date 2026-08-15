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
import json
import sys
from pathlib import Path

import pytest

from fluid.context.compiler import (
    PromptInputs,
    PromptSection,
    compile_prompt,
    compile_system_prompt,
)
from fluid.context.trim import TrimConfig, estimate_tokens_of_text
from fluid.context.usage import (
    DEFERRED_TOOLS,
    FREE_SPACE,
    MEMORY_FILES,
    MESSAGES,
    SKILLS,
    SYSTEM_PROMPT,
    TOOLS,
    build_context_usage,
)
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, parse_control_response
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode, new_id
from fluid.tools.registry import ToolRegistry, ToolSpec

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


def _cmd() -> list[str]:
    return [sys.executable, FAKE]


# ─── Capability bit ──────────────────────────────────────────────────────────


def test_caps_advertise_context_usage() -> None:
    assert NativeLoop.caps.context_usage is True
    assert DelegatedLoop.caps.context_usage is True


# ─── Compiler: sections without changing the joined string ───────────────────


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (proj / ".git").mkdir()
    (proj / "CLAUDE.md").write_text("Project rule: be terse.", encoding="utf-8")
    (proj / "AGENTS.md").write_text("Agents rule: run tests.", encoding="utf-8")
    (proj / ".claude" / "skills" / "deploy" / "SKILL.md").write_text(
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
    assert {Path(s.path or "").name for s in memory} == {"CLAUDE.md", "AGENTS.md"}
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
    assert set(cats) == {SYSTEM_PROMPT, MEMORY_FILES, SKILLS, TOOLS, MESSAGES}
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


def test_deferred_tools_are_flagged_and_excluded_from_the_total() -> None:
    deferred = [{"name": "mcp__x__big", "description": "d" * 400, "input_schema": {}}]
    with_deferred = build_context_usage(
        deferred_tools_api=deferred, model="m", context_window=10_000,
    )
    cats = _by_name(with_deferred)
    assert cats[DEFERRED_TOOLS]["isDeferred"] is True
    assert cats[DEFERRED_TOOLS]["tokens"] > 0
    # Not in the prompt → not in the total, and free space is untouched by it.
    assert with_deferred["totalTokens"] == 0
    assert cats[FREE_SPACE]["tokens"] == 10_000
    assert "isDeferred" not in cats[FREE_SPACE]


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
    assert {Path(e["path"]).name for e in files} == {"CLAUDE.md", "AGENTS.md"}
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


def _loop(tmp_path, store, **kw) -> NativeLoop:
    compiled = compile_prompt(_inputs(_project(tmp_path)))
    return NativeLoop(
        provider=_IdleProvider(), store=store, model="claude-sonnet-5",
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
        "CLAUDE.md", "AGENTS.md",
    }
    # Full SDK parity: the per-file detail ships in the response, not just in
    # the helper — same list, same order, in the CLI's entry shape.
    assert usage["memoryFiles"] == [m.to_dict() for m in loop.memory_file_usage()]
    assert all(
        set(e) == {"path", "type", "tokens"} and e["type"] == "Project"
        for e in usage["memoryFiles"]
    )
    assert cats[TOOLS]["tokens"] == estimate_tokens_of_text(
        json.dumps([{"name": "Read", "description": "Read a file",
                     "input_schema": {"type": "object"}}], default=str)
    )
    assert cats[DEFERRED_TOOLS]["isDeferred"] is True
    assert usage["maxTokens"] == 200_000 and usage["percentage"] is not None
    assert usage["model"] == "claude-sonnet-5"


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
        trimmed = _by_name(await loop.get_context_usage())[MESSAGES]["tokens"]
    finally:
        await loop.aclose()
    assert trimmed < untrimmed_messages


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
    # A trigger this low would arm a batch under apply(); view() must not.
    assert loop._trimmer._trimmed == set()


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
    assert [type(e).__name__ for e in events] == [
        "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
    ] * 2
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
        "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
    ] * 2
