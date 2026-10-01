"""Arg elision behind CompactionConfig.elide_old_args (plan item 11).

The input of an old tool call whose result tier 1 already trimmed renders as a
small JSON summary object. These tests pin the contract: off by default and
byte-identical when off, keyed only on the persisted trimmed set (so resume and
forks behave), errors and the protected tail untouched, the stored tree never
mutated, and both serializers carrying the elided shape as valid JSON.
"""

import json

from fluid.config.model import ContextConfig
from fluid.context.arg_elision import (
    ELIDED_KEY,
    MIN_ELIDE_CHARS,
    SUMMARY_CHARS,
    elidable_call_ids,
    elide_tool_args,
    elided_input,
    summarize_input,
)
from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.trim import TrimConfig
from fluid.messages import Message, Reasoning, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.serialize import (
    build_request_body as responses_body,
)
from fluid.store.base import SessionNode


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _cell(t: int) -> str:
    return f"files = tools.Glob(pattern='src/{t}/**')\n" + "print(len(files))\n" * 60


def _turn(t: int, *, error: bool = False, reasoning: bool = False) -> list[SessionNode]:
    """user, assistant(run_python), result, conclusion."""
    assistant: list = []
    if reasoning:
        assistant.append(Reasoning(
            origin="openai_responses", encrypted_content=f"ENC-{t}",
            summary_text=f"thinking {t}", model="gpt-5.6-sol",
        ))
    assistant += [Text(f"checking {t}"),
                  ToolUseBlock(id=f"call{t}", name="run_python", input={"code": _cell(t)})]
    return [
        _node(f"node_u{t}", "user", [Text(f"question {t}")]),
        _node(f"node_a{t}", "assistant", assistant),
        _node(f"node_r{t}", "user", [ToolResultBlock(
            tool_use_id=f"call{t}", content=[Text(f"out-{t} " + "y" * 4_000)],
            is_error=error,
        )]),
        _node(f"node_f{t}", "assistant", [Text(f"done {t}")]),
    ]


def _history(turns: int, *, errors: int = 0, reasoning: bool = False) -> list[SessionNode]:
    """``turns`` turns; the first ``errors`` of them end in an error result."""
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes.extend(_turn(t, error=t < errors, reasoning=reasoning))
    return nodes


def _trim_only(**kw) -> CompactionConfig:
    return CompactionConfig(
        enabled=False,
        trim=TrimConfig(enabled=True, trigger_tokens=1_000,
                        keep_recent_tool_results=2, min_result_chars=500),
        **kw,
    )


def _call(messages: list[Message], call_id: str) -> ToolUseBlock:
    return next(b for m in messages for b in m.content
                if isinstance(b, ToolUseBlock) and b.id == call_id)


def _dump(messages: list[Message]) -> str:
    return json.dumps([m.to_dict() for m in messages], sort_keys=True)


# The summary object


def test_elided_input_is_a_small_json_summary() -> None:
    original = {"code": "\n\nimport os\n" + "x = 1\n" * 200}
    elided = elided_input(original, "node_a1")
    assert elided == {
        ELIDED_KEY: True,
        "summary": "import os",
        "chars": len(json.dumps(original, separators=(",", ":"))),
        "node": "node_a1",
    }
    assert json.loads(json.dumps(elided)) == elided


def test_small_and_already_elided_inputs_are_kept() -> None:
    assert elided_input({"file_path": "a.py"}, "n") is None
    assert elided_input({"code": "x" * (MIN_ELIDE_CHARS - 20)}, "n") is None
    once = elided_input({"code": "x" * 5_000}, "n")
    assert once is not None and elided_input(once, "n") is None


def test_summary_uses_the_longest_string_field_and_clips() -> None:
    wide = {"file_path": "a.py", "content": "line one\nline two" + "z" * 900}
    assert summarize_input(wide) == "line one"
    long = summarize_input({"code": "q" * 500})
    assert len(long) == SUMMARY_CHARS and long.endswith("…")
    assert summarize_input({"n": 1}) == '{"n":1}'


