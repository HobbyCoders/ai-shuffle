"""The prompt/description regression harness (Phase 4.2).

Guards the two drift classes the codebase has actually hit:
* prose referencing tools that do not exist in the session's surface
  (hallucinated-call bait), and
* the base prompt and tool descriptions drifting apart (the Bash
  "Git discipline" incident, PR #5).
"""

from __future__ import annotations

import re

from fluid.cli.prompt import (
    BUILTIN_TOOL_NAMES,
    DEFAULT_BASE_PROMPT,
    PromptProfile,
    compose_base_prompt,
    mentioned_tool_names,
)
from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge
from fluid.config.model import HarnessConfig
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolRegistry


def _registry_names() -> set[str]:
    registry = ToolRegistry()
    register_builtins(registry)
    return {s.name for s in registry.list_tools(loaded_only=False)}


def test_prompt_vocabulary_matches_registry_exactly() -> None:
    # Every name the prompt module writes prose about must be a registered
    # builtin, and every registered builtin must be in the vocabulary (else
    # the gating and the mention-scan silently ignore it).
    assert set(BUILTIN_TOOL_NAMES) == _registry_names()


def test_every_description_references_only_real_tools() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    names = {s.name for s in registry.list_tools(loaded_only=False)}
    token_re = re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b")
    for spec in registry.list_tools(loaded_only=False):
        mentioned = {m.group(1) for m in token_re.finditer(spec.description)}
        unknown = mentioned - names
        assert not unknown, f"{spec.name} description references unknown tools: {unknown}"


def test_default_prompt_mentions_subset_of_full_surface() -> None:
    assert mentioned_tool_names(DEFAULT_BASE_PROMPT) <= set(BUILTIN_TOOL_NAMES)


def test_gated_profiles_never_leak_absent_tools() -> None:
    surfaces = [
        frozenset(),
        frozenset({"Read"}),
        frozenset({"Bash", "Read", "Grep"}),
        frozenset({"Edit", "Write", "Read"}),
        frozenset({"Task"}),
        frozenset({"Note"}),
        frozenset({"Ledger"}),
        frozenset({"WebFetch"}),
        frozenset({"WebSearch", "WebFetch"}),
        frozenset({"HistoryExpand", "HistorySearch", "ToolSearch"}),
        frozenset(BUILTIN_TOOL_NAMES) - {"Monitor"},
        frozenset(BUILTIN_TOOL_NAMES) - {"Ledger", "Note"},
    ]
    for surface in surfaces:
        for code_mode in (False, True):
            text = compose_base_prompt(
                PromptProfile(tools=surface, code_mode=code_mode, model="claude")
            )
            leaked = mentioned_tool_names(text) - surface
            assert not leaked, f"surface {sorted(surface)} leaked {sorted(leaked)}"


def test_gated_descriptions_never_leak_absent_tools() -> None:
    # The description refinement pass mirrors the base prompt's invariant:
    # a tool's description may reference another tool only when that tool is
    # in the session's permitted surface.
    from fluid.tools.builtins import _DESCRIBERS, refine_builtin_descriptions

    surfaces = [
        frozenset({"Read"}),
        frozenset({"Bash"}),
        frozenset({"Bash", "Read", "Grep"}),
        frozenset({"Edit", "Write"}),
        frozenset({"Grep", "Glob"}),
        frozenset({"WebSearch"}),
        frozenset({"WebFetch"}),
        frozenset({"HistorySearch"}),
        frozenset({"SessionSearch"}),
        frozenset({"Monitor"}),
        frozenset({"Task"}),
        frozenset({"Swarm"}),
        frozenset({"Task", "Swarm"}),
        frozenset(BUILTIN_TOOL_NAMES) - {"Swarm"},
        frozenset(BUILTIN_TOOL_NAMES) - {"Read"},
        frozenset(BUILTIN_TOOL_NAMES) - {"Bash", "Monitor"},
        frozenset(BUILTIN_TOOL_NAMES),
    ]
    for surface in surfaces:
        registry = ToolRegistry()
        register_builtins(registry)
        refine_builtin_descriptions(registry, allowed=sorted(surface))
        for spec in registry.list_tools(loaded_only=False):
            if spec.name not in _DESCRIBERS or spec.name not in surface:
                continue
            leaked = mentioned_tool_names(spec.description) - surface
            assert not leaked, (
                f"{spec.name} with surface {sorted(surface)} "
                f"leaked {sorted(leaked)}"
            )


