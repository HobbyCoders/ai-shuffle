"""The delegated Claude leg: Claude Code's preset prompt with the app's text
appended (decision D6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.events import ResultMessage
from app.core.provider.types import SessionOptions
from fluid.engine.delegated import DelegatedLoop
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"

_BRIDGED = ("Remember", "Recall", "Forget", "SessionSearch", "SessionView")


def _memory_registry() -> ToolRegistry:
    """A registry carrying the app-memory bridge the way a delegated connect
    registers it (``mcp__builtins__*`` names)."""

    async def handler(input, ctx):
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    for tool in _BRIDGED:
        registry.register_builtin(
            ToolSpec(name=f"mcp__builtins__{tool}", description=tool, input_schema={}),
            handler,
        )
    return registry


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """The appended text reads ~/.fluid (AGENTS.md, skills) and the learned
    guidance home: point both at an empty directory so the developer's own
    files never reach these tests."""
    home = tmp_path / "home"
    home.mkdir()
    # Bound the project walk: without a git root it climbs to the drive root
    # and finds whatever AGENTS.md files the machine has above the temp dir.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))
    return home


def _preset_options(cwd, *, registry=None, **extra) -> SessionOptions:
    return SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND TEXT"},
        cwd=str(cwd),
        extra={"fluid_registry": registry if registry is not None else ToolRegistry(), **extra},
    )


class _ArgvLoop(DelegatedLoop):
    """The real delegated loop, recording the argv of every CLI spawn."""

    argvs: list = []

    def _build_argv(self):
        argv = super()._build_argv()
        type(self).argvs.append(argv)
        return argv


def _flag(argv: list, name: str):
    return argv[argv.index(name) + 1] if name in argv else None


async def _run_turn(adapter, text: str) -> ResultMessage:
    await adapter.send_query(text)
    async for message in adapter.receive_events():
        if isinstance(message, ResultMessage):
            return message
    raise AssertionError("turn ended without a result")


def test_delegated_append_carries_the_app_guidance(tmp_path):
    """Memory, recall and the gh note ride the appended text, named the way
    the CLI sees the bridged tools; fluid's Note/Ledger sentence does not,
    because the delegated leg has neither tool."""
    text = ha._delegated_append_text(
        "APPEND TEXT",
        _preset_options(tmp_path, github_connected=True),
        _memory_registry(),
    )
    assert text.startswith("APPEND TEXT")
    assert "# App tools on this session" in text
    for tool in _BRIDGED:
        assert f"__{tool}" in text
    assert ha.RECALL_BLOCK in text
    assert ha._MEMORY_BLOCK_CORE in text
    assert "Note and Ledger" not in text
    assert ha.GITHUB_BLOCK in text


def test_delegated_append_names_the_apps_task_and_background_tools(tmp_path):
    """The prompt's Task and Monitor are the app's bridged tools on this leg;
    the CLI's own tools of those names are banned there."""

    async def handler(input, ctx):
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    for tool in ("Task", "Monitor", "TaskStop"):
        registry.register_builtin(
            ToolSpec(name=f"mcp__builtins__{tool}", description=tool, input_schema={}),
            handler,
        )
    text = ha._delegated_append_text("APPEND TEXT", _preset_options(tmp_path), registry)
    assert "# App tools on this session" in text
    assert "__Task, __Monitor, __TaskStop" in text
    assert "__TaskOutput" not in text  # not registered, so not named
    assert all(name in ha.DELEGATED_BANNED_TOOLS for name in ("Task", "Monitor", "TaskStop"))


def test_delegated_append_drops_guidance_for_tools_off_the_surface(tmp_path):
    """Ruling T9 on this leg too: no block describes a tool the session
    cannot call."""
    options = _preset_options(tmp_path, github_connected=True)
    options.tool_allowlist = ["Read"]
    text = ha._delegated_append_text("APPEND TEXT", options, _memory_registry())
    assert text == "APPEND TEXT"


