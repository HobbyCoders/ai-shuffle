"""Round-9 audit: the memory learner that never learned.

Pins the fixes for the four findings behind "557 synthesis runs, zero
memories": an empty or truncated completion counted as a clean pass and
advanced the cursor (M1/M2), every friction entry made the next pass urgent
and burned the 48-call daily budget by mid-morning (M5), the journal
deadlocked at its byte cap because append refused lines the tail rule
would never reap (M3/M4), and a fresh install never told the model memory
existed (M6/M7/M8). Plus the mechanical fact harvest that stops sessions
re-discovering "no pytest here" by hand.
"""

from __future__ import annotations

import json
import time

import pytest

from fluid.codemode import SkillStore, Synthesizer
from fluid.codemode import continual as continual_mod
from fluid.codemode import skills as skills_mod
from fluid.codemode.journal import ToolJournal
from fluid.providers.base import StreamChunk
from fluid.tools.builtins import recall as recall_mod
from fluid.tools.builtins import remember as remember_mod
from fluid.tools.registry import ToolCtx


class _ScriptedProvider:
    """Streams ``text`` then a usage chunk carrying ``stop_reason``."""

    name = "fake"

    def __init__(self, text: str, stop_reason: str = "end_turn", output_tokens: int = 7) -> None:
        self._text = text
        self._stop = stop_reason
        self._out = output_tokens
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        if self._text:
            yield StreamChunk(kind="text_delta", payload={"text": self._text})
        yield StreamChunk(kind="usage", payload={
            "usage": {"input_tokens": 100, "output_tokens": self._out},
            "stop_reason": self._stop,
        })
        yield StreamChunk(kind="message_stop")


def _journal(store: SkillStore, n: int, kind: str = "cell") -> None:
    for i in range(n):
        if kind == "cell":
            store.journal_append({"kind": "cell", "code": f"x = {i}", "host_calls": 0})
        else:
            store.journal_append({"kind": kind, "text": f"event {i}"})


def _log_rows(store: SkillStore) -> list[dict]:
    path = store.root / ".synthesis-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ─── 1a: empty / max_tokens completions are failures, not NO_CHANGES ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text,stop", [("", "end_turn"), ("", "max_tokens"), ("<memory>cut off", "max_tokens")])
async def test_empty_or_truncated_synthesis_does_not_advance_cursor(tmp_path, text, stop) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 3)
    synth = Synthesizer(_ScriptedProvider(text, stop), "m", store, min_new_entries=1)
    assert await synth.run() == []
    assert store.synth_cursor() == 0, "an unjudged slice must be retried, not consumed"
    assert synth.last_error and "synthesis returned" in synth.last_error
    row = _log_rows(store)[-1]
    assert row["consumed"] == 0
    assert row["stop_reason"] == stop
    assert row["output_tokens"] == 7
    assert row["output_chars"] == len(text)
    assert store.memories() == []


@pytest.mark.asyncio
async def test_real_no_changes_reply_still_advances(tmp_path) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 3)
    synth = Synthesizer(_ScriptedProvider("NO_CHANGES"), "m", store, min_new_entries=1)
    assert await synth.run() == []
    assert store.synth_cursor() == 3
    row = _log_rows(store)[-1]
    assert row["stop_reason"] == "end_turn" and row["consumed"] == 3


# ─── 1b: effort and output room on the request ───────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_request_carries_low_effort_and_room(tmp_path) -> None:
    store = SkillStore(tmp_path)
    _journal(store, 2)
    provider = _ScriptedProvider("NO_CHANGES")
    synth = Synthesizer(provider, "m", store, min_new_entries=1)
    await synth.run()
    (request,) = provider.requests
    assert request.effort == continual_mod.SYNTH_EFFORT == "low"
    assert request.max_tokens == continual_mod.SYNTH_MAX_TOKENS >= 16_000
    # Overridable per host.
    _journal(store, 2)
    provider2 = _ScriptedProvider("NO_CHANGES")
    await Synthesizer(provider2, "m", store, min_new_entries=1, max_tokens=2048, effort=None).run()
    assert provider2.requests[0].max_tokens == 2048 and provider2.requests[0].effort is None


# ─── 1c: friction no longer makes a pass urgent; threshold is a real slice ───