def test_swarm_routing_is_gated_and_does_not_encourage_task_fanout():
    from fluid.tools.builtins.task import describe
    from fluid.tools.fragments import SWARM_ROUTING, SWARM_ROUTING_SUMMARY

    for code_mode in (False, True):
        for surface in (frozenset({"Task"}), frozenset({"Task", "Swarm"})):
            prompt = compose_base_prompt(PromptProfile(tools=surface, code_mode=code_mode))
            description = describe(surface)
            if "Swarm" in surface:
                assert SWARM_ROUTING_SUMMARY in prompt
                assert SWARM_ROUTING in description
                assert "three or more planned subagent tasks" in description
                assert "Do not split batches into Task calls" in description
                assert "adaptive follow-up" in description
                assert "never override a restriction on delegation" in description
                assert "send several Task calls" not in description
            else:
                assert "Swarm" not in prompt + description


def test_refined_full_surface_matches_module_constants() -> None:
    # Refining with the full surface must reproduce the shipped SPEC text —
    # the describers and the constants may not drift apart.
    from fluid.tools.builtins import refine_builtin_descriptions

    baseline = ToolRegistry()
    register_builtins(baseline)
    before = {s.name: s.description for s in baseline.list_tools(loaded_only=False)}

    refined = ToolRegistry()
    register_builtins(refined)
    refine_builtin_descriptions(refined)
    after = {s.name: s.description for s in refined.list_tools(loaded_only=False)}
    assert before == after


def test_shared_fragments_appear_verbatim() -> None:
    # Cross-tool rules live once in fluid.tools.fragments and are spliced
    # byte-identical into every carrier (the Claude Code shared-fragment
    # pattern) — a reworded copy is exactly the drift this file guards.
    from fluid.tools import fragments as fr

    registry = ToolRegistry()
    register_builtins(registry)
    descs = {s.name: s.description for s in registry.list_tools(loaded_only=False)}
    carriers = {
        "READ_BEFORE_EDIT": (fr.READ_BEFORE_EDIT, ("Read", "Edit", "Write")),
        "NO_VERIFY_READBACK": (fr.NO_VERIFY_READBACK, ("Edit", "Write")),
        "FRESH_PROCESS": (fr.FRESH_PROCESS, ("Bash", "Monitor")),
        "DONT_PREDICT_PENDING": (fr.DONT_PREDICT_PENDING, ("Task", "Monitor")),
        "END_TURN_ON_BACKGROUND": (
            fr.END_TURN_ON_BACKGROUND,
            ("Task", "Monitor"),
        ),
        "SWARM_ROUTING": (fr.SWARM_ROUTING, ("Task", "Swarm")),
        "WEB_UNTRUSTED": (fr.WEB_UNTRUSTED, ("WebFetch", "WebSearch")),
    }
    for label, (fragment, names) in carriers.items():
        for name in names:
            assert fragment in descs[name], f"{label} missing from {name}"
    # The base prompt's ending section splices the same park doctrine.
    from fluid.tools.fragments import END_TURN_ON_BACKGROUND

    assert END_TURN_ON_BACKGROUND in DEFAULT_BASE_PROMPT
    assert fr.SWARM_ROUTING_SUMMARY in DEFAULT_BASE_PROMPT
    # Bash and Monitor share one cwd parameter description.
    for name in ("Bash", "Monitor"):
        spec = registry.get(name)
        assert spec is not None
        assert (
            spec.input_schema["properties"]["cwd"]["description"] == fr.CWD_PARAM
        )


def test_prompt_structure_invariants() -> None:
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {}))
    from fluid.cli.prompt import default_base_prompt

    text = default_base_prompt(config)
    # Deterministic and newline-terminated.
    assert text == default_base_prompt(config)
    assert text.endswith("\n") and not text.endswith("\n\n")
    # Section order: identity first, ending last.
    order = [
        text.index("## Communication"),
        text.index("## Doing the work"),
        text.index("## Tools"),
        text.index("## Permissions and safety"),
        text.index("## Instructions, skills, memory"),
        text.index("## Context"),
        text.index("## Mid-turn interjections"),
        text.index("## Environment"),
        text.index("## Ending a turn"),
    ]
    assert order == sorted(order)
    # Bounded: the full default prompt stays well under provider minimums.
    assert len(text) < 16_000


