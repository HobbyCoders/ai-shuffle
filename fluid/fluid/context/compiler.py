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
* instruction stack — Fluid-native AGENTS.md discovery
                      (``fluid.context.instructions.build_instruction_stack``);
* skills index      — name+description only; bodies load on demand via the
                      ``Skill`` builtin (progressive disclosure). Omitted when
                      ``PromptInputs.tools`` says the session has no ``Skill``;
* agents index      — the native ``.agents/agents`` catalog, again
                      name+description only; the definitions themselves reach a
                      host dispatcher via ``ctx.extra["agent_registry"]``.
                      Omitted when ``PromptInputs.tools`` says the session has
                      no ``Task``.

Layering order (stable — prompt-cache friendly):
    base → instruction stack → skills index → agents index → learned-memory
    snapshot → environment → extra blocks

Two views of the same compilation:

* :func:`compile_system_prompt` — the joined string every engine consumes;
* :func:`compile_prompt` — a :class:`CompiledPrompt` that keeps the same text
  *plus* the labeled sections it was built from (one per instruction file, with
  its path). ``compile_system_prompt`` is exactly ``compile_prompt(...).text``,
  so the two can never drift. The sections exist for the context-usage
  breakdown (``loop.get_context_usage()``): pricing "memory files" separately
  from "system prompt" needs attribution the joined string has already lost.

The delegated (claude CLI) leg in *append* mode must NOT use this compiler for
env/instructions — the CLI gathers its own; compiling both would duplicate.
Callers there pass the base + extra blocks straight through as the append text
and only switch to the compiled form under full-replace (ruling A3).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from fluid.context.agents import discover_agents
from fluid.context.environment import environment_block
from fluid.context.instructions import build_instruction_blocks, discover_skills

SKILLS_HEADER = (
    "# Available skills\n"
    "Load a skill's full instructions with the Skill tool before doing a task "
    "it covers."
)