def test_elide_tool_args_returns_the_same_object_when_nothing_changes() -> None:
    msg = Message(role="assistant", content=[
        ToolUseBlock(id="c1", name="Read", input={"file_path": "a.py"}),
    ])
    assert elide_tool_args("n", msg, frozenset({"c1"})) is msg
    assert elide_tool_args("n", msg, frozenset()) is msg


def test_off_path_trim_pairs_do_not_elide() -> None:
    pairs = {("node_r1", "call1"), ("node_other", "call2")}
    assert elidable_call_ids(pairs, {"node_r1"}) == frozenset({"call1"})


# The compactor hook


def test_flag_is_off_by_default_everywhere() -> None:
    assert CompactionConfig().elide_old_args is False
    assert ContextConfig().elide_old_args is False
    assert ContextConfig.from_dict({"elide_old_args": True}).elide_old_args is True


def test_flag_off_view_is_byte_identical() -> None:
    nodes = _history(6)
    off = Compactor(config=_trim_only())
    off.apply(nodes)
    assert off.trimmer._trimmed  # the trim really ran
    view = off.view(nodes)
    for node, message in zip(nodes, view):
        if node.message.role == "assistant":
            assert message is node.message
    explicit = Compactor(config=_trim_only(elide_old_args=False))
    # mark_trimmed is the resume API and defaults to render version 1; these
    # trims were live, so they replay at the trimmer's own version.
    explicit.trimmer.mark_trimmed(off.trimmer._trimmed,
                                  render_version=explicit.trimmer.render_version)
    assert _dump(explicit.view(nodes)) == _dump(view)


def test_trimmed_calls_are_elided_and_recent_ones_kept() -> None:
    nodes = _history(6)
    stored = _dump([n.message for n in nodes])
    comp = Compactor(config=_trim_only(elide_old_args=True))
    rendered, promotions = comp.apply(nodes)
    trimmed_ids = {tuid for _, tuid in comp.trimmer._trimmed}
    assert trimmed_ids and promotions
    # The newest results sit in the protected tail, so their calls stay verbatim.
    assert "call5" not in trimmed_ids and "call4" not in trimmed_ids
    for t in range(6):
        call = _call(rendered, f"call{t}")
        if f"call{t}" in trimmed_ids:
            assert call.input[ELIDED_KEY] is True
            assert call.input["node"] == f"node_a{t}"
            assert call.input["summary"] == f"files = tools.Glob(pattern='src/{t}/**')"
        else:
            assert call.input == {"code": _cell(t)}
    # The stored tree is never mutated.
    assert _dump([n.message for n in nodes]) == stored


def test_elision_lands_in_the_same_batch_and_saves_more() -> None:
    nodes = _history(6)
    plain = Compactor(config=_trim_only())
    elided = Compactor(config=_trim_only(elide_old_args=True))
    _, p1 = plain.apply(nodes)
    _, p2 = elided.apply(nodes)
    assert p1 and p2
    assert [p.trimmed for p in p1] == [p.trimmed for p in p2]
    assert p2[-1].after_tokens < p1[-1].after_tokens
    # Still one rewrite: the first changed message is the first elided call,
    # right before its own trimmed result.
    assert p2[-1].first_changed_message == 1
    assert p1[-1].first_changed_message == 2


def test_error_results_keep_their_call_verbatim() -> None:
    nodes = _history(6, errors=3)
    comp = Compactor(config=_trim_only(elide_old_args=True))
    rendered, _ = comp.apply(nodes)
    for t in range(3):
        assert _call(rendered, f"call{t}").input == {"code": _cell(t)}