@pytest.mark.asyncio
async def test_delegated_append_is_byte_identical_across_turns(monkeypatch, tmp_path):
    """The appended prompt is part of the CLI's cached prefix, so it must not
    change between turns. Two turns, with a CLI restart between them so the
    second one builds its argv again, must send the same bytes. The text
    rides a file (``--append-system-prompt-file``), so each spawn's file
    contents are what must match."""
    import fluid.engine.delegated as delegated_mod

    class _FileLoop(_ArgvLoop):
        """Keeps the prompt file's bytes as each spawn saw them."""

        prompt_bytes: list = []

        def _build_argv(self):
            argv = super()._build_argv()
            path = _flag(argv, "--append-system-prompt-file")
            type(self).prompt_bytes.append(Path(path).read_bytes() if path else None)
            return argv

    # The fake CLI reports version 2.0.0; the prompt file flag is sent from
    # 2.1.0 on, so lower the gate for this test instead of taking the
    # inline fallback.
    monkeypatch.setattr(delegated_mod, "_CLI_FLAGS_SINCE", "2.0.0")
    monkeypatch.setattr(ha, "DelegatedLoop", _FileLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _ArgvLoop.argvs = []
    _FileLoop.prompt_bytes = []

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        _preset_options(tmp_path, registry=_memory_registry(), github_connected=True)
    )
    try:
        first = await _run_turn(adapter, "first turn")
        await adapter.engine._stop_process()
        second = await _run_turn(adapter, "second turn")
    finally:
        await adapter.disconnect()

    assert not first.is_error and not second.is_error
    assert len(_ArgvLoop.argvs) == 2
    assert all(_flag(argv, "--append-system-prompt") is None for argv in _ArgvLoop.argvs)
    appends = _FileLoop.prompt_bytes
    assert len(appends) == 2 and appends[0]
    assert appends[0] == appends[1]
    assert all(
        _flag(argv, "--system-prompt") is None and _flag(argv, "--system-prompt-file") is None
        for argv in _ArgvLoop.argvs
    )
    # The second spawn resumed the same CLI conversation.
    assert _flag(_ArgvLoop.argvs[1], "--resume") is not None

    # A reconnect (pool eviction, worker restart) builds the text again from
    # the same connect-time inputs; it must come out the same bytes too.
    again = ha.HarnessAdapter()
    await again.connect(
        _preset_options(tmp_path, registry=_memory_registry(), github_connected=True)
    )
    try:
        rebuilt = again.engine.append_system_prompt
    finally:
        await again.disconnect()
    assert rebuilt.encode("utf-8") == appends[0]


# ─── Hooks: the delegated leg feeds the same HookBus the native leg does ────


class _KwargsLoop:
    """Records the kwargs an engine is built with."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.agents = []

    async def _ensure_session(self):
        class _Meta:
            session_id = "ses_x"

        return _Meta()

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_both_legs_get_the_adapters_hook_bus(monkeypatch, tmp_path):
    """One bus per adapter, handed to whichever loop the connect builds, so a
    callback on ``adapter.hooks`` observes either leg."""

    async def _client(kind, options):
        return object(), None

    # This test inspects engine wiring, not host CLI discovery. The SDK-free
    # resolver intentionally has no bundled fallback on a clean test runner.
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    monkeypatch.setattr(ha, "DelegatedLoop", _KwargsLoop)
    delegated = ha.HarnessAdapter()
    await delegated.connect(_preset_options(tmp_path))
    assert _KwargsLoop.captured["hooks"] is delegated.hooks
    assert callable(_KwargsLoop.captured["tool_policy"])

    monkeypatch.setattr(ha, "NativeLoop", _KwargsLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _client)
    native = ha.HarnessAdapter()
    await native.connect(SessionOptions(
        model="claude-sonnet-4-5", cwd=str(tmp_path),
        extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
    ))
    assert _KwargsLoop.captured["hooks"] is native.hooks


@pytest.mark.asyncio
async def test_delegated_leg_sends_mod_usage_to_the_claude_usage_store(monkeypatch, tmp_path):
    """The plan usage the Claude Code mod reports reaches the same store the
    stream's rate_limit events feed."""
    from app.core.usage_limits import claude_mod_usage_sink

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    monkeypatch.setattr(ha, "DelegatedLoop", _KwargsLoop)
    await ha.HarnessAdapter().connect(_preset_options(tmp_path))
    assert _KwargsLoop.captured["plan_usage_sink"] is claude_mod_usage_sink