def test_friction_does_not_mark_urgent(tmp_path) -> None:
    store = SkillStore(tmp_path)
    synth = Synthesizer(object(), "m", store)
    journal = ToolJournal(store, synth)
    for _ in range(3):
        journal.record_tool("Read", {"file_path": "/nope.py"}, "file not found: /nope.py", True)
    kinds = [e["kind"] for e in store.journal_entries()]
    assert "friction" in kinds
    assert not synth.due(), "friction alone must not trigger a pass"
    assert continual_mod.MIN_NEW_ENTRIES >= 20
    _journal(store, continual_mod.MIN_NEW_ENTRIES)
    assert synth.due()


# ─── 1d: budget exhaustion keeps the slice and stops re-kicking ──────────────


@pytest.mark.asyncio
async def test_budget_exhaustion_retains_slice_and_quiets_kicks(tmp_path, monkeypatch) -> None:
    from fluid.codemode import facts as facts_mod

    monkeypatch.setattr(facts_mod, "MAX_INFERENCE_CALLS_PER_DAY", 0)
    store = SkillStore(tmp_path)
    _journal(store, 4)
    provider = _ScriptedProvider("NO_CHANGES")
    synth = Synthesizer(provider, "m", store, min_new_entries=1)
    assert synth.due()
    assert await synth._run_safe() == []
    assert provider.requests == []
    assert store.synth_cursor() == 0
    assert store.journal_count() == 4
    # Reap keeps everything unconsumed even past the byte target.
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", 64)
    assert store.reap_journal() == 0
    assert store.journal_count() == 4
    row = _log_rows(store)[-1]
    assert "budget" in row["error"]
    # The same UTC day: not due again, so the log does not fill with budget rows.
    assert not synth.due()
    assert len(_log_rows(store)) == 1
    # Next day: due again.
    synth._budget_spent_day = int(time.time() // 86400) - 1
    assert synth.due()


# ─── 2: journal deadlock ──────────────────────────────────────────────────────


def test_over_quota_journal_with_consumed_cursor_drains_and_accepts_appends(
    tmp_path, monkeypatch,
) -> None:
    """The desktop shape: every line consumed, file at the cap, tail rule
    keeping all of it. Reap must free it and append must land afterwards."""
    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 500)
    store = SkillStore(tmp_path)
    fat = "y" * 200
    for i in range(20):
        store.journal_append({"kind": "cell", "code": f"{i}:{fat}", "host_calls": 0})
    size = store._journal_size()
    assert store.journal_count() == 20
    store.set_synth_cursor(20)
    # Now pretend the cap is exactly the current size (the deadlock).
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", size)
    # Old behavior: a mechanical append is dropped silently...
    dropped = store.reap_journal()
    assert dropped > 0, "reap must drop consumed lines once over the byte target"
    assert store._journal_size() <= size * skills_mod.JOURNAL_REAP_FRACTION + 250
    assert store.synth_cursor() == 20 - dropped
    store.journal_append({"kind": "remember", "text": "a fact the user asked to keep"})
    kinds = [e["kind"] for e in store.journal_entries()]
    assert kinds[-1] == "remember"


def test_append_reaps_on_its_own_and_raises_for_explicit_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skills_mod, "JOURNAL_KEEP_ENTRIES", 500)
    store = SkillStore(tmp_path)
    fat = "z" * 200
    for i in range(10):
        store.journal_append({"kind": "cell", "code": f"{i}:{fat}", "host_calls": 0})
    size = store._journal_size()
    store.set_synth_cursor(10)
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", size)
    # append itself triggers the reap when the line would cross the cap.
    store.journal_append({"kind": "remember", "text": "kept"})
    assert [e["kind"] for e in store.journal_entries()][-1] == "remember"
    assert store.journal_count() < 11

    # Nothing consumed and no room: mechanical entries drop with a warning,
    # explicit ones raise so the caller can say so.
    store2 = SkillStore(tmp_path / "b")
    for i in range(5):
        store2.journal_append({"kind": "cell", "code": f"{i}:{fat}", "host_calls": 0})
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", store2._journal_size())
    store2.journal_append({"kind": "cell", "code": "dropped", "host_calls": 0})  # no raise
    assert store2.journal_count() == 5
    with pytest.raises(OSError):
        store2.journal_append({"kind": "remember", "text": "must not vanish silently"})


