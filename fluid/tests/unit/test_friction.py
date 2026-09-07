"""Friction capture + the end-of-turn reflection nudge.

The continual loop only learns from mistakes if mistakes are RECORDED, and
audited sessions show small models never recording them: the model narrates
the failure in its thinking, redoes the work by hand, and calls Remember zero
times. So the harness detects the mistake itself (``FrictionDetector``) and
then asks — once, cheaply, ignorably — for a lesson (``_reflection_nudge``).

What these tests pin: each detector fires on its own pattern and stays silent
on ordinary use, the per-turn rate limit holds, the nudge is absent on a clean
turn and appears at most once otherwise, and a friction entry reaches a
synthesis pass as a first-class ``[friction]`` line with the salience flag set.
"""

from __future__ import annotations

import json

import pytest

from fluid.codemode.continual import Synthesizer
from fluid.codemode.journal import (
    FRICTION_KIND,
    MAX_FRICTION_PER_TURN,
    FrictionDetector,
    ToolJournal,
)
from fluid.codemode.skills import SkillStore
from fluid.engine.native import (
    REFLECTION_NUDGE_DISPATCH_MARK,
    REFLECTION_NUDGE_MARK,
    NativeLoop,
)
from fluid.events import TurnComplete, UserTurn
from fluid.messages import Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins._util import repeat_search_pointer, unchanged_read_note
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolRegistry
from tests.unit.test_tool_loop import ScriptedProvider, _text, _tool

# ─── the detector, in isolation ──────────────────────────────────────────────


def _signals(entries: list[dict]) -> list[str]:
    return [e["signal"] for e in entries]


def test_not_found_on_a_constructed_path_is_friction() -> None:
    detector = FrictionDetector()
    entries = detector.observe(
        "Read", {"file_path": "/repo/src/guessed.py"},
        "file not found: /repo/src/guessed.py", True,
    )
    assert _signals(entries) == ["path_not_found"]
    entry = entries[0]
    assert entry["kind"] == FRICTION_KIND
    assert entry["name"] == "Read"
    # Mechanical facts only: the call, and what the harness said back.
    assert "/repo/src/guessed.py" in entry["text"]
    assert "file not found" in entry["text"]


def test_other_not_found_phrasings_are_recognized() -> None:
    for name, message in (
        ("Glob", "path not found: /nope"),
        ("Grep", "path does not exist: /nope"),
        ("Bash", "cat: /nope: No such file or directory"),
    ):
        assert _signals(FrictionDetector().observe(name, {}, message, True)) == [
            "path_not_found"
        ]


def test_an_ordinary_error_is_not_friction() -> None:
    detector = FrictionDetector()
    assert detector.observe("Bash", {}, "exit 1: tests failed", True) == []
    assert detector.count == 0


def test_three_consecutive_failures_of_one_tool_are_friction() -> None:
    detector = FrictionDetector()
    assert detector.observe("Edit", {"n": 1}, "boom", True) == []
    assert detector.observe("Edit", {"n": 2}, "boom", True) == []
    entries = detector.observe("Edit", {"n": 3}, "boom again", True)
    assert _signals(entries) == ["error_streak"]
    assert "failed 3 times in a row" in entries[0]["text"]


def test_a_success_resets_the_failure_streak() -> None:
    detector = FrictionDetector()
    detector.observe("Edit", {"n": 1}, "boom", True)
    detector.observe("Edit", {"n": 2}, "boom", True)
    detector.observe("Edit", {"n": 3}, "applied", False)
    detector.observe("Edit", {"n": 4}, "boom", True)
    detector.observe("Edit", {"n": 5}, "boom", True)
    assert detector.count == 0


def test_failures_spread_across_tools_are_not_a_streak() -> None:
    detector = FrictionDetector()
    for name in ("Edit", "Write", "Bash"):
        detector.observe(name, {}, "boom", True)
    assert detector.count == 0


def test_the_harness_repeat_work_notes_are_hooked() -> None:
    detector = FrictionDetector()
    body = "1\tdef f():" + unchanged_read_note(True)
    assert _signals(detector.observe("Read", {"file_path": "a.py"}, body, False)) == [
        "repeat_work"
    ]
    pointer = repeat_search_pointer("Found 3 matches\na\nb\nc")
    assert _signals(detector.observe("Grep", {"pattern": "x"}, pointer, False)) == [
        "repeat_work"
    ]


