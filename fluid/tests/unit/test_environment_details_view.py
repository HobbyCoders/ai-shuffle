"""The environment details ride only when the path does not hold them.

The date, branch and working tree go with every turn. Recent commits and the
runtime facts (about 1.2k characters) form their own block under
``ENVIRONMENT_DETAILS_HEADER``, inside the host-pinned range, sent only when
the path the turn extends holds no identical copy. The compactor keeps the
newest copy in view through any fold (``CompactionConfig.pinned_blocks``), so
nothing is restored between calls and nothing is reserved.

Each test drives real turns through the real compactor and a scripted
provider and checks every request actually sent. The control is the same
loop with the block unpinned, which sends it on every turn as base did.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest
from fluid.context.compaction import CompactionConfig
from fluid.context.compiler import PromptInputs, compile_prompt
from fluid.context.environment import (
    ENVIRONMENT_DETAILS_HEADER,
    ENVIRONMENT_NOTE_HEADER,
    environment_note_parts,
)
from fluid.context.trim import estimate_tokens_of_messages
from fluid.engine.native import NativeLoop, _add_pinned_block
from fluid.events import Resume, UserTurn
from fluid.messages import Message, Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry

_OPEN = "<system" + "-reminder>\n"
_CLOSE = "\n</system" + "-reminder>"
_MARKER = _OPEN + ENVIRONMENT_DETAILS_HEADER


class ScriptedProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(len(self.requests), len(self.scripts) - 1)]
        self.requests.append(copy.deepcopy(request))
        # Bill what the request really holds, so the compactor's measured
        # scale stays near 1 as it would against a live provider.
        billed = estimate_tokens_of_messages(request.messages) + len(str(request.system or "")) // 4
        for chunk in script:
            if chunk.kind == "message_start":
                chunk = StreamChunk("message_start", {"usage": {"input_tokens": billed}})
            yield chunk


def _call(tool_name: str, tuid: str, args: dict) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_name": f"mcp__wx__{tool_name}", "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _weather_server():
    @tool("get_weather", "Get weather", {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    @tool("dump", "Dump a log", {"type": "object", "properties": {}})
    async def dump(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "log " * 10_000}]}

    return create_inproc_server("wx", [get_weather, dump])


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=repo, check=True)


_UNPINNED = replace(CompactionConfig(), pinned_blocks=("# Harness scratch\n",))


def _loop(repo: Path, provider: ScriptedProvider, store: MemoryStore, sid: str, *,
          include_env: bool = True, **kwargs) -> NativeLoop:
    compiled = compile_prompt(PromptInputs(
        base="b", cwd=str(repo), include_env=include_env,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
    ))
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    return NativeLoop(provider=provider, store=store, model="m", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"),
                      system=compiled.text, system_sections=compiled,
                      ctx=ToolCtx(cwd=str(repo), session_id=sid), **kwargs)


# About 15k tokens in blocks small enough to stay whole until a fold.
_LARGE_FIRST_TURN = [Text("first"), *(Text(c * 20_000) for c in "xyz")]


def _small_window(compaction: CompactionConfig | None = None) -> dict:
    """A real compactor in a small window: turn 1 sits outside the recency guard."""
    base = compaction or CompactionConfig()
    return {"context_window": 32_000, "max_tokens": 8_000,
            "compaction": replace(base, keep_recent_turns=1)}


async def _session() -> tuple[MemoryStore, str]:
    store = MemoryStore()
    return store, (await store.create_session()).session_id


async def _turn(loop: NativeLoop, op: UserTurn | Resume) -> None:
    await loop.submit(op)
    await asyncio.wait_for(loop.wait_turn(), timeout=20)


def _details(repo: Path) -> str:
    return environment_note_parts(repo)[1]


def _blocks(messages: list[Message]) -> list[str]:
    """Every details block in ``messages``, oldest first, verbatim."""
    found = []
    for message in messages:
        text = "\n".join(b.text for b in message.content if isinstance(b, Text))
        start = text.find(_MARKER)
        while start >= 0:
            end = text.find(_CLOSE, start)
            assert end >= 0, "a details block lost its closing tag"
            found.append(text[start:end + len(_CLOSE)])
            start = text.find(_MARKER, end)
    return found


def _assert_holds(request: ProviderRequest, details: str) -> None:
    """The request shows the current details: its newest block is exactly them."""
    blocks = _blocks(request.messages)
    assert blocks and blocks[-1] == details, "the request lacks the current details"


def _sent(request: ProviderRequest) -> bool:
    """The turn's own message carries a details block."""
    return bool(_blocks(request.messages[-1:]))