def test_compiled_prompt_section_order(tmp_path, monkeypatch) -> None:
    # base → instructions → skills/agents → memory → environment → extras.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / "home" / ".fluid").mkdir(parents=True)
    from fluid.codemode.skills import SkillStore

    skills_dir = tmp_path / "home" / ".fluid" / "skills"
    SkillStore(skills_dir).save_memory("durable fact")
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    from fluid.cli.config import resolved_prompt

    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {
            "cwd": str(tmp_path),
            "continual": True,
            "skills_dir": str(skills_dir),
        })
    )
    compiled = resolved_prompt(config)
    kinds = [s.kind for s in compiled.sections]
    assert kinds[0] == "base"
    assert kinds[-1] == "environment"
    # The learner-memory snapshot (path = memories.json) sits after the
    # instruction stack and right before the environment block.
    indexes = {
        "instructions": next(
            i for i, s in enumerate(compiled.sections)
            if s.kind == "memory" and (s.path or "").endswith("AGENTS.md")
        ),
        "memory_store": next(
            i for i, s in enumerate(compiled.sections)
            if s.kind == "memory" and (s.path or "").endswith("memories.json")
        ),
        "environment": kinds.index("environment"),
    }
    assert indexes["instructions"] < indexes["memory_store"] < indexes["environment"]


def test_code_mode_addendum_never_leaks_absent_tools() -> None:
    """The addendum names concrete tools now, so it is gated like every other
    prompt section — a name only appears when the session can call it."""
    from fluid.cli.prompt import CODE_MODE_TOOL_NAMES, compose_code_mode_addendum

    code_mode_surface = set(CODE_MODE_TOOL_NAMES)
    surfaces = [
        frozenset(),
        frozenset({"Read"}),
        frozenset({"Bash", "Read", "Grep"}),
        frozenset({"Edit", "Write", "Read"}),
        frozenset({"Glob"}),
        frozenset(BUILTIN_TOOL_NAMES),
    ]
    for surface in surfaces:
        text = compose_code_mode_addendum(surface)
        leaked = mentioned_tool_names(text) - surface - code_mode_surface
        assert not leaked, f"surface {sorted(surface)} leaked {sorted(leaked)}"


def test_code_mode_addendum_carries_the_cell_authoring_rules() -> None:
    """The two mistakes that account for most unparseable cells are named."""
    from fluid.cli.prompt import CODE_MODE_ADDENDUM

    assert 'Grep(pattern=r"def foo|bar", glob="*.py")' in CODE_MODE_ADDENDUM
    assert "heredoc" in CODE_MODE_ADDENDUM
    assert "A known Read/Edit recipe may share one cell" in CODE_MODE_ADDENDUM
    assert "first select that evidence and stop at the decision boundary" in CODE_MODE_ADDENDUM
    assert "single-call cell is appropriate" in CODE_MODE_ADDENDUM
    assert "stated stdout cap" in CODE_MODE_ADDENDUM


def test_code_mode_addendum_explains_deferred_tools() -> None:
    """Code mode has no ToolSearch, so the addendum is where the deferred tier
    is explained: fetch the docs with read_tool_docs, the tool is callable
    either way."""
    from fluid.cli.prompt import (
        CODE_MODE_ADDENDUM,
        PromptProfile,
        compose_base_prompt,
        compose_code_mode_addendum,
    )

    assert "[deferred]" in CODE_MODE_ADDENDUM
    assert "`read_tool_docs` tool" in CODE_MODE_ADDENDUM
    assert "rather than withholding a capability" in CODE_MODE_ADDENDUM
    # "It imports and calls exactly like any other tool" read as a promise
    # about read_tool_docs — the nearest name — and seven of nine audited
    # code-mode sessions opened a batched cell with
    # `from tools.read_tool_docs import read_tool_docs`, losing the whole
    # cell. The subject of that sentence is now named, and the catalog tools
    # are said outright to be unimportable.
    assert "never importable from `tools`" in CODE_MODE_ADDENDUM
    assert "The deferred tool itself" in CODE_MODE_ADDENDUM
    # Gated like the rest: the text is surface-independent, so it survives even
    # the empty builtin surface (it names only code-mode tools).
    assert "[deferred]" in compose_code_mode_addendum(frozenset())

    # The ToolSearch bullet in the base prompt stays off under code mode — the
    # tool is not in that schema, so the addendum owns the whole story.
    code_mode_text = compose_base_prompt(
        PromptProfile(tools=frozenset(BUILTIN_TOOL_NAMES), code_mode=True, model="claude")
    )
    assert "load one with ToolSearch" not in code_mode_text