@pytest.mark.asyncio
async def test_delegated_leg_puts_the_turn_note_back_after_a_compaction(monkeypatch, tmp_path):
    """The loop's ``compact_context`` renders the same note every user
    message carries (date and branch, memory, Away mode), fresh, and the
    main thread's compact SessionStart answer carries it."""
    from app.core import away_mode

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    away = {"text": "<system-reminder>\nAWAY MODE: the owner is away\n</system-reminder>"}
    monkeypatch.setattr(away_mode, "turn_note", lambda _sid: away["text"])
    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(tmp_path, turn_env={
        "execution_mode": "worktree", "worktree_info": {"branch": "feature-note"},
    }))
    try:
        loop = adapter._engine
        assert isinstance(loop, DelegatedLoop)
        note = await loop.compact_context()
        assert note == adapter._turn_context_note()
        assert "Today's date" in note and "feature-note" in note and "AWAY MODE" in note
        # Rendered on every call: Away mode turned off mid-turn is not put back.
        away["text"] = None
        assert "AWAY MODE" not in await loop.compact_context()

        away["text"] = "<system-reminder>\nAWAY MODE: still away\n</system-reminder>"
        loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
        output = await loop._cli_session_start({"source": "compact", "transcript_path": ""})
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "AWAY MODE: still away" in context and "feature-note" in context
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_compaction_note_carries_date_and_branch_without_the_app_env(monkeypatch, tmp_path):
    """#1794: a profile without the app's <env> block has no date or branch
    in its turn note, so after /compact the agent ran git to answer "what
    branch are we on". The put-back note now names both."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(repo))
    try:
        assert adapter._env_note() is None
        note = await adapter._engine.compact_context()
        assert "Today's date: " in note and "Current branch: feature-x" in note
    finally:
        await adapter.disconnect()


def test_delegated_tool_policy_denies_what_the_app_denies(tmp_path):
    """The PreToolUse policy refuses the blocklist, anything off the surface
    and a permission-engine deny rule; everything else is left to the CLI."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    options = _preset_options(tmp_path)
    options.tool_allowlist = ["Bash", "Read", "mcp__browser__*"]
    permissions = PermissionEngine(
        "on_write", rules=[Rule(tool="Read", decision="deny", input_globs={"file_path": "*.env"})],
    )
    policy = ha._delegated_tool_policy(options, permissions, ["Task", "Agent"])

    assert policy("Task", {}) == "Tool 'Task' is disabled in this profile"
    assert policy("WebFetch", {"url": "x"}) == "Tool 'WebFetch' is not enabled in this profile"
    assert "configured rule" in policy("Read", {"file_path": "prod.env"})
    assert policy("Read", {"file_path": "notes.txt"}) is None
    assert policy("Bash", {"command": "ls"}) is None
    assert policy("mcp__browser__navigate", {}) is None


@pytest.mark.asyncio
async def test_delegated_pre_tool_use_reaches_the_bus_and_the_policy_blocks(
    monkeypatch, tmp_path
):
    """End to end against the fake CLI: the CLI's PreToolUse for Bash lands
    on ``adapter.hooks`` as the native loop's PreToolUse event, and the
    profile blocklist denies the call there, so the model gets the denial as
    the tool's result."""
    from fluid.events.model import ToolResult
    from fluid.hooks.bus import PRE_TOOL_USE

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    seen: list = []

    async def observe(event, payload):
        seen.append((event, payload["name"], payload["input"]))
        return None

    options = _preset_options(tmp_path)
    options.disallowed_tools = ["Bash"]
    options.env = {"FAKE_CLAUDE_SCENARIO": "u"}
    adapter = ha.HarnessAdapter()
    adapter.hooks.on(PRE_TOOL_USE, observe)
    await adapter.connect(options)
    try:
        result = await _run_turn(adapter, "run it")
        events = adapter.engine.bus.log(adapter.engine.session_id)
    finally:
        await adapter.disconnect()

    assert not result.is_error
    assert seen == [(PRE_TOOL_USE, "Bash", {"command": "echo hooked"})]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [(r.is_error, r.output) for r in results] == [
        (True, "Tool 'Bash' is disabled in this profile"),
    ]


# ─── Usage: the delegated leg fills the same per-call roll-up ───────────────