AGENTS_HEADER = (
    "# Available agent types\n"
    "These agent types can be delegated to with the Task tool. Each runs in "
    "its own session with its own system prompt and tools and reports back — "
    "pick the one whose description matches the work."
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
    # Native ``.agents/agents`` catalog advertised for Task delegation.
    include_agents: bool = True
    # Continual-learner memory (fluid.codemode.skills): the facts the
    # synthesizer distilled from past sessions' journals, rendered as a frozen
    # snapshot after the indexes and before the environment block — the most
    # cross-session-mutable section sits nearest the volatile tail. The value
    # is the skills root (the continual store's home); ``None`` omits the
    # section (continual learning off).
    memories_dir: str | os.PathLike[str] | None = None
    # Enabled plugins (~/.fluid/plugins, gated by the settings stack's
    # enabledPlugins map) contribute their skills and agents to the indexes,
    # namespaced <plugin>:<name> and just as deferred — descriptions only.
    include_plugins: bool = True
    # Which filesystem scopes discovery may draw from — the Agent SDK's
    # ``setting_sources`` vocabulary: any subset of ("user", "project",
    # "local"). ``None`` means no restriction. "user" gates the ~/.fluid
    # tree (global instructions/skills/agents and plugins), "project" the
    # git-root→cwd walk. The ``include_*`` flags still apply on top — a
    # source restriction only ever removes.
    setting_sources: tuple[str, ...] | None = None
    # The session's tool surface, when the host restricts it (ruling T9).
    # ``None`` means "no filter — describe everything". When a set is supplied,
    # a section whose subject tool the session cannot call is omitted: the
    # skills index needs ``Skill``, the agents index needs ``Task``. The
    # ``include_*`` flags still apply on top — this only ever removes.
    tools: frozenset[str] | None = None


# Section kinds a compiled prompt can carry. ``memory`` sections additionally
# carry the instruction file's ``path`` and ``scope``; a ``tools`` section is
# host-contributed tool documentation priced under the tool categories by the
# usage breakdown (see :mod:`fluid.context.usage`).
SECTION_KINDS = ("base", "memory", "skills", "agents", "environment", "extra",
                 "tools")
@dataclass(frozen=True, kw_only=True)
class PromptSection:
    """One labeled slice of the compiled system prompt."""

    kind: str  # one of SECTION_KINDS
    text: str
    path: str | None = None  # memory sections: the instruction file it came from
    scope: str | None = None  # memory sections: "global" | "project" | "nested"
    # tools sections: the full registry tool name this block documents. Absent
    # (None) on any other kind; a tools section without one prices as system
    # prompt rather than being misattributed.
    tool_name: str | None = None


@dataclass(frozen=True, kw_only=True)
class CompiledPrompt:
    """The compiled prompt in both views: joined text + its labeled sections."""

    sections: tuple[PromptSection, ...] = ()
    text: str | None = None

    def of_kind(self, *kinds: str) -> tuple[PromptSection, ...]:
        return tuple(s for s in self.sections if s.kind in kinds)


def _tool_available(tools: frozenset[str] | None, name: str) -> bool:
    """Can this session call ``name``? ``None`` (no filter) means yes."""
    return tools is None or name in tools


def skills_index(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
    include_plugins: bool = False,
    plugins: list | None = None,
) -> str | None:
    """Progressive-disclosure skills listing (name + description only).

    Sorted by name: the index feeds the cached prompt prefix, so its order must
    not depend on filesystem walk order.
    """
    skills = discover_skills(
        cwd,
        include_global=include_global,
        include_project=include_project,
        include_plugins=include_plugins,
        plugins=plugins,
    )
    if not skills:
        return None
    lines = "\n".join(
        f"- {s.name}: {s.description}" for s in sorted(skills, key=lambda s: s.name)
    )
    return f"{SKILLS_HEADER}\n{lines}"


def agents_index(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
    include_plugins: bool = False,
    plugins: list | None = None,
) -> str | None:
    """Delegation catalog: the discovered agent types (name + description).

    Same shape and the same name sort as :func:`skills_index` (CE7) — one line
    per agent, bodies left out. The bodies are system prompts for the *worker*
    session; the caller only needs to know what exists to delegate to. The
    definitions themselves reach a host dispatcher through
    ``ctx.extra["agent_registry"]``, not through this text.
    """
    agents = discover_agents(
        cwd,
        include_global=include_global,
        include_project=include_project,
        include_plugins=include_plugins,
        plugins=plugins,
    )
    if not agents:
        return None
    lines = "\n".join(
        f"- {a.name}: {a.description}" for a in sorted(agents, key=lambda a: a.name)
    )
    return f"{AGENTS_HEADER}\n{lines}"


def compile_prompt(inputs: PromptInputs) -> CompiledPrompt:
    """Assemble the system prompt, keeping the sections it was built from.

    The instruction stack contributes ONE section per instruction file (in the
    stack's own order), so per-file attribution survives; every other input is a
    single section. ``text`` is the sections joined with blank lines — i.e. the
    exact string :func:`compile_system_prompt` has always returned.
    """
    sections: list[PromptSection] = []
    # Source gating (setting_sources): "user" turns the ~/.fluid tree and
    # plugins on or off, "project" the git-root→cwd walk. None = no restriction.
    sources = inputs.setting_sources
    user_on = sources is None or "user" in sources
    project_on = sources is None or "project" in sources
    include_global = inputs.include_global_instructions and user_on
    include_project = inputs.include_project_instructions and project_on

    want_skills = inputs.include_skills and _tool_available(inputs.tools, "Skill")
    want_agents = inputs.include_agents and _tool_available(inputs.tools, "Task")
    plugins: list | None = None
    if inputs.include_plugins and user_on and (want_skills or want_agents):
        from fluid.context.plugins import discover_plugins

        plugins = discover_plugins(
            inputs.cwd, sources=sources if sources is not None else ("user", "project", "local")
        )

    if inputs.base and inputs.base.strip():
        sections.append(PromptSection(kind="base", text=inputs.base.rstrip()))
    if include_global or include_project:
        for file, block in build_instruction_blocks(
            inputs.cwd,
            include_global=include_global,
            include_project=include_project,
        ):
            if block.strip():
                sections.append(
                    PromptSection(
                        kind="memory", text=block, path=file.path, scope=file.scope
                    )
                )
    if want_skills:
        index = skills_index(
            inputs.cwd,
            include_global=include_global,
            include_project=project_on,
            include_plugins=plugins is not None,
            plugins=plugins,
        )
        if index:
            sections.append(PromptSection(kind="skills", text=index))
    if want_agents:
        index = agents_index(
            inputs.cwd,
            include_global=include_global,
            include_project=project_on,
            include_plugins=plugins is not None,
            plugins=plugins,
        )
        if index:
            sections.append(PromptSection(kind="agents", text=index))
    if inputs.memories_dir is not None:
        from fluid.codemode.skills import SkillStore

        store = SkillStore(inputs.memories_dir)
        section = store.render_memory_section()
        if section:
            sections.append(
                PromptSection(
                    kind="memory",
                    text=section,
                    path=str(store.memories_path),
                    scope="global",
                )
            )
    if inputs.include_env:
        sections.append(
            PromptSection(kind="environment", text=environment_block(inputs.cwd))
        )
    for block in inputs.extra_blocks:
        if block and block.strip():
            sections.append(PromptSection(kind="extra", text=block.rstrip()))
    text = "\n\n".join(s.text for s in sections) if sections else None
    return CompiledPrompt(sections=tuple(sections), text=text)


def compile_system_prompt(inputs: PromptInputs) -> str | None:
    """Assemble the final system prompt. Returns ``None`` only when every
    section is disabled/empty (so callers can distinguish "no prompt at all")."""
    return compile_prompt(inputs).text
