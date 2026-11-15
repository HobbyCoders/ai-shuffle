"""The environment note's details ride only when the context lacks them.

The date, branch and working tree go with every turn. Recent commits and the
runtime facts (about 1.2k characters) are replaced by a fingerprinted
"unchanged" line when, and only when, the compacted view of the path the
turn extends shows an identical block. Every decision is read from the view:
a fold between calls is repaired by a stored, append-only harness note; a
fork, rewind, Resume or fresh engine reads its own path.

Each test drives real turns through a scripted provider and checks every
request actually sent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from collections.abc import AsyncIterator
from itertools import pairwise
from pathlib import Path

import pytest
from fluid.context.compiler import PromptInputs, compile_prompt
from fluid.context.environment import (
    ENVIRONMENT_DETAILS_UNCHANGED,
    ENVIRONMENT_NOTE_HEADER,
    details_fingerprint,
    environment_note_parts,
    render_environment_note,
    scan_environment_notes,
    unsupported_environment_claim,
)
from fluid.engine.native import NativeLoop, _context_texts, _plan_in_view
from fluid.events import Resume, UserTurn
from fluid.messages import Message, Text
from fluid.messages.content import (
    HARNESS_NOTE_META_KEY,
    PLAN_REVISION_META_KEY,
    is_harness_note,
)
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry


class ScriptedProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(len(self.requests), len(self.scripts) - 1)]
        self.requests.append(copy.deepcopy(request))
        for chunk in script:
            yield chunk


def _tool_call(tuid: str, city: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_name": "mcp__wx__get_weather", "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({"city": city})}),
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

    return create_inproc_server("wx", [get_weather])


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


def _loop(repo: Path, provider: ScriptedProvider, store: MemoryStore, sid: str) -> NativeLoop:
    compiled = compile_prompt(PromptInputs(
        base="b", cwd=str(repo), include_env=True,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
    ))
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    return NativeLoop(provider=provider, store=store, model="m", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"),
                      system=compiled.text, system_sections=compiled,
                      ctx=ToolCtx(cwd=str(repo), session_id=sid))


async def _session() -> tuple[MemoryStore, str]:
    store = MemoryStore()
    return store, (await store.create_session()).session_id


async def _turn(loop: NativeLoop, op: UserTurn | Resume) -> None:
    await loop.submit(op)
    await asyncio.wait_for(loop.wait_turn(), timeout=20)


def _details(repo: Path) -> str:
    return environment_note_parts(repo)[1]


def _restored(messages: list[Message]) -> list[Message]:
    return [m for m in messages if is_harness_note(m) and ENVIRONMENT_NOTE_HEADER in m.text()]


def _assert_every_claim_is_backed(requests: list[ProviderRequest]) -> None:
    """Each request that says "unchanged" shows the exact block it names,
    and carries at most one restored note."""
    for index, request in enumerate(requests):
        texts = _context_texts(request.messages)
        assert not unsupported_environment_claim(texts), f"request {index} claims unseen details"
        assert len(_restored(request.messages)) <= 1, f"request {index} repeats a restored note"


def _claims(request: ProviderRequest) -> int:
    """"Unchanged" lines in the request's newest message (the turn's own note)."""
    return request.messages[-1].text().count(ENVIRONMENT_DETAILS_UNCHANGED)


# ─── pure parts ──────────────────────────────────────────────────────────────


def test_the_scan_compares_the_whole_details_block(git_repo: Path) -> None:
    lines, details = environment_note_parts(git_repo)
    full = render_environment_note(lines, details)
    claim = render_environment_note(lines, details, unchanged=True)
    assert f"[{details_fingerprint(details)}]" in claim and "Worker Python:" not in claim
    assert scan_environment_notes([full, "work", claim]) == (details, details_fingerprint(details))
    assert not unsupported_environment_claim([full, claim])
    assert unsupported_environment_claim([claim])  # the copy was folded away
    # Review S4: without git the details are only the runtime facts, a
    # suffix of the old block. A suffix is not the block.
    facts_only = details[details.index("Worker Python: "):]
    assert facts_only != details
    assert unsupported_environment_claim([full, render_environment_note(lines, facts_only, unchanged=True)])
    # A note cut before its closing tag proves nothing.
    assert unsupported_environment_claim([full[:-40], claim])
    # A newer full note supersedes an older claim.
    assert not unsupported_environment_claim([claim, full])


def test_a_restored_environment_note_is_not_taken_for_a_plan_note() -> None:
    plan_text = "# Session plan (revision 2; ...)"
    plan_note = Message(role="user", content=[Text(plan_text)],
                        provider_meta={HARNESS_NOTE_META_KEY: True, PLAN_REVISION_META_KEY: "r2"})
    env_note = Message(role="user", content=[Text(ENVIRONMENT_NOTE_HEADER + "Worker Python: x")],
                       provider_meta={HARNESS_NOTE_META_KEY: True})
    # Otherwise every restore would make the plan look stale and store it again.
    assert _plan_in_view([plan_note, env_note], plan_text, "r2")


# ─── producing path ──────────────────────────────────────────────────────────


async def test_unchanged_details_ride_once_and_a_commit_brings_them_back(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("one"), _text("two"), _text("three")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))
    _commit(git_repo, "later")
    await _turn(loop, UserTurn(content="third"))
    first, second, third = (r.messages[-1].text() for r in provider.requests)
    assert "Worker Python:" in first and _claims(provider.requests[0]) == 0
    assert "Worker Python:" not in second and ENVIRONMENT_DETAILS_UNCHANGED in second
    assert "Current branch: main" in second and "Current time:" in second
    assert "later" in third and "Worker Python:" in third
    _assert_every_claim_is_backed(provider.requests)


async def test_a_fold_between_two_calls_restores_the_details_once(git_repo: Path, monkeypatch) -> None:
    """Review S1: a compaction inside the turn removed the only full copy
    while the turn's own "unchanged" line stayed in view."""
    store, sid = await _session()
    provider = ScriptedProvider([
        _text("one"),
        _tool_call("t1", "Oslo"), _tool_call("t2", "Rome"), _text("two"),
        _tool_call("t3", "Lima"), _text("three"),
    ])
    loop = _loop(git_repo, provider, store, sid)
    real_apply = loop._compactor.apply
    folded: set[str] = set()
    applies = 0

    def apply(nodes, **kwargs):
        # Before turn 2's second call, fold turn 1 (which holds the full
        # note) into a summary, as a real digest would.
        nonlocal applies
        applies += 1
        rendered, promotions = real_apply(nodes, **kwargs)
        if applies == 3:
            folded.update(n.node_id for n in nodes[:2])
        if not folded:
            return rendered, promotions
        kept = [n.message for n in nodes if n.node_id not in folded]
        return [Message(role="user", content=[Text("[summary of earlier work]")]), *kept], promotions

    monkeypatch.setattr(loop._compactor, "apply", apply)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))
    await _turn(loop, UserTurn(content="third"))

    requests = provider.requests
    assert len(requests) == 6
    assert _claims(requests[1]) == 1  # turn 2 relied on turn 1's copy
    restored = _restored(requests[2].messages)
    assert len(restored) == 1
    assert _details(git_repo) in restored[0].text()
    assert requests[2].messages[-1].text() == restored[0].text()  # appended, nothing patched
    _assert_every_claim_is_backed(requests)
    # Written once: later calls and the next turn find the restored copy.
    stored = [n.message for n in await store.load_path(sid)]
    assert len(_restored(stored)) == 1
    assert _claims(requests[4]) == 1 and len(_restored(requests[5].messages)) == 1
    # The fold rewrote history once; from then on each request extends the last.
    for before, after in pairwise(requests[2:]):
        assert after.messages[:len(before.messages)] == before.messages