# ─── the delegated ``claude`` leg ────────────────────────────────────────────


def _delegated_config(tmp_path, **overrides):
    return HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {
            "cwd": str(tmp_path),
            "default_provider": "claude",
            "continual": True,
            "code_mode": True,
            "skills_dir": str(tmp_path / "skills"),
            **overrides,
        })
    )


async def _bridge_listing(engine) -> set[str]:
    """The tool names the bundled bridge would announce for ``engine``."""
    from fluid.tools.mcp.bundled_server import _answer

    listed = await _answer(
        engine.registry, engine.ctx, {"op": "list"},
        tool_filter=engine._bridge_tool_permitted,
    )
    return {t["name"] for t in listed["tools"]}


async def test_native_only_tools_are_exactly_the_ones_the_bridge_cannot_list(tmp_path):
    """The literal in prompt.py must agree with the registry: a builtin is
    native-only when the delegated engine's tool context lacks one of the
    capabilities it requires."""
    from fluid.cli.config import make_engine
    from fluid.cli.prompt import NATIVE_ONLY_TOOL_NAMES
    from fluid.store.jsonl import JsonlStore

    engine = make_engine(
        _delegated_config(tmp_path), JsonlStore(tmp_path / "store"), provider_name="claude",
    )
    try:
        listed = await _bridge_listing(engine)
    finally:
        await engine.aclose()
    from fluid.cli.prompt import HOST_ONLY_TOOL_NAMES
    assert set(BUILTIN_TOOL_NAMES) - listed == set(NATIVE_ONLY_TOOL_NAMES) | HOST_ONLY_TOOL_NAMES


async def test_delegated_prompt_names_only_tools_the_cli_can_reach(tmp_path):
    """PRM-1: the prompt the ``claude`` leg ships names no tool the bundled
    bridge does not list, and tells the model about the MCP prefix."""
    from fluid.cli.config import make_engine, resolved_prompt
    from fluid.cli.prompt import DELEGATED_TOOL_PREFIX, NATIVE_ONLY_TOOL_NAMES
    from fluid.store.jsonl import JsonlStore

    config = _delegated_config(tmp_path)
    engine = make_engine(config, JsonlStore(tmp_path / "store"), provider_name="claude")
    try:
        listed = await _bridge_listing(engine)
        text = engine.system
    finally:
        await engine.aclose()
    assert text == resolved_prompt(config, provider_name="claude").text
    mentioned = mentioned_tool_names(text)
    assert mentioned <= listed, sorted(mentioned - listed)
    assert not mentioned & set(NATIVE_ONLY_TOOL_NAMES)
    assert DELEGATED_TOOL_PREFIX in text
    # The native prompt for the same config still names the recall tools.
    native = resolved_prompt(config, provider_name="anthropic").text
    assert "HistoryExpand" in native and DELEGATED_TOOL_PREFIX not in native


def test_delegated_prompt_drops_the_code_mode_addendum(tmp_path, capsys):
    """PRM-2: ``--continual`` implies code mode, but the delegated engine has
    no run_python; the addendum stays out and the user is told."""
    from fluid.cli.config import code_mode_unavailable, resolved_prompt
    from fluid.cli.prompt import code_mode_addendum

    config = _delegated_config(tmp_path)
    assert code_mode_addendum(config, provider_name="anthropic") is not None
    assert code_mode_addendum(config, provider_name="claude") is None
    text = resolved_prompt(config, provider_name="claude").text
    assert "run_python" not in text and "# Code mode" not in text
    assert "run_python" in resolved_prompt(config, provider_name="anthropic").text
    assert code_mode_unavailable(config, "claude")
    assert code_mode_unavailable(config, "anthropic") is None
    off = _delegated_config(tmp_path, code_mode=False, continual=False)
    assert code_mode_unavailable(off, "claude") is None


