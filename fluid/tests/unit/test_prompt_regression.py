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
    (tmp_path / "home" / ".fluid" / "MEMORY.md").write_text(
        "durable fact\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    from fluid.cli.config import resolved_prompt

    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)})
    )
    compiled = resolved_prompt(config)
    kinds = [s.kind for s in compiled.sections]
    assert kinds[0] == "base"
    assert kinds[-1] == "environment"
    # The memory-store snapshot (path = MEMORY.md) sits after the instruction
    # stack and right before the environment block.
    indexes = {
        "instructions": next(
            i for i, s in enumerate(compiled.sections)
            if s.kind == "memory" and (s.path or "").endswith("AGENTS.md")
        ),
        "memory_store": next(
            i for i, s in enumerate(compiled.sections)
            if s.kind == "memory" and (s.path or "").endswith("MEMORY.md")
        ),
        "environment": kinds.index("environment"),
    }
    assert indexes["instructions"] < indexes["memory_store"] < indexes["environment"]