def test_resume_from_promotions_is_byte_identical() -> None:
    nodes = _history(6)
    live = Compactor(config=_trim_only(elide_old_args=True))
    rendered, promotions = live.apply(nodes)
    assert promotions
    resumed = Compactor(config=_trim_only(elide_old_args=True))
    resumed.rehydrate(promotions)
    assert _dump(resumed.view(nodes)) == _dump(rendered)


def test_fork_with_the_result_off_path_keeps_the_call() -> None:
    nodes = _history(6)
    comp = Compactor(config=_trim_only(elide_old_args=True))
    comp.apply(nodes)
    # A fork that kept node_a0 but replaced its result with a new node.
    forked = [n for n in nodes if n.node_id != "node_r0"]
    forked.insert(2, _node("node_r0_fork", "user", [ToolResultBlock(
        tool_use_id="call0", content=[Text("fresh")])]))
    assert _call(comp.view(forked), "call0").input == {"code": _cell(0)}


def test_full_ladder_elides_live_calls_beside_digests() -> None:
    nodes = _history(8)
    cfg = CompactionConfig(
        trigger_tokens=1_000, target_ratio=0.5, keep_recent_turns=6,
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=2,
                        min_result_chars=500),
        elide_old_args=True,
    )
    rendered, promotions = Compactor(config=cfg).apply(nodes)
    assert [p.tier for p in promotions] == [1, 2]
    elided = [b for m in rendered for b in m.content
              if isinstance(b, ToolUseBlock) and ELIDED_KEY in b.input]
    # Turns 0 and 1 are digested; 2 to 5 are live with trimmed results; 6 and
    # 7 hold the protected results.
    assert [b.id for b in elided] == ["call2", "call3", "call4", "call5"]
    for block in elided:
        assert set(block.input) == {ELIDED_KEY, "summary", "chars", "node"}


# Serializers accept and round-trip the elided shape


def _elided_view(**kw) -> list[Message]:
    nodes = _history(6, **kw)
    rendered, _ = Compactor(config=_trim_only(elide_old_args=True)).apply(nodes)
    assert ELIDED_KEY in _call(rendered, "call0").input
    return rendered


def test_responses_function_call_arguments_carry_the_elided_object() -> None:
    view = _elided_view(reasoning=True)
    body = responses_body(ProviderRequest(model="gpt-5.6-sol", messages=view))
    items = body["input"]
    calls = [i for i in items if i["type"] == "function_call"]
    assert [c["call_id"] for c in calls] == [f"call{t}" for t in range(6)]
    first = calls[0]
    assert isinstance(first["arguments"], str)
    assert json.loads(first["arguments"]) == _call(view, "call0").input
    # The encrypted reasoning item still precedes its call, untouched.
    idx = items.index(first)
    assert items[idx - 2] == {
        "type": "reasoning", "encrypted_content": "ENC-0", "content": [],
        "summary": [{"type": "summary_text", "text": "thinking 0"}],
    }
    assert items[idx - 1]["type"] == "message"
    # Recent calls keep their original arguments.
    assert json.loads(calls[5]["arguments"]) == {"code": _cell(5)}
    outputs = {i["call_id"] for i in items if i["type"] == "function_call_output"}
    assert outputs == {c["call_id"] for c in calls}


def test_anthropic_tool_use_input_carries_the_elided_object() -> None:
    view = _elided_view()
    body = anthropic_body(ProviderRequest(model="claude-sonnet-5", messages=view))
    uses = [b for m in body["messages"] for b in m["content"] if b["type"] == "tool_use"]
    assert uses[0]["input"] == _call(view, "call0").input
    assert uses[0]["input"][ELIDED_KEY] is True
    assert uses[5]["input"] == {"code": _cell(5)}
    assert json.loads(json.dumps(body)) == body


def test_elided_message_round_trips_through_the_codec() -> None:
    view = _elided_view()
    msg = next(m for m in view if any(
        isinstance(b, ToolUseBlock) and ELIDED_KEY in b.input for b in m.content))
    assert Message.from_dict(json.loads(json.dumps(msg.to_dict()))) == msg
