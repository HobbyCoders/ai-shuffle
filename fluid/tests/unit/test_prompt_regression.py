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
        "WEB_UNTRUSTED": (fr.WEB_UNTRUSTED, ("WebFetch", "WebSearch")),
    }
    for label, (fragment, names) in carriers.items():
        for name in names:
            assert fragment in descs[name], f"{label} missing from {name}"
    # The base prompt's ending section splices the same park doctrine.
    from fluid.tools.fragments import END_TURN_ON_BACKGROUND

    assert END_TURN_ON_BACKGROUND in DEFAULT_BASE_PROMPT
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
    assert "Read then Edit the same file in a single cell" in CODE_MODE_ADDENDUM


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
    assert 'read_tool_docs("<name>")' in CODE_MODE_ADDENDUM
    assert "never withholds a capability" in CODE_MODE_ADDENDUM
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
    assert set(BUILTIN_TOOL_NAMES) - listed == set(NATIVE_ONLY_TOOL_NAMES)


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