def test_delegated_usage_stream_rolls_up_with_its_cache_hit_ratio():
    """A synthetic CLI message stream with known usage, mapped by the
    delegated leg and translated by the adapter, fills the per-call roll-up
    the native leg fills: summed tokens, a cost for every call, one call per
    model message, and a cache-hit ratio that matches the totals."""
    from app.core.provider.events import AssistantMessage
    from fluid.engine.delegated_proto import StreamJsonMapper
    from fluid.events.model import Usage
    from fluid.providers.pricing import estimate_cost

    model = "claude-opus-4-6"
    calls = [
        {"input_tokens": 100, "output_tokens": 20,
         "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 900},
        {"input_tokens": 50, "output_tokens": 40,
         "cache_read_input_tokens": 9900, "cache_creation_input_tokens": 50},
    ]

    def frame(msg_id, usage, block):
        return {"type": "assistant", "session_id": "cli", "message": {
            "id": msg_id, "model": model, "stop_reason": "end_turn",
            "content": [block], "usage": usage,
        }}

    frames = [
        # One model message split into two per-block frames, each repeating
        # the message's usage: it is still one call.
        frame("msg_1", calls[0], {"type": "thinking", "thinking": "plan", "signature": "s"}),
        frame("msg_1", calls[0], {"type": "text", "text": "Looking."}),
        frame("msg_2", calls[1], {"type": "text", "text": "Done."}),
    ]
    mapper = StreamJsonMapper(session_id="ses", turn_id="turn", model="fable", user_node_id="n0")
    events = [e for f in frames for e in mapper.feed(f).events]
    # No result frame: release the last call, which the mapper holds for its
    # final counts until the stream ends.
    events.extend(mapper.flush_usage())
    usages = [e for e in events if isinstance(e, Usage)]
    # The context breakdown the loop asks for after each call arrives as its
    # own record, tokens zero, carrying the index of the call it describes.
    events.append(Usage(
        session_id="ses", node_id=usages[-1].node_id, turn_id="turn",
        context_usage={"totalTokens": 20000, "maxTokens": 200000},
        raw={"model_call_index": 2},
    ))

    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="fable")
    adapter._mark_turn_floor()
    per_call = [
        m.usage for e in events for m in adapter._translate(e)
        if isinstance(m, AssistantMessage) and m.usage
    ]

    totals = adapter._turn_usage_snapshot()
    assert adapter._turn_model_calls == 2
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 60
    assert totals["cache_read_input_tokens"] == 18900
    assert totals["cache_creation_input_tokens"] == 950
    assert totals["cost_usd"] == pytest.approx(
        estimate_cost(model, calls[0]) + estimate_cost(model, calls[1])
    )
    prompt = (
        totals["input_tokens"] + totals["cache_read_input_tokens"]
        + totals["cache_creation_input_tokens"]
    )
    assert totals["cache_read_input_tokens"] / prompt == pytest.approx(0.945)

    # Each call's record carries the provider-reported cache verdict and its
    # index, like a native record; the snapshot rides with the second call's.
    assert [u["model_call_index"] for u in per_call] == [1, 2, 2]
    assert per_call[0]["cache_read_report"]["cached_fraction"] == pytest.approx(0.9, abs=1e-3)
    assert per_call[1]["cache_read_report"]["cached_fraction"] == pytest.approx(0.99, abs=1e-3)
    assert per_call[2]["context_usage"]["totalTokens"] == 20000


def test_a_zero_usage_native_call_still_counts_as_a_call():
    """Only the delegated leg's snapshot record (a call index already counted
    this turn, every token field zero) is skipped. A native call whose
    provider reported no usage also has zero tokens and a context breakdown,
    but it is a real call with its own index, and the ErrorEvent backstop
    must still count it."""
    from fluid.events.model import Usage

    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="local-model")
    adapter._mark_turn_floor()
    for index in (1, 2):
        adapter._translate(Usage(
            session_id="ses", node_id=f"n{index}", turn_id="turn",
            raw={"model_call_index": index},
            context_usage={"totalTokens": 1000 * index, "maxTokens": 32000},
        ))
    assert adapter._turn_model_calls == 2


def test_delegated_append_carries_fluid_instructions_skills_and_learned_guidance(
    tmp_path, _isolated_home,
):
    """Claude Code reads CLAUDE.md and .claude/skills only. fluid's
    AGENTS.md-family files and its skills, which the full-replace prompt
    carried, ride the appended text now, and so does the model's learned
    guidance."""
    project = tmp_path / "proj"
    (project / ".fluid" / "skills" / "deploy").mkdir(parents=True)
    (project / ".agents").mkdir()
    (project / "AGENTS.md").write_text("root agents rule", encoding="utf-8")
    (project / ".fluid" / "AGENTS.md").write_text("fluid agents rule", encoding="utf-8")
    (project / ".agents" / "AGENTS.md").write_text("dot-agents rule", encoding="utf-8")
    (project / ".fluid" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship a build\n---\nbody", encoding="utf-8",
    )
    (_isolated_home / ".fluid").mkdir()
    (_isolated_home / ".fluid" / "AGENTS.md").write_text("global fluid rule", encoding="utf-8")
    guidance = _isolated_home / ".fluid" / "guidance"
    guidance.mkdir()
    (guidance / "fable.md").write_text("learned: prefer small diffs", encoding="utf-8")

    text = ha._delegated_append_text(
        "APPEND TEXT", _preset_options(project), ToolRegistry(),
    )
    assert text.startswith("APPEND TEXT")
    assert ha.DELEGATED_INSTRUCTIONS_HEADER in text
    for rule in ("global fluid rule", "root agents rule", "fluid agents rule", "dot-agents rule"):
        assert rule in text
    assert ha.DELEGATED_SKILLS_HEADER in text
    assert "- deploy: Ship a build (" in text
    assert "learned: prefer small diffs" in text
    # Native-only: it points at a workspace card this leg does not have.
    assert ha.VERIFICATION_BLOCK not in text

    # setting_sources gates them as the compiler does.
    options = _preset_options(project)
    options.setting_sources = ["project"]
    project_only = ha._delegated_append_text("APPEND TEXT", options, ToolRegistry())
    assert "global fluid rule" not in project_only
    assert "root agents rule" in project_only
    options.setting_sources = []
    assert ha.DELEGATED_INSTRUCTIONS_HEADER not in ha._delegated_append_text(
        "APPEND TEXT", options, ToolRegistry(),
    )