def test_reap_journal_is_safe_on_the_deadlocked_desktop_shape(tmp_path, monkeypatch) -> None:
    """436 lines, cursor 436, 1 MiB: one call drains to under the target and
    rebases the cursor; a second call is a no-op."""
    store = SkillStore(tmp_path)
    line = {"kind": "cell", "code": "c" * 2300, "stdout": "", "host_calls": 0}
    for _ in range(436):
        store.journal_append(line)
    assert store._journal_size() > skills_mod.JOURNAL_MAX_BYTES * skills_mod.JOURNAL_REAP_FRACTION
    store.set_synth_cursor(436)
    dropped = store.reap_journal()
    assert dropped > 0
    assert store._journal_size() <= skills_mod.JOURNAL_MAX_BYTES * skills_mod.JOURNAL_REAP_FRACTION
    assert store.synth_cursor() == 436 - dropped == store.journal_count()
    assert store.reap_journal() == 0


def test_cell_code_is_capped_in_the_journal(tmp_path) -> None:
    from fluid.codemode.session import _JOURNAL_CODE_CAP, _cap_code

    short = "x = 1"
    assert _cap_code(short) == short
    long = "y = 2\n" * 2000
    capped = _cap_code(long)
    assert len(capped) < len(long)
    assert capped.startswith(long[:_JOURNAL_CODE_CAP])
    assert "not journaled" in capped


# ─── 3: memory visibility ─────────────────────────────────────────────────────


def test_empty_store_renders_a_placeholder_section(tmp_path) -> None:
    store = SkillStore(tmp_path)
    section = store.render_memory_section()
    assert section is not None and section.startswith("# Memory")
    assert "no saved facts yet" in section
    assert "Remember" in section and "Recall" in section
    assert len(section) < 600


