"""Repeat calls answered with a pointer only while the earlier result is in view.

The owner's rule: only cut what the model does not need again, and make
anything cut easy to get back. A pointer that hides a real change, or points
at a result the request no longer carries, is a bug. These tests pin both
sides: the pointer fires when the content is unchanged AND the earlier result
is still verbatim in the model's request, and misses after a trim, fold or
compaction, a file change, on surfaces that cannot check the view, and across
sessions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid.codemode.journal import FrictionDetector
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode, new_id
from fluid.tools import view
from fluid.tools.builtins import glob as glob_tool
from fluid.tools.builtins import grep as grep_tool
from fluid.tools.builtins import read as read_tool
from fluid.tools.builtins import skill as skill_tool
from fluid.tools.registry import ToolCtx


def _native(ctx: ToolCtx, call_id: str, in_view: set[str] | frozenset[str]) -> ToolCtx:
    """Put ``ctx`` in the state NativeLoop._dispatch_tool sets for one call."""
    ctx.extra["tool_use_id"] = call_id
    ctx.view_results = frozenset(in_view)
    return ctx


def _is_pointer(text: str) -> bool:
    return text.startswith(view.POINTER_MARK)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_text(
        "".join(f"line {i} of the notes file, long enough to be worth a pointer\n"
                for i in range(1, 41)),
        encoding="utf-8",
    )
    for i in range(40):
        (tmp_path / f"companion_file_with_a_long_descriptive_name_{i:02}.txt").write_text(
            "x\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def test_unchanged_reread_in_view_gets_a_pointer_naming_the_earlier_result(source):
    ctx = ToolCtx(cwd=str(source.parent))
    first = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_1", set()))
    assert "line 40" in first.text

    again = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_2", {"toolu_1"}))
    assert _is_pointer(again.text)
    assert "(earlier result toolu_1)" in again.text
    assert "line 40" not in again.text
    assert len(again.text) < len(first.text)


async def test_reread_after_the_earlier_result_left_the_view_returns_content(source):
    # A trim replaced it, a fold summarized it or a compaction dropped it: in
    # every case it is no longer in the set of verbatim results.
    ctx = ToolCtx(cwd=str(source.parent))
    await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_1", set()))
    again = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_2", {"toolu_other"}))
    assert "line 40" in again.text
    # No "reuse what you learned" note either: that copy is gone.
    assert "already" not in again.text.lower()
    # The fresh copy is now the one a later repeat may point at.
    third = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_3", {"toolu_2"}))
    assert "(earlier result toolu_2)" in third.text


async def test_reread_after_a_file_change_returns_content(source):
    ctx = ToolCtx(cwd=str(source.parent))
    await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_1", set()))
    before = os.stat(source)
    # Same size and the same mtime: only the content hash can tell.
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("line 7 of", "LINE 7 of"), encoding="utf-8")
    assert source.read_text(encoding="utf-8") != original
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(source).st_size == before.st_size

    again = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_2", {"toolu_1"}))
    assert "LINE 7" in again.text
    assert not _is_pointer(again.text)


async def test_a_different_window_is_not_a_repeat(source):
    ctx = ToolCtx(cwd=str(source.parent))
    await read_tool.handler({"file_path": str(source), "offset": 1, "limit": 20},
                            _native(ctx, "toolu_1", set()))
    other = await read_tool.handler({"file_path": str(source), "offset": 21, "limit": 20},
                                    _native(ctx, "toolu_2", {"toolu_1"}))
    assert "line 25" in other.text
    same = await read_tool.handler({"file_path": str(source), "offset": 1, "limit": 20},
                                   _native(ctx, "toolu_3", {"toolu_1", "toolu_2"}))
    assert "lines 1-20" in same.text and _is_pointer(same.text)


async def test_a_short_result_is_sent_again_rather_than_pointed_at(tmp_path):
    small = tmp_path / "small.txt"
    small.write_text("one\ntwo\n", encoding="utf-8")
    ctx = ToolCtx(cwd=str(tmp_path))
    await read_tool.handler({"file_path": str(small)}, _native(ctx, "toolu_1", set()))
    again = await read_tool.handler({"file_path": str(small)}, _native(ctx, "toolu_2", {"toolu_1"}))
    assert "two" in again.text and not _is_pointer(again.text)


async def test_a_pointer_is_spent_so_asking_again_returns_the_content(source):
    ctx = ToolCtx(cwd=str(source.parent))
    await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_1", set()))
    pointed = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_2", {"toolu_1", "toolu_2"}))
    assert _is_pointer(pointed.text)
    back = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_3", {"toolu_1", "toolu_2"}))
    assert "line 40" in back.text


@pytest.mark.parametrize("surface", ["no_view", "code_mode"])
async def test_surfaces_that_cannot_see_the_view_never_get_a_pointer(source, surface):
    # The delegated CLI and host calls outside the native loop publish no
    # view; a code-mode cell may never have printed the earlier result.
    ctx = ToolCtx(cwd=str(source.parent))
    for call in ("toolu_1", "toolu_2"):
        ctx.extra["tool_use_id"] = call
        if surface == "code_mode":
            ctx.view_results = frozenset({"toolu_1"})
            ctx.result_budget = 1_000_000
        out = await read_tool.handler({"file_path": str(source)}, ctx)
        assert not _is_pointer(out.text)
        assert "line 40" in out.text
    assert ctx.deliveries == {}


async def test_another_session_sharing_the_ctx_cannot_point_at_this_one(source):
    # A sub-agent or second session on the same loop answers from its own
    # request, which never carries the first session's call.
    ctx = ToolCtx(cwd=str(source.parent))
    await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_parent", set()))
    child = await read_tool.handler({"file_path": str(source)}, _native(ctx, "toolu_child", {"toolu_x"}))
    assert "line 40" in child.text
    # A separate ctx (an independent child) has no record at all.
    fresh = ToolCtx(cwd=str(source.parent))
    alone = await read_tool.handler({"file_path": str(source)}, _native(fresh, "toolu_c2", {"toolu_parent"}))
    assert "line 40" in alone.text


# ---------------------------------------------------------------------------
# Grep and Glob
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool, args", [
    (grep_tool, {"pattern": "notes"}),
    (glob_tool, {"pattern": "*.txt"}),
])
async def test_identical_search_points_only_while_inputs_and_view_hold(source, tool, args):
    ctx = ToolCtx(cwd=str(source.parent))
    first = await tool.handler(dict(args), _native(ctx, "toolu_1", set()))
    assert not first.is_error and not _is_pointer(first.text)

    again = await tool.handler(dict(args), _native(ctx, "toolu_2", {"toolu_1"}))
    assert _is_pointer(again.text) and "(earlier result toolu_1)" in again.text

    # Trimmed out: the full result comes back.
    gone = await tool.handler(dict(args), _native(ctx, "toolu_3", set()))
    assert gone.text == first.text

    # A change on disk changes the result, so it is delivered.
    (source.parent / "more.txt").write_text("notes again\n", encoding="utf-8")
    changed = await tool.handler(dict(args), _native(ctx, "toolu_4", {"toolu_3"}))
    assert not _is_pointer(changed.text)
    assert "more.txt" in changed.text


async def test_grep_without_a_view_keeps_its_old_behaviour(source):
    ctx = ToolCtx(cwd=str(source.parent))
    await grep_tool.handler({"pattern": "notes"}, ctx)
    again = await grep_tool.handler({"pattern": "notes"}, ctx)
    assert not _is_pointer(again.text)
    assert ctx.deliveries == {}


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

def _home_skill(monkeypatch, tmp_path: Path, body: str) -> Path:
    home = tmp_path / "home"
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    root = home / ".fluid" / "skills" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(f"---\nname: demo\ndescription: d\n---\n{body}", encoding="utf-8")
    return path


async def test_skill_reload_in_view_points_and_reloads_after_fold_or_edit(monkeypatch, tmp_path):
    path = _home_skill(monkeypatch, tmp_path, "step one " * 200)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    ctx = ToolCtx(cwd=str(cwd))
    first = await skill_tool.handler({"name": "demo"}, _native(ctx, "toolu_1", set()))
    assert "step one" in first.text

    again = await skill_tool.handler({"name": "demo"}, _native(ctx, "toolu_2", {"toolu_1"}))
    assert _is_pointer(again.text) and "demo skill" in again.text
    assert len(again.text) < 400

    # Folded away: the instructions load in full again.
    folded = await skill_tool.handler({"name": "demo"}, _native(ctx, "toolu_3", set()))
    assert folded.text == first.text

    # The skill file changed: the new instructions load even while the old
    # copy is still in view.
    path.write_text("---\nname: demo\ndescription: d\n---\nstep two", encoding="utf-8")
    edited = await skill_tool.handler({"name": "demo"}, _native(ctx, "toolu_4", {"toolu_3"}))
    assert "step two" in edited.text and not _is_pointer(edited.text)


# ---------------------------------------------------------------------------
# What is in view, and pointers whose target left it
# ---------------------------------------------------------------------------

def _pair(call_id: str, name: str, text: str) -> tuple[Message, Message]:
    return (
        Message(role="assistant", content=[ToolUseBlock(id=call_id, name=name, input={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id=call_id, content=[Text(text)])]),
    )


def _nodes(messages: list[Message]) -> list[SessionNode]:
    return [SessionNode(node_id=new_id("node"), parent_id=None, message=m) for m in messages]


def test_only_results_rendered_exactly_as_stored_are_in_view():
    a_use, a_res = _pair("toolu_a", "Read", "alpha body")
    b_use, b_res = _pair("toolu_b", "Read", "beta body")
    c_use, c_res = _pair("toolu_c", "Read", "gamma body")
    stored = [a_use, a_res, b_use, b_res, c_use, c_res]
    trimmed_b = Message(role="user", content=[
        ToolResultBlock(tool_use_id="toolu_b", content=[Text("[trimmed]")]),
    ])
    # c was folded into a summary: it is not in the request at all.
    rendered = [a_use, a_res, b_use, trimmed_b]
    assert view.verbatim_tool_results(_nodes(stored), rendered) == {"toolu_a"}


def test_a_pointer_whose_target_left_the_view_is_shown_as_gone():
    a_use, a_res = _pair("toolu_a", "Read", "alpha body")
    p_text = view.pointer("x lines 1-3", "your earlier Read", "toolu_a")
    p_use, p_res = _pair("toolu_p", "Read", p_text)
    history = [a_use, a_res, p_use, p_res]

    kept = view.settle_pointers(history, frozenset({"toolu_a"}))
    assert all(new is old for new, old in zip(kept, history))

    settled = view.settle_pointers(history, frozenset())
    assert settled[:3] == history[:3] and settled[1] is a_res
    note = settled[3].content[0].content[0].text
    assert note.startswith(view.GONE_MARK) and "toolu_a" in note
    # The input is untouched.
    assert history[3].content[0].content[0].text == p_text


def test_marker_text_from_other_tools_is_left_alone():
    p_text = view.pointer("x", "y", "toolu_gone")
    use, res = _pair("toolu_bash", "Bash", p_text)
    assert view.settle_pointers([use, res], frozenset())[1] is res


# ---------------------------------------------------------------------------
# Through a real NativeLoop
# ---------------------------------------------------------------------------

def _chunks(tuid: str | None, args: dict | None = None, text: str = "done"):
    import json

    from fluid.providers.base import StreamChunk

    if tuid is None:
        body = [
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": text}),
        ]
        stop = "end_turn"
    else:
        body = [
            StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                        "tool_name": "Read", "tool_use_id": tuid}),
            StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)}),
        ]
        stop = "tool_use"
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        *body,
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": stop}),
        StreamChunk("message_stop", {}),
    ]


class _Scripted:
    name = "anthropic"

    def __init__(self, scripts):
        from fluid.providers.base import ProviderCaps

        self.caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)
        self.scripts = scripts
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield chunk


def _result_text(request, tuid: str) -> str:
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and block.tool_use_id == tuid:
                return "".join(getattr(part, "text", "") for part in block.content)
    raise AssertionError(f"{tuid} not in request")


@pytest.mark.parametrize("still_in_view", [True, False])
async def test_native_loop_points_only_at_a_result_its_request_carries(
    source, monkeypatch, still_in_view,
):
    from fluid.engine import native
    from fluid.events import UserTurn
    from fluid.permissions.engine import PermissionEngine
    from fluid.store.base import MemoryStore
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    if not still_in_view:
        # Stands in for a trim or fold that rewrote the earlier result.
        monkeypatch.setattr(native, "verbatim_tool_results", lambda nodes, history: frozenset())
    registry = ToolRegistry()
    register_builtins(registry)
    args = {"file_path": str(source)}
    provider = _Scripted([_chunks("t1", args), _chunks("t2", args), _chunks(None)])
    loop = native.NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(source.parent)),
    )
    await loop.submit(UserTurn(content="read it twice"))
    await loop.wait_turn()

    final = provider.requests[-1]
    assert "line 40" in _result_text(final, "t1")
    second = _result_text(final, "t2")
    if still_in_view:
        assert _is_pointer(second) and "(earlier result t1)" in second
    else:
        assert "line 40" in second
    # Nothing is left published once the calls are done.
    assert loop.ctx.view_results is None


async def test_a_gone_pointer_keeps_the_steer_and_blocks_riding_on_it(source, monkeypatch):
    """A steer the user sent during the repeat call is appended to the
    pointer's text, and a reminder rides as a second block. When a later trim
    drops the earlier result, only the pointer's own words become the "gone"
    note: the user's instruction, the other block and the delivery record are
    all still in the request."""
    from fluid.engine import native
    from fluid.events import UserTurn
    from fluid.events.ops import Steer
    from fluid.permissions.engine import PermissionEngine
    from fluid.store.base import MemoryStore
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    real_view = native.verbatim_tool_results
    computed = []

    def view_of(nodes, history):
        computed.append(None)
        # Requests 1 and 2 see the real view; from request 3 on, a trim
        # has rewritten the earlier result t1.
        return real_view(nodes, history) if len(computed) < 3 else frozenset()

    monkeypatch.setattr(native, "verbatim_tool_results", view_of)
    registry = ToolRegistry()
    register_builtins(registry)
    args = {"file_path": str(source)}

    class _Steering(_Scripted):
        async def stream(self, request):
            if len(self.requests) == 1:  # serving request 2: t2 is next
                await loop.submit(Steer(text="Do not deploy", id="s1"))
            async for chunk in super().stream(request):
                yield chunk

    provider = _Steering([_chunks("t1", args), _chunks("t2", args), _chunks(None)])
    loop = native.NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(source.parent)),
    )
    nudges = iter([None, "REFLECTION-BLOCK"])
    monkeypatch.setattr(loop, "_reflection_nudge", lambda: next(nudges, None))
    await loop.submit(UserTurn(content="read it twice"))
    await loop.wait_turn()

    final = provider.requests[-1]
    block = next(
        b for m in final.messages for b in m.content
        if isinstance(b, ToolResultBlock) and b.tool_use_id == "t2"
    )
    first = block.content[0].text
    assert first.startswith(view.GONE_MARK) and "(earlier result t1)" in first
    assert view.POINTER_MARK not in first and "still above" not in first
    assert "Do not deploy" in first
    assert [p.text for p in block.content[1:]] == ["REFLECTION-BLOCK"]
    assert [d.id for d in block.delivered_steers] == ["s1"]
    # The stored transcript still holds the pointer: settlement is render-only.
    stored = [
        b for n in await loop.store.load_path(loop.session_id) for b in n.message.content
        if isinstance(b, ToolResultBlock) and b.tool_use_id == "t2"
    ]
    assert stored[0].content[0].text.startswith(view.POINTER_MARK)


def test_settlement_rewrites_only_the_pointer_span():
    a_use, a_res = _pair("toolu_a", "Read", "alpha body")
    p_text = view.pointer("x lines 1-3", "your earlier Read", "toolu_a")
    p_use = Message(role="assistant", content=[ToolUseBlock(id="toolu_p", name="Read", input={})])
    appended = "\n\n<hook context>keep me</hook context>"
    p_res = Message(role="user", content=[ToolResultBlock(
        tool_use_id="toolu_p",
        content=[Text(p_text + appended), Text("answer to your question")],
    )])
    settled = view.settle_pointers([a_use, a_res, p_use, p_res], frozenset())
    parts = settled[3].content[0].content
    assert parts[0].text.startswith(view.GONE_MARK)
    assert parts[0].text.endswith(appended)
    assert parts[1].text == "answer to your question"
    # A pointer whose target is still carried is left as the same object.
    kept = view.settle_pointers([a_use, a_res, p_use, p_res], frozenset({"toolu_a"}))
    assert kept[3] is p_res


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def test_pointer_hits_count_and_a_re_request_is_flagged():
    friction = FrictionDetector()
    pointed = view.pointer("a.py lines 1-40", "your earlier Read", "toolu_1")
    hit = friction.observe("Read", {"file_path": "a.py"}, pointed, False)
    assert [e["signal"] for e in hit] == ["repeat_work"]
    # Asked again for the same file, with another window: the pointer did
    # not land.
    again = friction.observe("Read", {"file_path": "a.py", "offset": 5}, "body", False)
    assert "reread_after_pointer" in [e["signal"] for e in again]
    # A different file after a pointer is not a re-request.
    friction.observe("Read", {"file_path": "a.py"}, pointed, False)
    other = friction.observe("Read", {"file_path": "b.py"}, "body", False)
    assert "reread_after_pointer" not in [e["signal"] for e in other]