# ─── Batch 8: the prompt says what the code enforces ─────────────────────────


def _builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtins(registry)
    return registry


def test_tool_capabilities_match_the_registry() -> None:
    """PRM-9: the prompt's capability map is the registry's requires_extra."""
    from fluid.cli.prompt import TOOL_CAPABILITIES

    expected = {
        s.name: tuple(s.requires_extra)
        for s in _builtin_registry().list_tools(loaded_only=False)
        if s.requires_extra
    }
    assert TOOL_CAPABILITIES == expected


def test_unwired_capabilities_drop_their_prose(tmp_path) -> None:
    """PRM-9: a session whose store cannot search drops SessionSearch from
    the schema; the prompt drops the paragraph the same way. Same for the
    Task and Monitor dispatchers when no host is attached."""
    from fluid.cli.config import make_engine, resolved_prompt, wired_capabilities
    from fluid.cli.prompt import TOOL_CAPABILITIES
    from fluid.store.base import MemoryStore
    from fluid.store.jsonl import JsonlStore

    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    none = resolved_prompt(config, wired=frozenset()).text
    assert not mentioned_tool_names(none) & set(TOOL_CAPABILITIES)
    full = resolved_prompt(config).text
    assert {"SessionSearch", "Task", "Monitor", "HistoryExpand"} <= mentioned_tool_names(full)

    memory_only = wired_capabilities(config, MemoryStore(), background_host=False)
    assert "session_search" not in memory_only and "task_dispatch" not in memory_only
    searchable = wired_capabilities(config, JsonlStore(tmp_path / "store"))
    assert {"session_search", "session_view", "task_dispatch", "monitor_spawn"} <= searchable

    engine = make_engine(config, MemoryStore(), provider=object(), background_host=False)
    mentioned = mentioned_tool_names(engine.system)
    assert not mentioned & {"SessionSearch", "SessionView", "Task", "Monitor"}
    assert "HistoryExpand" in mentioned  # the native loop wires recall itself
    # The delegated leg never gets the native-wired set.
    assert "history_recall" not in wired_capabilities(config, MemoryStore(), provider_name="claude")


def test_code_mode_prompt_drops_the_bullets_the_addendum_contradicts() -> None:
    """PRM-7: reply-level parallelism, re-Read after a failed Edit and "the
    user saw the tool call" describe the JSON surface; under code mode the
    addendum owns those stories."""
    from fluid.cli.prompt import PromptProfile, compose_base_prompt

    json_mode = compose_base_prompt(PromptProfile(model="claude"))
    code_mode = compose_base_prompt(PromptProfile(model="claude", code_mode=True))
    for phrase in ("Make independent tool calls in the same reply", "re-Read the file"):
        assert phrase in json_mode and phrase not in code_mode
    assert "The user saw the tool call" in json_mode
    assert "The user saw the cell run" in code_mode


def test_addendum_teaches_the_import_spelling_the_kernel_prefers() -> None:
    """PRM-8."""
    from fluid.cli.prompt import CODE_MODE_ADDENDUM

    assert "`from tools import <name>`" in CODE_MODE_ADDENDUM
    assert "from tools.<name>" not in CODE_MODE_ADDENDUM


def test_outage_and_denial_are_told_apart() -> None:
    """PRM-6: the base prompt forbids routing around a denial; an outage of a
    tool's backing service is named as a different case, and the registry's
    own message says which one it is."""
    import asyncio

    from fluid.tools.registry import ToolCtx, ToolSpec

    assert "temporary outage" in DEFAULT_BASE_PROMPT
    assert "never treat the outage as permission" in DEFAULT_BASE_PROMPT
    registry = ToolRegistry()

    async def handler(input, ctx):  # pragma: no cover - never reached
        raise AssertionError

    registry.register_builtin(
        ToolSpec(name="flaky", description="x", input_schema={"type": "object"},
                 check_available=lambda: False),
        handler,
    )
    outcome = asyncio.run(registry.dispatch("flaky", {}, ToolCtx()))
    assert outcome.is_error and "This is an outage, not a denial" in outcome.text
    assert "use another tool that is itself available" in outcome.text


