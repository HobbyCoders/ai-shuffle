"""Dynamic system-prompt compiler — fluid owns prompt assembly (ruling A1/A12).

The harness, not the embedding application, compiles the final system prompt —
the same relationship Claude Code has to its own prompt. The application passes
only what it alone knows:

* ``base``          — the agent's *configured* system prompt, verbatim. There is
                      no hidden curated base underneath it (ruling A12): an
                      empty/None base means the agent runs with just the
                      compiled context around it.
* ``extra_blocks``  — app-private text blocks appended after the base (security
                      instructions, per-agent memory, request-time appends…).

Everything else the compiler gathers itself:

* environment block — cwd, platform, OS, date, git state
                      (``fluid.context.environment``);
* instruction stack — CLAUDE.md / AGENTS.md discovery
                      (``fluid.context.instructions.build_instruction_stack``);
* skills index      — name+description only; bodies load on demand via the
                      ``Skill`` builtin (progressive disclosure).

Layering order (stable — prompt-cache friendly):
    base → instruction stack → skills index → environment → extra blocks

The delegated (claude CLI) leg in *append* mode must NOT use this compiler for
env/instructions — the CLI gathers its own; compiling both would duplicate.
Callers there pass the base + extra blocks straight through as the append text
and only switch to the compiled form under full-replace (ruling A3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence

from fluid.context.environment import environment_block
from fluid.context.instructions import build_instruction_stack, discover_skills

SKILLS_HEADER = (
    "# Available skills\n"
    "Load a skill's full instructions with the Skill tool before doing a task "
    "it covers."
)


@dataclass(kw_only=True)
class PromptInputs:
    """Inputs to :func:`compile_system_prompt`. App-supplied fields first."""

    base: str | None = None
    extra_blocks: Sequence[str] = field(default_factory=tuple)
    cwd: str | os.PathLike[str] = "."
    include_env: bool = True
    include_global_instructions: bool = True
    include_project_instructions: bool = True
    include_skills: bool = True


def skills_index(cwd: str | os.PathLike[str], *, include_global: bool = True) -> str | None:
    """Progressive-disclosure skills listing (name + description only)."""
    skills = discover_skills(cwd, include_global=include_global)
    if not skills:
        return None
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return f"{SKILLS_HEADER}\n{lines}"


def compile_system_prompt(inputs: PromptInputs) -> str | None:
    """Assemble the final system prompt. Returns ``None`` only when every
    section is disabled/empty (so callers can distinguish "no prompt at all")."""
    parts: list[str] = []
    if inputs.base and inputs.base.strip():
        parts.append(inputs.base.rstrip())
    if inputs.include_global_instructions or inputs.include_project_instructions:
        stack = build_instruction_stack(
            inputs.cwd,
            include_global=inputs.include_global_instructions,
            include_project=inputs.include_project_instructions,
        )
        if stack.strip():
            parts.append(stack)
    if inputs.include_skills:
        index = skills_index(
            inputs.cwd, include_global=inputs.include_global_instructions
        )
        if index:
            parts.append(index)
    if inputs.include_env:
        parts.append(environment_block(inputs.cwd))
    for block in inputs.extra_blocks:
        if block and block.strip():
            parts.append(block.rstrip())
    return "\n\n".join(parts) if parts else None