def test_repeat_work_markers_match_the_real_notes() -> None:
    """Drift guard: the notes live in _util.py, the markers live in journal.py."""
    from fluid.codemode.journal import _REPEAT_MARKERS

    assert any(m in unchanged_read_note(True) for m in _REPEAT_MARKERS)
    assert any(m in repeat_search_pointer("head\nrow") for m in _REPEAT_MARKERS)


def test_empty_result_retried_with_near_identical_arguments() -> None:
    detector = FrictionDetector()
    header = "Grep(pattern=handleClick) in /repo"
    assert detector.observe(
        "Grep", {"pattern": "handleClick", "glob": "*.ts"},
        f"{header}\n(no matches)", False,
    ) == []
    entries = detector.observe(
        "Grep", {"pattern": "handleClick", "glob": "*.tsx"},
        f"{header}\n(no matches)", False,
    )
    assert "repeat_after_empty" in _signals(entries)


def test_a_genuinely_different_retry_is_not_friction() -> None:
    detector = FrictionDetector()
    detector.observe("Grep", {"pattern": "handleClick"}, "(no matches)", False)
    detector.observe(
        "Grep", {"pattern": "onSubmitTheWholeFormNow", "path": "/other/tree"},
        "found 4", False,
    )
    assert detector.count == 0


def test_a_productive_search_clears_the_empty_marker() -> None:
    """Only a retry OF THE EMPTY CALL counts; a later repeat of a hit does not."""
    detector = FrictionDetector()
    detector.observe("Grep", {"pattern": "nowhere"}, "(no matches)", False)
    detector.observe("Grep", {"pattern": "somewhere_else_entirely"},
                     "app.py:1: hit", False)
    detector.observe("Grep", {"pattern": "somewhere_else_entirely"},
                     "app.py:1: hit", False)
    assert detector.count == 0


def test_a_clean_turn_records_nothing() -> None:
    detector = FrictionDetector()
    detector.observe("Read", {"file_path": "a.py"}, "1\tcontents", False)
    detector.observe("Edit", {"file_path": "a.py"}, "applied 1 edit", False)
    detector.observe("Bash", {"command": "pytest"}, "12 passed", False)
    assert detector.count == 0


def test_friction_is_rate_limited_per_turn() -> None:
    detector = FrictionDetector()
    for i in range(20):
        detector.observe("Read", {"file_path": f"/nope/{i}"},
                         f"file not found: /nope/{i}", True)
    assert detector.count == MAX_FRICTION_PER_TURN
    detector.begin_turn()
    assert detector.count == 0
    detector.observe("Read", {"file_path": "/nope"}, "file not found: /nope", True)
    assert detector.count == 1


def test_detector_failures_never_escape() -> None:
    class Exploding:
        def __repr__(self) -> str:  # reached through json default=str
            raise RuntimeError("nope")

    detector = FrictionDetector()
    assert detector.observe("Read", Exploding(), None, True) == []


def test_long_previews_are_capped() -> None:
    detector = FrictionDetector()
    entry = detector.observe(
        "Read", {"file_path": "/x" * 500}, "file not found: " + "y" * 5000, True,
    )[0]
    assert len(entry["text"]) <= 320


# ─── journaling: entries land, salience is flagged ───────────────────────────


class _Synth:
    def __init__(self) -> None:
        self.urgent = 0
        self.kicks = 0

    def mark_urgent(self) -> None:
        self.urgent += 1

    def kick(self) -> None:
        self.kicks += 1


def test_tool_journal_writes_friction_and_flags_salience(tmp_path) -> None:
    store = SkillStore(tmp_path)
    synth = _Synth()
    journal = ToolJournal(store, synth)
    journal.record_tool(
        "Read", {"file_path": "/nope.py"}, "file not found: /nope.py", True
    )
    kinds = [e["kind"] for e in store.journal_entries()]
    assert kinds == ["tool", FRICTION_KIND]
    assert synth.urgent == 1
    # Salience pulls the pass forward; it does not start one mid-turn.
    assert synth.kicks == 0
    assert journal.friction_count == 1
    journal.begin_turn()
    assert journal.friction_count == 0


def test_tool_journal_leaves_clean_calls_alone(tmp_path) -> None:
    store = SkillStore(tmp_path)
    synth = _Synth()
    journal = ToolJournal(store, synth)
    journal.record_tool("Read", {"file_path": "/a.py"}, "1\tcontents", False)
    assert [e["kind"] for e in store.journal_entries()] == ["tool"]
    assert synth.urgent == 0
    assert journal.friction_count == 0