# ─── pure parts ──────────────────────────────────────────────────────────────


def test_the_compactor_pins_the_details_header() -> None:
    assert ENVIRONMENT_DETAILS_HEADER in CompactionConfig().pinned_blocks


def test_split_note_is_no_larger_than_the_single_note_it_replaces(git_repo: Path) -> None:
    """On a turn that sends the details, the two blocks together are no
    larger than base's one note; on any other turn only the short note goes."""
    note, details = environment_note_parts(git_repo)
    base_header = ("Environment as of this message (the system prompt's environment "
                   "section holds the facts that do not change):\n")
    body = note[len(_OPEN + "Environment as of this message:\n"):-len(_CLOSE)]
    base = f"{_OPEN}{base_header}{body}\n{details[len(_MARKER):]}"
    assert len(note) + len(details) <= len(base)


def test_the_block_joins_the_hosts_pinned_range() -> None:
    memory = _OPEN + "# Memory\n- m1" + _CLOSE
    block = _MARKER + "Worker Python: x" + _CLOSE
    prompt = f"hello\n\n{memory}"
    message = Message(role="user", content=[Text(prompt), Text("tail")],
                      context_pin=(len("hello\n\n"), len(prompt)))
    _add_pinned_block(message, block, 2)
    assert [b.text for b in message.content] == [prompt, block, "tail"]
    assert message.pinned_text() == memory + block
    # No range: the block is the range, where the caller put it.
    alone = Message(role="user", content=[Text("hi"), Text("note")])
    _add_pinned_block(alone, block, 2)
    assert alone.pinned_text() == block and alone.content[2].text == block
    # A range ending inside a text splits it there: the block is pinned and
    # the unowned rest stays outside the range.
    inner = Message(role="user", content=[Text(prompt + "trailing owner text")],
                    context_pin=(len("hello\n\n"), len(prompt)))
    _add_pinned_block(inner, block, 1)
    assert [b.text for b in inner.content] == [prompt, block, "trailing owner text"]
    assert inner.pinned_text() == memory + block
    assert inner.text() == prompt + block + "trailing owner text"


# ─── producing path ──────────────────────────────────────────────────────────