async def test_a_rewind_reads_its_own_path_not_the_old_head(git_repo: Path) -> None:
    """Review S2: a fresh engine at a head holding details B, rewinding onto a
    turn that only ever saw A, must send B in full."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))  # full A
    reply_a = (await store.load_path(sid))[-1].node_id
    _commit(git_repo, "bee")
    await _turn(loop, UserTurn(content="second"))  # full B
    await _turn(loop, UserTurn(content="third"))  # unchanged (B)
    assert _claims(provider.requests[2]) == 1

    fresh = _loop(git_repo, provider, store, sid)
    await _turn(fresh, UserTurn(content="redo", parent_node_id=reply_a))
    rewound = provider.requests[-1]
    assert "bee" in rewound.messages[-1].text() and _claims(rewound) == 0
    _assert_every_claim_is_backed(provider.requests)


async def test_a_rewind_onto_matching_details_may_rely_on_them(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    reply = (await store.load_path(sid))[-1].node_id
    venv = git_repo / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("", encoding="utf-8")  # runtime facts change
    await _turn(loop, UserTurn(content="second"))
    assert "venv" in provider.requests[1].messages[-1].text()
    (venv / "python").unlink()  # and change back
    await _turn(loop, UserTurn(content="redo", parent_node_id=reply))
    assert _claims(provider.requests[-1]) == 1
    _assert_every_claim_is_backed(provider.requests)


async def test_a_fork_before_any_full_note_sends_the_details(git_repo: Path) -> None:
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
    assert _claims(provider.requests[-1]) == 1
    await _turn(loop, UserTurn(content="fork", parent_node_id="reply"))
    assert "Worker Python:" in provider.requests[-1].messages[-1].text()
    _assert_every_claim_is_backed(provider.requests)


async def test_a_resume_note_is_never_counted_as_held(git_repo: Path) -> None:
    """Review S3: Resume shows details B in a request-only note; the next turn
    must not call B unchanged when stored history holds only A."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))  # stored full A
    head = (await store.load_path(sid))[-1]
    await store.append(sid, SessionNode(node_id="notice", parent_id=head.node_id,
                                        message=Message(role="user", content=[Text("a peer notice")])))
    _commit(git_repo, "bee")
    await _turn(loop, Resume())
    resumed = provider.requests[-1]
    assert "bee" in resumed.messages[-1].text() and _claims(resumed) == 0
    stored = "".join(n.message.text() for n in await store.load_path(sid))
    assert "bee" not in stored  # the Resume note was request-only
    await _turn(loop, UserTurn(content="next"))
    nxt = provider.requests[-1]
    assert "bee" in nxt.messages[-1].text() and "Worker Python:" in nxt.messages[-1].text()
    _assert_every_claim_is_backed(provider.requests)