def test_code_mode_addendum_rides_ahead_of_the_environment_block(tmp_path) -> None:
    """The addendum is static; the environment block carries today's date.
    Placing the addendum first keeps the daily date change from
    invalidating the addendum's cached prefix."""
    from fluid.cli.config import resolved_prompt

    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {
        "cwd": str(tmp_path), "code_mode": True,
    }))
    compiled = resolved_prompt(config, extra_system="per-run note")
    text = compiled.text
    assert text.index("# Code mode") < text.index("\n# Environment") < text.index("per-run note")
    kinds = [s.kind for s in compiled.sections]
    assert kinds[0] == "base" and kinds[-1] == "extra"
    assert "# Code mode" in compiled.sections[0].text


def _golden_path(name: str):
    from pathlib import Path

    return Path(__file__).parent.parent / "golden" / name


def _check_golden(name: str, rendered: str) -> None:
    import os

    path = _golden_path(name)
    if os.environ.get("FLUID_UPDATE_GOLDEN"):
        path.write_text(rendered, encoding="utf-8", newline="\n")
    stored = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert rendered == stored, (
        f"{name} drifted from the golden snapshot; review the diff and, when "
        "the change is intended, regenerate with FLUID_UPDATE_GOLDEN=1"
    )


def test_base_prompt_matches_golden_snapshot() -> None:
    """The rendered default prompt, byte for byte. A wording change is a
    deliberate act that updates this file in the same commit; a drift the
    author did not intend (PRM-3 shipped for weeks) fails here."""
    _check_golden("base_prompt.txt", DEFAULT_BASE_PROMPT)


def test_code_mode_addendum_matches_golden_snapshot() -> None:
    from fluid.cli.prompt import CODE_MODE_ADDENDUM

    _check_golden("code_mode_addendum.txt", CODE_MODE_ADDENDUM + "\n")


def test_mounted_builtin_tools_reads_a_tool_context_extras_mapping() -> None:
    """One derivation of "what this leg mounts", so the prompt block and the
    context breakdown cannot disagree. They did: a host that never injected
    ``agent_message`` still advertised SendMessage and ListAgents in the
    prompt and billed 546 + 245 tokens for them, while the engine withheld
    both from every surface the model could call."""
    from fluid.cli.prompt import BUILTIN_TOOL_NAMES, mounted_builtin_tools

    extras = {"monitor_spawn": object()}
    mounted = mounted_builtin_tools(BUILTIN_TOOL_NAMES, extras)
    assert "SendMessage" not in mounted and "ListAgents" not in mounted
    assert {"Monitor"} <= mounted
    # Capability-free builtins are always mounted.
    assert {"Read", "Grep", "Glob"} <= mounted
    # Bare keys work too, and no extras at all leaves only the free ones.
    assert mounted == mounted_builtin_tools(BUILTIN_TOOL_NAMES, set(extras))
    assert not mounted_builtin_tools(BUILTIN_TOOL_NAMES, None) & {"Monitor", "Task"}


def test_the_peer_message_block_follows_the_mounted_surface(tmp_path) -> None:
    """gpt-6-astra imported SendMessage in 3 of 3 sessions on a host that had
    no agent messaging, because the prompt described how peer messages arrive.
    The block rides on the tool being mounted, not on the tool existing."""
    from fluid.cli.config import resolved_prompt
    from fluid.cli.prompt import BUILTIN_TOOL_NAMES, mounted_builtin_tools

    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)})
    )
    wired = frozenset(
        mounted_builtin_tools(BUILTIN_TOOL_NAMES, ())  # nothing injected
    )
    assert "SendMessage" not in wired
    without = resolved_prompt(config, wired=frozenset()).text
    assert "SendMessage" not in without and "ListAgents" not in without
    with_messaging = resolved_prompt(
        config, wired=frozenset({"agent_message", "agent_roster"})
    ).text
    assert "SendMessage" in with_messaging