async def test_unchanged_details_ride_once_and_a_commit_brings_them_back(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    first = _details(git_repo)
    await _turn(loop, UserTurn(content="second"))
    _commit(git_repo, "later")
    await _turn(loop, UserTurn(content="third"))
    one, two, three = provider.requests
    assert _sent(one) and not _sent(two) and _sent(three)
    assert "Current branch: main" in two.messages[-1].text()
    assert "Current time:" in two.messages[-1].text()
    _assert_holds(one, first)
    _assert_holds(two, first)
    _assert_holds(three, _details(git_repo))
    assert "later" in _blocks(three.messages)[-1]


async def test_details_under_a_pin_ending_mid_text_are_pinned_so_a_revert_is_resent(
    git_repo: Path,
) -> None:
    """A -> B -> A: B arrives on a turn whose host pin ends inside a Text.
    B must be pinned there, so the later A is seen as a change and resent
    instead of leaving the stale B as the newest block in view."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    details_a = _details(git_repo)
    python = git_repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    details_b = _details(git_repo)
    assert details_b != details_a
    memory = _OPEN + "# Memory\n- [m1 r1; session] A synthetic fact" + _CLOSE
    prompt = f"second\n\n{memory}"
    await _turn(loop, UserTurn(content=prompt + "\ntrailing host text",
                               context_pin=(len("second\n\n"), len(prompt))))
    python.unlink()
    await _turn(loop, UserTurn(content="third"))
    one, two, three = provider.requests
    assert _sent(one) and _sent(two) and _sent(three)
    _assert_holds(two, details_b)
    _assert_holds(three, details_a)
    assert "none in venv/.venv" in _blocks(three.messages)[-1]
    stored = (await store.load_path(sid))[2].message
    pinned = stored.pinned_text()
    assert pinned.startswith(memory) and pinned.endswith(details_b)
    note = pinned[len(memory):-len(details_b)]
    assert note.startswith(_OPEN + ENVIRONMENT_NOTE_HEADER) and note.endswith(_CLOSE)
    assert "trailing host text" not in pinned


async def test_a_real_fold_between_two_calls_keeps_the_details_in_view(git_repo: Path) -> None:
    """Review S1: a large tool result in turn 2 makes the real compactor
    digest turn 1, the only copy, between two calls of one turn. The digest
    carries the pinned block verbatim, and nothing is appended to restore it."""
    store, sid = await _session()
    provider = ScriptedProvider([
        _text("one"),
        _call("dump", "t1", {}), _call("get_weather", "t2", {"city": "Rome"}), _text("two"),
        _call("get_weather", "t3", {"city": "Lima"}), _text("three"),
    ])
    loop = _loop(git_repo, provider, store, sid, **_small_window())
    await _turn(loop, UserTurn(content=_LARGE_FIRST_TURN))
    await _turn(loop, UserTurn(content="second"))
    await _turn(loop, UserTurn(content="third"))

    requests = provider.requests
    assert len(requests) == 6
    details = _details(git_repo)
    folded = requests[2].messages[0].text()
    assert folded.startswith("[compacted turn") and details in folded
    for request in requests:
        _assert_holds(request, details)
        assert len(_blocks(request.messages)) == 1  # one copy, never repeated
    assert [_sent(r) for r in requests] == [True, False, False, False, False, False]
    # Append-only within a turn once the fold is decided: no cached byte moves.
    for before, after in pairwise(requests[2:4]):
        assert after.messages[:len(before.messages)] == before.messages


async def test_a_newer_block_never_rewrites_an_older_fold(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("one"), _call("dump", "t1", {}), _text("two"), _text("three")])
    loop = _loop(git_repo, provider, store, sid, **_small_window())
    await _turn(loop, UserTurn(content=_LARGE_FIRST_TURN))
    await _turn(loop, UserTurn(content="second"))
    old = _details(git_repo)
    _commit(git_repo, "bee")
    await _turn(loop, UserTurn(content="third"))
    folded_before = provider.requests[2].messages[0]
    last = provider.requests[-1]
    assert last.messages[0] == folded_before  # the fold keeps the copy it carried
    assert _blocks(last.messages) == [old, _details(git_repo)]
    _assert_holds(last, _details(git_repo))


async def test_s8_tight_turn_after_a_fold_keeps_its_note_and_its_blocks(git_repo: Path) -> None:
    """Review S8: normal compaction folds turn 1, which holds the only copy,
    and turn 2 nearly fills the admission limit. The fold carries the block;
    no second admission pass runs, and turn 2 goes out whole."""
    async def run(compaction: CompactionConfig) -> ProviderRequest:
        store, sid = await _session()
        provider = ScriptedProvider([_text("one"), _text("two")])
        loop = _loop(git_repo, provider, store, sid, **_small_window(compaction))
        await _turn(loop, UserTurn(content=_LARGE_FIRST_TURN))
        await _turn(loop, UserTurn(content=[Text(c * 24_000) for c in "abc"] + [Text("d" * 9_300)]))
        return provider.requests[-1]

    pinned, control = await run(CompactionConfig()), await run(_UNPINNED)
    for request in (pinned, control):
        sent = request.messages[-1].text()
        assert "d" * 9_300 in sent and "Environment as of this message:" in sent
        _assert_holds(request, _details(git_repo))
    assert request.messages[0].text().startswith("[compacted turn")
    assert not _sent(pinned) and _sent(control)  # the copy rides in the fold instead
    assert estimate_tokens_of_messages(pinned.messages) <= estimate_tokens_of_messages(control.messages)


async def test_s6_first_turn_near_the_limit_is_sent_as_the_control_sends_it(git_repo: Path) -> None:
    """Review S6: nothing is reserved, so a first turn just under the
    admission limit is admitted, digested or kept exactly as with the block
    unpinned, at every size across the boundary."""
    async def run(compaction: CompactionConfig, last: int) -> ProviderRequest:
        store, sid = await _session()
        provider = ScriptedProvider([_text("ok")])
        loop = _loop(git_repo, provider, store, sid, context_window=32_000, max_tokens=8192,
                     compaction=compaction)
        blocks = [Text("a" * 24_000), Text("b" * 24_000), Text("c" * 24_000), Text("d" * last)]
        await _turn(loop, UserTurn(content=blocks))
        return provider.requests[-1]

    outcomes = set()
    for last in range(21_600, 23_600, 200):
        pinned, control = await run(CompactionConfig(), last), await run(_UNPINNED, last)
        intact = "d" * last in pinned.messages[-1].text()
        assert intact == ("d" * last in control.messages[-1].text()), last
        # Kept: byte-identical to the control. Digested: the digest still
        # carries the pinned block, which the control loses (#1656).
        _assert_holds(pinned, _details(git_repo))
        if intact:
            assert pinned.messages == control.messages, last
        else:
            assert not _blocks(control.messages), last
        outcomes.add(intact)
    assert outcomes == {True, False}  # the sweep crossed the admission boundary


async def test_an_oversized_open_turn_digest_keeps_the_details(git_repo: Path) -> None:
    """#1656: admission digests a first turn too large to admit. The digest
    carries the pinned block, where the unpinned control loses it."""
    async def run(compaction: CompactionConfig) -> ProviderRequest:
        store, sid = await _session()
        provider = ScriptedProvider([_text("ok")])
        loop = _loop(git_repo, provider, store, sid, context_window=32_000, max_tokens=8192,
                     compaction=compaction)
        blocks = [Text("a" * 24_000), Text("b" * 24_000), Text("c" * 24_000), Text("d" * 24_000)]
        await _turn(loop, UserTurn(content=blocks))
        return provider.requests[-1]

    pinned, control = await run(CompactionConfig()), await run(_UNPINNED)
    assert "d" * 24_000 not in pinned.messages[-1].text()  # the turn was digested
    _assert_holds(pinned, _details(git_repo))
    assert not _blocks(control.messages)


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("include_env", [False, True])
async def test_admission_keeps_one_note_per_turn(git_repo: Path, oversized: bool, include_env: bool) -> None:
    """#1656: real intake, compactor, admission and two provider calls, then
    a later turn. Small blocks bypass intake stubs but exceed admission."""
    store, sid = await _session()
    provider = ScriptedProvider([_call("get_weather", "t1", {"city": "Oslo"}), _text("ok")])
    loop = _loop(git_repo, provider, store, sid, include_env=include_env,
                 context_window=32_000, max_tokens=8192)
    content = [Text(c * 24_000) for c in "abcd"] if oversized else "hello"
    await _turn(loop, UserTurn(content=content))
    first, followup = provider.requests
    sent = "\n".join(m.text() for m in first.messages)
    header = ENVIRONMENT_NOTE_HEADER if include_env else "Runtime facts as of this message ("
    assert sent.count(header) == 1
    assert sent.count("Worker Python:") == 1
    assert any(m.text().startswith("[compacted turn") for m in first.messages) == oversized
    if include_env:
        assert "Current time:" in sent and "Current branch: main" in sent
    assert followup.messages[:len(first.messages)] == first.messages
    await _turn(loop, UserTurn(content="later"))
    later = provider.requests[-1]
    # One per user turn, or one held runtime-only block for the whole path.
    assert sum(m.text().count(header) for m in later.messages) == (2 if include_env else 1)
    assert sum(m.text().count("Worker Python:") for m in later.messages) == 1
    assert later.messages[:len(followup.messages)] == followup.messages


@pytest.mark.parametrize("include_env", [False, True])
async def test_legacy_resume_pins_environment_before_admission(git_repo: Path, include_env: bool) -> None:
    """S8: a failed pre-pinning turn, with a compiled environment section
    too, must not lose its replacement note when Resume digests its anchor.
    Two failed Resumes also must not turn a harness note into user intent."""
    store, sid = await _session()
    failed = [StreamChunk("error", {"message": "scripted failure", "retriable": False})]
    provider = ScriptedProvider([failed, failed, _text("ok")])
    asked = "Please run `cat README.md` for me"
    loop = _loop(git_repo, provider, store, sid, include_env=include_env,
                 context_window=200_000, max_tokens=8192)
    await _turn(loop, UserTurn(content=[Text(asked), *(Text(c * 24_000) for c in "abcd")]))
    for node in store._nodes[sid].values():
        node.message.context_pin = None  # legacy metadata, not a seeded consumer-only fixture
    header = ENVIRONMENT_NOTE_HEADER if include_env else "Runtime facts as of this message ("
    for _ in range(2):
        loop = _loop(git_repo, provider, store, sid, include_env=include_env,
                     context_window=32_000, max_tokens=8192)
        await _turn(loop, Resume())
        request = provider.requests[-1]
        assert request.messages[0].text().startswith("[compacted turn")
        assert sum(m.text().count(header) for m in request.messages) == 1
        assert sum(m.text().count("Worker Python:") for m in request.messages) == 1
        assert loop.ctx.extra["bash_file_guard_user_text"].startswith(asked)
    assert provider.requests[-1].messages == provider.requests[-2].messages
    await _turn(loop, UserTurn(content="next"))
    assert sum(m.text().count("Worker Python:") for m in provider.requests[-1].messages) == 1


@pytest.mark.parametrize("include_env", [False, True])
@pytest.mark.parametrize("old_repair", [False, True])
async def test_repeated_legacy_resumes_keep_the_original_hook_prompt(
    git_repo: Path, include_env: bool, old_repair: bool,
) -> None:
    """S1: a real UserPromptSubmit gate must accept all three Resumes,
    even when an older transcript ends in a harness-only repair."""
    from fluid.events import TurnStarted
    from fluid.hooks.bus import TURN_START, HookBus, HookDecision

    store, sid = await _session()
    failed = [StreamChunk("error", {"message": "scripted failure", "retriable": False})]
    provider = ScriptedProvider([failed, failed, failed, _text("ok")])
    loop = _loop(git_repo, provider, store, sid, include_env=include_env,
                 context_window=200_000, max_tokens=8192)
    await _turn(loop, UserTurn(content=[Text("original request"), *(Text(c * 24_000) for c in "abcd")]))
    (original,) = await store.load_path(sid)
    original.message.context_pin = None  # pre-pinning transcript, produced by the real loop
    if old_repair:
        note = environment_note_parts(git_repo)[0]
        await store.append(sid, SessionNode(
            node_id="old-repair", parent_id=original.node_id,
            message=Message(role="user", content=[Text(note)],
                            provider_meta={"harness_note": True}).pin_context(note),
        ))
    prompts = []
    hooks = HookBus()

    async def require_original(event, payload):
        prompts.append(payload["prompt"])
        return HookDecision(action="allow" if payload["prompt"] == original.message.text() else "deny")

    hooks.on(TURN_START, require_original)
    for count in range(1, 4):
        loop = _loop(git_repo, provider, store, sid, include_env=include_env, hooks=hooks,
                     context_window=32_000, max_tokens=8192)
        await _turn(loop, Resume())
        assert len(provider.requests) == count + 1  # the hook did not block this Resume
        assert prompts == [original.message.text()] * count
        starts = [e for e in await store.events_since(sid, 0) if isinstance(e, TurnStarted)]
        assert starts[-1].node_id == original.node_id
        assert provider.requests[-1].messages[0].text().startswith("[compacted turn")
        assert sum(m.text().count("Worker Python:") for m in provider.requests[-1].messages) == 1
    assert provider.requests[-1].messages == provider.requests[-2].messages


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("old_repair", [False, True])
async def test_resume_clock_is_fresh_last_and_not_stored(git_repo: Path, oversized: bool, old_repair: bool) -> None:
    """S2: advancing clocks survive admission without accumulating repairs,
    including a transcript imported from the earlier repair-node design."""
    from datetime import UTC, datetime

    store, sid = await _session()
    failed = [StreamChunk("error", {"message": "scripted failure", "retriable": False})]
    provider = ScriptedProvider([failed])
    loop = _loop(git_repo, provider, store, sid, context_window=200_000, max_tokens=8192)
    loop.ctx.extra["time_context"] = lambda _: {"now": datetime(2026, 11, 14, tzinfo=UTC), "zone": "UTC"}
    content = [Text(c * 24_000) for c in "abcd"] if oversized else "original request"
    await _turn(loop, UserTurn(content=content))
    (original,) = await store.load_path(sid)
    original.message.context_pin = None  # pre-pinning transcript
    if old_repair:
        note, details = environment_note_parts(git_repo, time_context={
            "now": datetime(2026, 11, 13, tzinfo=UTC), "zone": "UTC",
        })
        block = note + "\n\n" + details
        await store.append(sid, SessionNode(
            node_id="old-repair", parent_id=original.node_id,
            message=Message(role="user", content=[Text(block)],
                            provider_meta={"harness_note": True}).pin_context(block),
        ))
    stored = copy.deepcopy(await store.load_path(sid))
    for day in (15, 16, 17):
        loop = _loop(git_repo, provider, store, sid, context_window=32_000, max_tokens=8192)
        loop.ctx.extra["time_context"] = lambda _, day=day: {
            "now": datetime(2026, 11, day, tzinfo=UTC), "zone": "UTC",
        }
        await _turn(loop, Resume())
        sent = "\n".join(m.text() for m in provider.requests[-1].messages)
        dates = [int(line.split("Nov ")[1].split(",")[0])
                 for line in sent.splitlines() if line.startswith("Current time:")]
        assert dates[-1] == day and dates.count(day) == 1
        if oversized:
            assert any(m.text().startswith("[compacted turn") for m in provider.requests[-1].messages)
            assert dates == ([13, day] if old_repair else [day])
        assert await store.load_path(sid) == stored  # no new node and no rewritten history


async def test_runtime_facts_follow_the_selected_path_and_survive_a_wake(git_repo: Path) -> None:
    store, sid = await _session()
    await store.append(sid, SessionNode(
        node_id="root", parent_id=None,
        message=Message(role="user", content=[Text("legacy root")]),
    ))
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid, include_env=False)
    await _turn(loop, UserTurn(content="first"))
    # A fresh engine must use the held pin, not re-send a duplicate.
    loop = _loop(git_repo, provider, store, sid, include_env=False)
    await _turn(loop, UserTurn(content="wake"))
    assert "Worker Python:" not in provider.requests[-1].messages[-1].text()
    # The fork parent never held the facts: engine memory must not suppress them.
    await _turn(loop, UserTurn(content="fork", parent_node_id="root"))
    assert sum(m.text().count("Worker Python:") for m in provider.requests[-1].messages) == 1
    assert "Worker Python:" in provider.requests[-1].messages[-1].text()


async def test_runtime_only_resume_over_held_facts_is_byte_identical(git_repo: Path) -> None:
    store, sid = await _session()
    failed = [StreamChunk("error", {"message": "scripted failure", "retriable": False})]
    provider = ScriptedProvider([failed, _text("ok")])
    await _turn(_loop(git_repo, provider, store, sid, include_env=False), UserTurn(content="first"))
    stored = len(store._nodes[sid])
    await _turn(_loop(git_repo, provider, store, sid, include_env=False), Resume())
    assert provider.requests[-1].messages == provider.requests[-2].messages
    assert len(store._nodes[sid]) == stored + 1  # only the assistant reply


async def test_an_overflow_escalation_keeps_the_details_in_view(git_repo: Path) -> None:
    """The provider rejects turn 2's second call; the real overflow
    escalation digests turn 1, the only copy, and the digest carries it."""
    store, sid = await _session()
    overflow = [StreamChunk("error", {"message": "prompt is too long", "retriable": False})]
    provider = ScriptedProvider([_text("one"), _call("get_weather", "t1", {"city": "Oslo"}),
                                 overflow, _text("two")])
    loop = _loop(git_repo, provider, store, sid, compaction=CompactionConfig(keep_recent_turns=1))
    await _turn(loop, UserTurn(content=_LARGE_FIRST_TURN))
    await _turn(loop, UserTurn(content="second"))

    requests = provider.requests
    assert len(requests) == 4
    folded = requests[3].messages[0].text()
    assert folded.startswith("[compacted turn")
    for request in requests:
        _assert_holds(request, _details(git_repo))
        assert len(_blocks(request.messages)) == 1


async def test_a_rewind_reads_its_own_path_not_the_old_head(git_repo: Path) -> None:
    """Review S2: a fresh engine at a head holding details B, rewinding onto a
    turn that only ever saw A, sends B."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))  # A
    reply_a = (await store.load_path(sid))[-1].node_id
    _commit(git_repo, "bee")
    await _turn(loop, UserTurn(content="second"))  # B
    await _turn(loop, UserTurn(content="third"))  # held
    assert not _sent(provider.requests[2])

    fresh = _loop(git_repo, provider, store, sid)
    await _turn(fresh, UserTurn(content="redo", parent_node_id=reply_a))
    rewound = provider.requests[-1]
    assert _sent(rewound)
    _assert_holds(rewound, _details(git_repo))


async def test_a_rewind_onto_matching_details_relies_on_them(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    reply = (await store.load_path(sid))[-1].node_id
    venv = git_repo / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("", encoding="utf-8")  # runtime facts change
    await _turn(loop, UserTurn(content="second"))
    assert _sent(provider.requests[1]) and "venv" in _blocks(provider.requests[1].messages)[-1]
    (venv / "python").unlink()  # and change back
    await _turn(loop, UserTurn(content="redo", parent_node_id=reply))
    assert not _sent(provider.requests[-1])
    _assert_holds(provider.requests[-1], _details(git_repo))


async def test_a_fork_before_any_block_sends_the_details(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    root = SessionNode(node_id="root", parent_id=None,
                       message=Message(role="user", content=[Text("imported, no note")]))
    reply = SessionNode(node_id="reply", parent_id="root",
                        message=Message(role="assistant", content=[Text("ok")]))
    await store.append(sid, root)
    await store.append(sid, reply)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))
    assert not _sent(provider.requests[-1])
    await _turn(loop, UserTurn(content="fork", parent_node_id="reply"))
    assert _sent(provider.requests[-1])
    _assert_holds(provider.requests[-1], _details(git_repo))


async def test_a_base_format_note_is_not_taken_as_held(git_repo: Path) -> None:
    """A session stored before the split holds one unpinned note with the
    details inside: the next turn sends the block once, then relies on it."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    _, details = environment_note_parts(git_repo)
    old_note = (_OPEN + "Environment as of this message (the system prompt's environment "
                "section holds the facts that do not change):\nCurrent branch: main\n"
                + details[len(_MARKER):])
    await store.append(sid, SessionNode(node_id="old", parent_id=None,
                                        message=Message(role="user", content=[Text("hi"), Text(old_note)])))
    await store.append(sid, SessionNode(node_id="old-reply", parent_id="old",
                                        message=Message(role="assistant", content=[Text("ok")])))
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))
    assert [_sent(r) for r in provider.requests] == [True, False]
    _assert_holds(provider.requests[-1], details)


async def test_a_resume_note_is_never_counted_as_held(git_repo: Path) -> None:
    """Review S3: Resume shows details B in a request-only note; the next turn
    sends B because stored history holds only A."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))  # stored A
    head = (await store.load_path(sid))[-1]
    await store.append(sid, SessionNode(node_id="notice", parent_id=head.node_id,
                                        message=Message(role="user", content=[Text("a peer notice")])))
    _commit(git_repo, "bee")
    await _turn(loop, Resume())
    _assert_holds(provider.requests[-1], _details(git_repo))
    stored = "".join(n.message.text() for n in await store.load_path(sid))
    assert "bee" not in stored  # the Resume note was request-only
    await _turn(loop, UserTurn(content="next"))
    assert _sent(provider.requests[-1])
    _assert_holds(provider.requests[-1], _details(git_repo))


async def test_a_fresh_engine_with_other_runtime_facts_sends_them(git_repo: Path) -> None:
    """A hand-off to another worker: same session, different runtime facts."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="first"))
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="second"))
    assert not _sent(provider.requests[-1])  # a fresh engine reads the stored path
    (git_repo / "package.json").write_text("{}", encoding="utf-8")
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="third"))
    assert _sent(provider.requests[-1])
    assert "Node dependencies" in _blocks(provider.requests[-1].messages)[-1]
    _assert_holds(provider.requests[-1], _details(git_repo))


async def test_an_unpinning_compactor_sends_the_details_every_turn(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid, compaction=_UNPINNED)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))
    assert [_sent(r) for r in provider.requests] == [True, True]