def test_delegated_full_replace_skills_name_the_file_to_read(
    tmp_path, _isolated_home, monkeypatch,
):
    """#1793: Claude Code's Skill tool cannot load the app's built-in skills
    or a repository's .fluid/skills ("Unknown skill"). The delegated
    full-replace prompt names each SKILL.md to Read instead of pointing at the
    Skill tool; the native leg keeps fluid's own header (its Skill tool loads
    them)."""
    from fluid.context import instructions
    from fluid.context.compiler import SKILLS_HEADER

    builtin = Path(instructions.__file__).resolve().parent.parent / "builtin_skills"
    monkeypatch.setattr(instructions, "builtin_skills_dir", lambda: builtin)
    project = tmp_path / "proj"
    (project / ".fluid" / "skills" / "deploy").mkdir(parents=True)
    skill_md = project / ".fluid" / "skills" / "deploy" / "SKILL.md"
    skill_md.write_text("---\nname: deploy\ndescription: Ship a build\n---\nbody", encoding="utf-8")
    options = SessionOptions(model="fable", system_prompt="BASE", cwd=str(project))

    delegated = ha.HarnessAdapter._compile_prompt("BASE", None, options)
    assert SKILLS_HEADER not in delegated
    assert ha.DELEGATED_SKILLS_HEADER in delegated
    assert f"- deploy: Ship a build ({skill_md})" in delegated
    assert (builtin / "build-plugin" / "SKILL.md").is_file()
    assert f"({builtin / 'build-plugin' / 'SKILL.md'})" in delegated

    native = ha.HarnessAdapter._compile_prompt_sections("BASE", None, options).text
    assert SKILLS_HEADER in native and ha.DELEGATED_SKILLS_HEADER not in native


# ─── Claude Code compactions become the chat's compaction card ──────────────


@pytest.mark.parametrize(("reason", "trigger"), [
    ("cli_auto_compact", "auto"), ("cli_manual_compact", "manual"),
])
def test_a_cli_compaction_becomes_the_compact_boundary_card(reason, trigger):
    from fluid.events.model import ContextCompaction

    from app.core.provider.events import SystemMessage

    adapter = ha.HarnessAdapter()
    [card] = adapter._translate(ContextCompaction(
        session_id="ses", turn_id="turn", tier=3, reason=reason,
        before_tokens=166_397, after_tokens=34_856,
    ))
    assert isinstance(card, SystemMessage)
    assert card.subtype == "compact_boundary"
    # The CLI's own boundary shape, which the chat's card reads; no summary.
    assert card.data == {"compact_metadata": {
        "trigger": trigger, "pre_tokens": 166_397, "post_tokens": 34_856,
    }}


def test_a_cli_compaction_without_counts_or_trigger_claims_neither():
    from fluid.events.model import ContextCompaction

    [card] = ha.HarnessAdapter()._translate(ContextCompaction(
        session_id="ses", tier=3, reason="cli_compact",
    ))
    assert card.subtype == "compact_boundary"
    assert card.data == {"compact_metadata": {}}


@pytest.mark.parametrize("reason", ["trigger", "overflow", "image_consumed", ""])
def test_a_native_compaction_still_maps_to_nothing(reason):
    """The native ladder's promotions are view-level; their marker comes from
    the replay, not from a card per promotion."""
    from fluid.events.model import ContextCompaction

    assert ha.HarnessAdapter()._translate(ContextCompaction(
        session_id="ses", tier=2, reason=reason, spans=[["a", "b", "digest"]],
    )) == []