def test_tool_provenance_facts_render_with_a_confirm_hint(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("pytest lives in the project venv", provenance="tool", key="env.pytest")
    section = store.render_memory_section()
    assert "pytest lives in the project venv" in section
    assert "confirm with Remember" in section
    hits = store.search_memories("pytest")
    assert hits and hits[0][0].provenance == "tool"


def test_remember_description_says_when() -> None:
    text = remember_mod.describe()
    assert "durable facts you establish yourself" in text
    assert "Task progress goes in Note" in text
    assert "pytest" in text


@pytest.mark.asyncio
async def test_recall_miss_says_what_to_try_next(tmp_path) -> None:
    store = SkillStore(tmp_path)
    ctx = ToolCtx()
    ctx.extra["recall_store"] = store
    out = await recall_mod.handler({"query": "session-audit round 8"}, ctx)
    assert not out.is_error
    assert "broader" in out.text
    assert "Remember it" in out.text


@pytest.mark.asyncio
async def test_recall_hit_labels_tool_facts(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.save_memory("node_modules missing in frontend", provenance="tool", key="env.node_modules")
    ctx = ToolCtx()
    ctx.extra["recall_store"] = store
    out = await recall_mod.handler({"query": "node_modules"}, ctx)
    assert "node_modules missing in frontend" in out.text
    assert "confirm with Remember" in out.text


# ─── 4: mechanical fact harvest ──────────────────────────────────────────────


def test_environment_facts_are_harvested_with_stable_keys(tmp_path, monkeypatch) -> None:
    import importlib.util

    from fluid.context import runtime_facts as rf

    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    facts = dict(rf.harvest_environment_facts(str(tmp_path)))
    assert "pytest is not importable" in facts["env.pytest"]
    assert "frontend" in facts["env.node_modules"]

    store = SkillStore(tmp_path / "store")
    ids = rf.write_harvested_facts(store, "project:x", list(facts.items()))
    assert len(ids) == 2
    entries = {e.key: e for e in store.memories()}
    assert entries["env.pytest"].provenance == "tool" and not entries["env.pytest"].protected
    assert entries["env.pytest"].scope == "project:x"
    # Idempotent: the same state again is not a new revision.
    rf.write_harvested_facts(store, "project:x", list(facts.items()))
    assert {e.key: e.revision for e in store.memories()} == {k: 1 for k in entries}
    # A user's explicit Remember on the same key wins and is left alone.
    store.save_memory("pytest: use .venv", protected=True, key="env.pytest", scope="project:x",
                      memory_id=entries["env.pytest"].id, expected_revision=1)
    rf.write_harvested_facts(store, "project:x", [("env.pytest", "harvested again")])
    assert {e.key: e.text for e in store.memories()}["env.pytest"] == "pytest: use .venv"
    section = store.render_memory_section("project:x")
    assert "Node dependencies" in section and "confirm with Remember" in section


def test_browser_host_fact_is_harvested_from_a_browser_result() -> None:
    from fluid.context import runtime_facts as rf

    out = "Cannot reach http://localhost:5193 from the browser host. Browse http://10.0.0.135:5193 instead."
    facts = rf.harvest_tool_facts("mcp__browser__browser_navigate", out)
    assert facts and facts[0][0] == "env.browser_host"
    assert "10.0.0.135" in facts[0][1] and "0.0.0.0" in facts[0][1]
    assert rf.harvest_tool_facts("Read", out) == []
    assert rf.harvest_tool_facts("browser_navigate", "Browse http://localhost:1 instead") == []


@pytest.mark.asyncio
async def test_runtime_facts_block_emits_without_an_environment_section(tmp_path, monkeypatch) -> None:
    from fluid.engine.native import NativeLoop

    from fluid.context.compiler import CompiledPrompt

    loop = NativeLoop.__new__(NativeLoop)
    loop.system_sections = CompiledPrompt(sections=(), text=None)  # compiled, no env section
    loop.ctx = ToolCtx(cwd=str(tmp_path))
    loop._runtime_facts_sent = None
    loop._runtime_facts_harvested = False
    store = SkillStore(tmp_path / "store")
    loop.ctx.extra["memory_store"] = store
    loop.ctx.extra["memory_project"] = "project:t"
    first = await loop._environment_reminder()
    assert first is not None and "Runtime facts" in first and "pytest in worker Python" in first
    assert any(e.key == "env.pytest" for e in store.memories())
    # Unchanged facts: nothing rides the second turn.
    assert await loop._environment_reminder() is None
    # Changed facts: the block comes back.
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    again = await loop._environment_reminder()
    assert again is not None and "Node dependencies" in again
    # A bare system string (no compiled sections) gets no block at all.
    bare = NativeLoop.__new__(NativeLoop)
    bare.system_sections = None
    bare.ctx = ToolCtx(cwd=str(tmp_path))
    bare._runtime_facts_sent = None
    bare._runtime_facts_harvested = True
    assert await bare._environment_reminder() is None


@pytest.mark.asyncio
async def test_runtime_facts_block_is_not_resent_by_a_resumed_engine(tmp_path) -> None:
    """The app rebuilds the engine on every wake; unchanged facts must not
    ride every wake turn (they are recovered from the stored transcript)."""
    import asyncio

    from fluid.context.compiler import CompiledPrompt, PromptSection
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

        def __init__(self) -> None:
            self.requests: list = []

        async def stream(self, request):
            self.requests.append(request)
            yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})
            yield StreamChunk("message_stop", {})

    sections = CompiledPrompt(sections=(PromptSection(kind="base", text="b"),), text="b")
    store = MemoryStore()
    first = NativeLoop(provider=_Provider(), store=store, model="m", system="b",
                       system_sections=sections, ctx=ToolCtx(cwd=str(tmp_path)))
    await first.submit(UserTurn(content="one"))
    await asyncio.wait_for(first.wait_turn(), timeout=5)
    sid = first.session_id
    await first.aclose()
    provider = _Provider()
    second = NativeLoop(provider=provider, store=store, model="m", system="b",
                        system_sections=sections, session_id=sid, ctx=ToolCtx(cwd=str(tmp_path)))
    await second.submit(UserTurn(content="two"))
    await asyncio.wait_for(second.wait_turn(), timeout=5)
    sent = "\n".join(b.text for m in provider.requests[0].messages for b in m.content if hasattr(b, "text"))
    assert sent.count("Runtime facts as of this message") == 1
    await second.aclose()