def test_friction_reaches_the_synthesis_prompt_as_its_own_line(tmp_path) -> None:
    store = SkillStore(tmp_path)
    journal = ToolJournal(store, None)
    journal.record_tool("Read", {"file_path": "/nope.py"},
                        "file not found: /nope.py", True)
    synth = Synthesizer(object(), "m", store)
    prompt = synth._prompt(store.journal_entries())
    assert "[friction] Read" in prompt
    # Not misfiled as a code cell (the pre-friction fallback branch).
    assert "### cell" not in prompt


@pytest.mark.asyncio
async def test_a_friction_entry_is_carried_into_a_real_synthesis_pass(
    tmp_path,
) -> None:
    """The loop closes: journal → urgent → the next pass actually sees it."""

    class _Provider:
        name = "fake"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def stream(self, request):
            self.prompts.append(request.messages[0].content[0].text)
            yield StreamChunk(kind="text_delta", payload={"text": "NO_CHANGES"})
            yield StreamChunk(kind="message_stop")

    store = SkillStore(tmp_path)
    provider = _Provider()
    synth = Synthesizer(provider, "m", store, min_new_entries=8)
    journal = ToolJournal(store, synth)
    journal.record_tool("Read", {"file_path": "/nope.py"},
                        "file not found: /nope.py", True)
    # The turn ends: record_event kicks, and urgency makes the slice due.
    journal.record_event("assistant", "I could not find it.")
    await synth.drain()
    assert provider.prompts, "urgency did not pull the pass forward"
    assert "[friction] Read" in provider.prompts[0]
    # Consumed, not stranded: the cursor moved past it.
    assert store.synth_cursor() == store.journal_count()


def test_salience_makes_a_pass_due_immediately(tmp_path) -> None:
    """The Remember convention, reused: urgent ⇒ one new entry is enough."""
    store = SkillStore(tmp_path)
    synth = Synthesizer(object(), "m", store, min_new_entries=8)
    journal = ToolJournal(store, synth)
    journal.record_tool("Read", {"file_path": "/a.py"}, "1\tcontents", False)
    assert not synth.due()  # one plain tool entry, threshold is 8
    journal.record_tool("Read", {"file_path": "/nope.py"},
                        "file not found: /nope.py", True)
    assert synth.due()


# ─── the reflection nudge, through the real engine ───────────────────────────


def _nudge_loop(provider, *, dispatch=None, tmp_path=None, store=None):
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(extra={"continual_journal": lambda *a, **k: None})
    if dispatch is not None:
        ctx.extra["task_dispatch"] = dispatch
    skills = SkillStore(tmp_path) if tmp_path is not None else None
    return NativeLoop(
        provider=provider,
        store=store if store is not None else MemoryStore(),
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ctx,
        journal=ToolJournal(skills, None) if skills is not None else None,
    )


def _texts(message) -> list[str]:
    """Every Text block in a message, tool-result-nested ones included."""
    out: list[str] = []
    for block in message.content:
        if isinstance(block, Text):
            out.append(block.text)
        for inner in getattr(block, "content", []) or []:
            if isinstance(inner, Text):
                out.append(inner.text)
    return out


def _sent(provider) -> list[str]:
    return [
        text
        for request in provider.requests
        for message in request.messages
        for text in _texts(message)
    ]


async def _run(loop, prompt: str = "go") -> None:
    await loop.submit(UserTurn(content=prompt))
    await loop.wait_turn()
    assert any(isinstance(e, TurnComplete) for e in loop.bus.log(loop.session_id))