async def test_a_resume_with_matching_details_says_unchanged(git_repo: Path) -> None:
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    loop = _loop(git_repo, provider, store, sid)
    await _turn(loop, UserTurn(content="first"))
    head = (await store.load_path(sid))[-1]
    await store.append(sid, SessionNode(node_id="notice", parent_id=head.node_id,
                                        message=Message(role="user", content=[Text("a peer notice")])))
    await _turn(_loop(git_repo, provider, store, sid), Resume())
    assert _claims(provider.requests[-1]) == 1
    _assert_every_claim_is_backed(provider.requests)


async def test_a_fresh_engine_with_other_runtime_facts_sends_them(git_repo: Path) -> None:
    """A hand-off to another worker: same session, different runtime facts."""
    store, sid = await _session()
    provider = ScriptedProvider([_text("ok")])
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="first"))
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="second"))
    assert _claims(provider.requests[-1]) == 1  # a fresh engine reads the view
    (git_repo / "package.json").write_text("{}", encoding="utf-8")
    await _turn(_loop(git_repo, provider, store, sid), UserTurn(content="third"))
    last = provider.requests[-1].messages[-1].text()
    assert "Node dependencies" in last and _claims(provider.requests[-1]) == 0
    _assert_every_claim_is_backed(provider.requests)


async def test_an_overflow_escalation_restores_the_details_before_the_retry(
    git_repo: Path, monkeypatch,
) -> None:
    """The overflow retry re-enters the loop top, so the same per-request
    check repairs a fold the escalation made."""
    from fluid.context.compaction import Promotion

    store, sid = await _session()
    overflow = [StreamChunk("error", {"message": "prompt is too long", "retriable": False})]
    provider = ScriptedProvider([_text("one"), _tool_call("t1", "Oslo"), overflow, _text("two")])
    loop = _loop(git_repo, provider, store, sid)
    real_apply = loop._compactor.apply
    folded: set[str] = set()

    def apply(nodes, **kwargs):
        rendered, promotions = real_apply(nodes, **kwargs)
        if not folded:
            return rendered, promotions
        kept = [n.message for n in nodes if n.node_id not in folded]
        return [Message(role="user", content=[Text("[summary of earlier work]")]), *kept], promotions

    real_escalate = loop._compactor.escalate

    def escalate(nodes, **kwargs):
        if len(provider.requests) < 3:  # admission asks too; only the overflow folds
            return real_escalate(nodes, **kwargs)
        folded.update(n.node_id for n in nodes[:2])  # turn 1, with the full note
        return [Promotion(tier=2, reason="overflow")]

    monkeypatch.setattr(loop._compactor, "apply", apply)
    monkeypatch.setattr(loop._compactor, "escalate", escalate)
    await _turn(loop, UserTurn(content="first"))
    await _turn(loop, UserTurn(content="second"))

    requests = provider.requests
    assert len(requests) == 4
    assert _claims(requests[1]) == 1
    assert not _restored(requests[2].messages)  # the rejected call, before the fold
    assert len(_restored(requests[3].messages)) == 1
    assert requests[3].messages[-1].text() == _restored(requests[3].messages)[0].text()
    _assert_every_claim_is_backed(requests)
    assert len(_restored([n.message for n in await store.load_path(sid)])) == 1