@pytest.mark.asyncio
async def test_no_nudge_on_a_clean_turn(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    provider = ScriptedProvider([
        _tool("c1", "Read", json.dumps({"file_path": str(tmp_path / "a.txt")})),
        _text("done"),
    ])
    loop = _nudge_loop(provider, tmp_path=tmp_path / "skills")
    await _run(loop)
    sent = _sent(provider)
    assert not any(REFLECTION_NUDGE_MARK in t for t in sent)
    assert not any(REFLECTION_NUDGE_DISPATCH_MARK in t for t in sent)


@pytest.mark.asyncio
async def test_friction_turn_gets_exactly_one_nudge(tmp_path) -> None:
    missing = str(tmp_path / "nope.py")
    provider = ScriptedProvider([
        _tool("c1", "Read", json.dumps({"file_path": missing})),
        _tool("c2", "Read", json.dumps({"file_path": missing + "2"})),
        _text("giving up"),
    ])
    loop = _nudge_loop(provider, tmp_path=tmp_path / "skills")
    await _run(loop)
    # Exactly one physical block in the final request, even though the turn
    # hit friction on both calls: once per turn, appended to immutable
    # history (never re-rendered, never prefix-mutating).
    final = [
        text
        for message in provider.requests[-1].messages
        for text in _texts(message)
        if REFLECTION_NUDGE_MARK in text
    ]
    assert len(final) == 1
    nudge = final[0]
    assert nudge.startswith("<system-reminder>")
    assert "Remember" in nudge
    assert "normal answer" in nudge


@pytest.mark.asyncio
async def test_background_dispatch_alone_earns_the_nudge(tmp_path) -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(
            task_id="task-1", worker_session_id="w1", description="research",
        )

    provider = ScriptedProvider([
        _tool("c1", "Task", json.dumps({
            "agent": "helper", "prompt": "research it", "mode": "background",
        })),
        _text("dispatched"),
    ])
    loop = _nudge_loop(provider, dispatch=dispatch, tmp_path=tmp_path / "skills")
    await _run(loop)
    assert any(REFLECTION_NUDGE_DISPATCH_MARK in t for t in _sent(provider))


# ─── code mode: in-kernel calls feed the same detector ───────────────────────


class _ProbeLoop:
    """The minimal ``loop`` surface CodeModeSession.run_python reads."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.ctx = ToolCtx()
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)

    async def _await_permission(self, *args, **kwargs):  # pragma: no cover
        return "allow", None, None


@pytest.mark.asyncio
async def test_in_kernel_friction_is_journaled_after_its_cell(tmp_path) -> None:
    from fluid.codemode.session import CodeModeSession
    from fluid.tools.builtins import read as read_tool

    registry = ToolRegistry()
    registry.register_builtin(read_tool.SPEC, read_tool.handler)
    store = SkillStore(tmp_path / "skills")
    synth = _Synth()
    session = CodeModeSession(registry, skills=store, synthesizer=synth)
    missing = (tmp_path / "nope.py").as_posix()
    code = (
        "from tools.Read import Read\n"
        "try:\n"
        f"    Read(file_path={missing!r})\n"
        "except Exception as exc:\n"
        "    print('caught')\n"
    )
    try:
        await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30},
            loop=_ProbeLoop(registry),
        )
    finally:
        await session.aclose()

    kinds = [e["kind"] for e in store.journal_entries()]
    # The cell first, then what it cost — a friction entry reads as a note on
    # the cell above it, never as a mystery ahead of it.
    assert kinds == ["cell", FRICTION_KIND]
    entry = store.journal_entries()[1]
    assert entry["signal"] == "path_not_found"
    assert entry["name"] == "Read"
    assert session.friction_count == 1
    assert synth.urgent == 1
    session.begin_turn()
    assert session.friction_count == 0


@pytest.mark.asyncio
async def test_a_clean_cell_journals_no_friction(tmp_path) -> None:
    from fluid.codemode.session import CodeModeSession
    from fluid.tools.builtins import read as read_tool

    registry = ToolRegistry()
    registry.register_builtin(read_tool.SPEC, read_tool.handler)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    store = SkillStore(tmp_path / "skills")
    session = CodeModeSession(registry, skills=store)
    target = (tmp_path / "a.txt").as_posix()
    code = f"from tools.Read import Read\nprint(Read(file_path={target!r}))\n"
    try:
        await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30},
            loop=_ProbeLoop(registry),
        )
    finally:
        await session.aclose()
    assert [e["kind"] for e in store.journal_entries()] == ["cell"]
    assert session.friction_count == 0


@pytest.mark.asyncio
async def test_no_nudge_without_a_continual_journal(tmp_path) -> None:
    """Nothing to record a lesson into ⇒ do not ask for one."""
    missing = str(tmp_path / "nope.py")
    provider = ScriptedProvider([
        _tool("c1", "Read", json.dumps({"file_path": missing})),
        _text("giving up"),
    ])
    loop = _nudge_loop(provider)  # no SkillStore ⇒ no ToolJournal
    await _run(loop)
    assert not any(REFLECTION_NUDGE_MARK in t for t in _sent(provider))


def test_nudge_is_silent_when_remember_is_off_the_surface(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    loop = NativeLoop(
        provider=ScriptedProvider([_text("hi")]),
        store=MemoryStore(),
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(),  # no continual_journal ⇒ Remember has no capability
        journal=ToolJournal(SkillStore(tmp_path), None),
    )
    loop._begin_friction_turn()
    loop._background_dispatches = 2
    assert loop._reflection_nudge() is None
