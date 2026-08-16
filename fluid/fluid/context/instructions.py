"""Instruction-file / skill / command discovery — ecosystem compatibility.

This is locked decision #5: setups authored for Claude Code, Codex, or any
AGENTS.md-speaking harness must port over unchanged. The discovery layer accepts
BOTH naming conventions and merges them into one "instruction stack" the
ContextManager treats as a single system-prompt input.

What is discovered
------------------
Global (per user):
  ~/.claude/CLAUDE.md
  ~/.config/fluid/AGENTS.md      (XDG_CONFIG_HOME honored)
  ~/.codex/AGENTS.md            (only if present)

Project (walk up from cwd to the git root, else the filesystem root):
  CLAUDE.md, CLAUDE.local.md, AGENTS.md, AGENT.md
  .claude/CLAUDE.md, .agents/AGENTS.md
  AGENTS.override.md — replaces AGENTS.md in the same directory (codex)

@-imports:
  ``@path`` tokens inside a discovered instruction file inline that file's
  content (see :func:`expand_imports`) — so a CLAUDE.md that says
  ``@communication-style.md`` ports over unchanged.

Merge rule (per the plan):
  Global entries come first, then project entries ordered outermost → cwd (so
  the most specific scope is appended last). When BOTH a CLAUDE.md and an
  AGENTS.md exist in the SAME directory scope, both are concatenated — neither
  is silently dropped — with a dedup note emitted instead of a second copy when
  their bytes are identical. Every block carries a ``# From: <path>`` provenance
  header.

Wiring (M6 seams, now closed):
  * ``nested_instructions(path)`` — injected by ``NativeLoop._dispatch_tool``:
    a tool call touching a path whose directory carries its own instruction
    file rides those instructions into that tool result, once per session.
  * ``discover_skills``/``discover_commands`` also surface the components of
    enabled Claude Code plugins (``fluid.context.plugins``, ruling CE5), named
    ``<plugin>:<component>`` and appended after the user's own.
  * ``discover_skills`` returns ``Skill`` records with ``body_loaded=False``;
    the CLI advertises name+description in the system prompt and the ``Skill``
    builtin loads a body on demand (progressive disclosure).
  * ``discover_commands`` — the REPL runs ``/name args`` from a discovered
    command file with ``$ARGUMENTS`` substituted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Ordered per-directory candidates. Order is significant: it fixes the order
# blocks appear within a single scope and which file "wins" the byte-identical
# dedup (the earlier one is kept, the later one noted).
#
# ``CLAUDE.local.md`` is Claude Code's gitignored per-developer sibling of
# CLAUDE.md; it loads *after* the CLAUDE.md variants in the same directory.
_CLAUDE_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
_AGENTS_NAMES = ("AGENTS.md", "AGENT.md", ".agents/AGENTS.md")

# Codex convention: an ``AGENTS.override.md`` REPLACES the ``AGENTS.md`` in the
# same directory (it does not stack on top of it).
_AGENTS_OVERRIDE = "AGENTS.override.md"

_CLAUDE_BASENAMES = ("CLAUDE.md", "CLAUDE.local.md")


def _kind_for(rel_name: str) -> str:
    base = os.path.basename(rel_name)
    if base in _CLAUDE_BASENAMES:
        return "claude"
    if base == "AGENT.md":
        return "agent"
    return "agents"


@dataclass(frozen=True, kw_only=True)
class InstructionFile:
    path: str
    scope: str  # "global" | "project" | "nested"
    kind: str  # "claude" | "agents" | "agent"
    content: str = ""


@dataclass(frozen=True, kw_only=True)
class Skill:
    name: str
    description: str
    path: str
    scope: str = "project"  # "global" | "project" | "plugin"
    body_loaded: bool = False  # progressive disclosure: body read on demand


@dataclass(frozen=True, kw_only=True)
class Command:
    name: str
    path: str
    scope: str = "project"  # "global" | "project" | "plugin"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_opt(path: Path) -> str | None:
    """``_read`` that distinguishes "empty file" from "could not read"."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


# ─── @-imports ───────────────────────────────────────────────────────────────

# Claude Code's ``@path`` import token: at the start of a line or preceded by
# whitespace, path = everything up to the next whitespace.
_IMPORT_RE = re.compile(r"(?<![^\s])@(\S+)")

# Hops, not nesting levels: the root file is hop 0, so a token sitting in a file
# already five hops deep is left literal.
MAX_IMPORT_DEPTH = 5


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _import_target(raw: str, source_dir: Path) -> Path:
    """Resolve an ``@`` token's path: ``~/`` expands, absolute stays, else it is
    relative to the *containing file's* directory (not the cwd)."""
    if raw.startswith("~"):
        return Path(raw).expanduser()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return source_dir / raw


def expand_imports(
    text: str,
    source_path: Path,
    *,
    _depth: int = 0,
    _seen: frozenset[Path] | None = None,
) -> str:
    """Expand Claude Code ``@path`` import tokens in ``text``.

    A token expands only when it sits at the start of a line or is preceded by
    whitespace, and is outside both fenced code blocks (``` / ~~~) and inline
    code spans — so documentation *about* imports stays inert. The referenced
    file's content replaces just the token; the rest of the line survives.

    Expansion is recursive up to :data:`MAX_IMPORT_DEPTH` hops and is guarded by
    the set of already-visited files, so a cycle cuts silently. Anything that
    cannot be expanded — too deep, cyclic, missing, unreadable — keeps its
    literal token. This never raises: instruction assembly must not be able to
    fail on a stray ``@``.
    """
    if "@" not in text:
        return text

    seen = _seen if _seen is not None else frozenset({_resolved(source_path)})
    source_dir = source_path.parent

    def _expand_token(match: re.Match[str]) -> str:
        literal = match.group(0)
        if _depth >= MAX_IMPORT_DEPTH:
            return literal
        target = _import_target(match.group(1), source_dir)
        resolved = _resolved(target)
        if resolved in seen:  # cycle — cut silently
            return literal
        if not target.is_file():
            return literal
        content = _read_opt(target)
        if content is None:
            return literal
        return expand_imports(
            content,
            target,
            _depth=_depth + 1,
            _seen=seen | {resolved},
        ).rstrip("\n")

    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            out.append(line)
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            out.append(line)
            continue
        out.append(_expand_outside_code_spans(line, _expand_token))
    return "\n".join(out)


def _expand_outside_code_spans(line: str, expand) -> str:
    """Apply ``expand`` to the parts of ``line`` that are not inside `backticks`.

    Splitting on backticks puts the code-span interiors at odd indices; the even
    segments are live text. Segments after the first cannot begin a token —
    their first character is preceded by a backtick, not whitespace — so a
    sentinel guards the lookbehind.
    """
    if "@" not in line:
        return line
    parts = line.split("`")
    for i in range(0, len(parts), 2):
        if "@" not in parts[i]:
            continue
        if i == 0:
            parts[i] = _IMPORT_RE.sub(expand, parts[i])
        else:
            parts[i] = _IMPORT_RE.sub(expand, "\x00" + parts[i])[1:]
    return "`".join(parts)


def _agents_names_for(directory: Path) -> tuple[str, ...]:
    """The AGENTS-family candidates for ``directory``.

    ``AGENTS.override.md`` takes ``AGENTS.md``'s slot outright — the codex
    override convention replaces rather than stacks.
    """
    if (directory / _AGENTS_OVERRIDE).is_file():
        return (_AGENTS_OVERRIDE, *_AGENTS_NAMES[1:])
    return _AGENTS_NAMES


def _files_in_dir(directory: Path, scope: str) -> list[InstructionFile]:
    """Discover instruction files directly under ``directory`` in canonical order."""
    found: list[InstructionFile] = []
    for rel in (*_CLAUDE_NAMES, *_agents_names_for(directory)):
        p = directory / rel
        if p.is_file():
            found.append(
                InstructionFile(
                    path=str(p),
                    scope=scope,
                    kind=_kind_for(rel),
                    content=expand_imports(_read(p), p),
                )
            )
    return found


def _git_root(cwd: Path) -> Path | None:
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return None


def _walk_up_dirs(cwd: Path) -> list[Path]:
    """Directories from the outermost boundary (git root, else fs root) down to
    ``cwd`` — so blocks end up ordered least-specific → most-specific."""
    cwd = cwd.resolve()
    root = _git_root(cwd) or Path(cwd.anchor or "/")
    chain: list[Path] = []
    d = cwd
    while True:
        chain.append(d)
        if d == root or d.parent == d:
            break
        d = d.parent
    chain.reverse()
    return chain


# ─── discovery ───────────────────────────────────────────────────────────────


def discover_global_instruction_files() -> list[InstructionFile]:
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_base = Path(xdg) if xdg else home / ".config"
    codex = home / ".codex"
    # Same override rule as the project walk: ~/.codex/AGENTS.override.md
    # replaces ~/.codex/AGENTS.md rather than stacking with it.
    codex_agents = codex / _AGENTS_OVERRIDE
    if not codex_agents.is_file():
        codex_agents = codex / "AGENTS.md"
    candidates = [
        (home / ".claude" / "CLAUDE.md", "claude"),
        (config_base / "fluid" / "AGENTS.md", "agents"),
        (codex_agents, "agents"),
    ]
    out: list[InstructionFile] = []
    for path, kind in candidates:
        if path.is_file():
            out.append(
                InstructionFile(
                    path=str(path),
                    scope="global",
                    kind=kind,
                    content=expand_imports(_read(path), path),
                )
            )
    return out


def discover_project_instruction_files(cwd: str | os.PathLike[str]) -> list[InstructionFile]:
    out: list[InstructionFile] = []
    for directory in _walk_up_dirs(Path(cwd)):
        out.extend(_files_in_dir(directory, "project"))
    return out


def discover_instruction_files(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
) -> list[InstructionFile]:
    """Global (first) + project (outermost → cwd) instruction files."""
    out: list[InstructionFile] = []
    if include_global:
        out.extend(discover_global_instruction_files())
    if include_project:
        out.extend(discover_project_instruction_files(cwd))
    return out


def nested_instructions(path: str | os.PathLike[str]) -> list[InstructionFile]:
    """Instruction files that apply to a file/dir the agent is reading.

    When the agent touches ``<subdir>/foo.py``, ``<subdir>``'s own CLAUDE.md /
    AGENTS.md apply on top of the project stack. This returns those files;
    ``NativeLoop._dispatch_tool`` appends them to that call's tool result.
    """
    p = Path(path)
    directory = p if p.is_dir() else p.parent
    return [
        InstructionFile(path=f.path, scope="nested", kind=f.kind, content=f.content)
        for f in _files_in_dir(directory, "nested")
    ]


def build_instruction_blocks(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
) -> list[tuple[InstructionFile, str]]:
    """The instruction stack as ``(file, rendered block)`` pairs.

    Same discovery, ordering and dedup rules as :func:`build_instruction_stack`
    — which is this function joined with blank lines — but keeps per-file
    attribution, so a caller (the context-usage breakdown) can price each
    CLAUDE.md/AGENTS.md separately instead of only the merged stack.
    """
    files = discover_instruction_files(
        cwd, include_global=include_global, include_project=include_project
    )
    blocks: list[tuple[InstructionFile, str]] = []
    # Track content already emitted per directory so an identical sibling
    # (CLAUDE.md vs AGENTS.md) is noted, not repeated.
    seen: dict[tuple[str, str], str] = {}  # (dir, content) -> emitting path
    for f in files:
        directory = str(Path(f.path).parent)
        key = (directory, f.content)
        prior = seen.get(key)
        if prior is not None and f.content.strip():
            blocks.append(
                (f, f"# From: {f.path}\n# (byte-identical to {prior}; not repeated)")
            )
            continue
        seen[key] = f.path
        blocks.append((f, f"# From: {f.path}\n{f.content.rstrip()}"))
    return blocks


def build_instruction_stack(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
) -> str:
    """Concatenate discovered instruction files into one prompt string.

    Global → project ordering, ``# From: <path>`` provenance headers, and a
    byte-identical dedup note (rather than a duplicated body) when a CLAUDE.md and
    an AGENTS.md in the same directory carry identical content.
    """
    return "\n\n".join(
        block
        for _, block in build_instruction_blocks(
            cwd, include_global=include_global, include_project=include_project
        )
    )


# ─── frontmatter (tiny hand-rolled splitter — no yaml dep) ────────────────────


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` frontmatter and parse simple ``key: value`` lines.

    Returns ``(fields, body)``. Only flat ``key: value`` pairs are understood —
    enough for a skill's ``name``/``description`` (~100-token) header.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    body_start = len(lines)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        raw = lines[i]
        if ":" in raw:
            key, _, value = raw.partition(":")
            key = key.strip()
            if key:
                fields[key] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[body_start:])
    return fields, body


def _skill_dirs(cwd: str | os.PathLike[str], include_global: bool) -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    if include_global:
        dirs.append((Path.home() / ".claude" / "skills", "global"))
    for directory in _walk_up_dirs(Path(cwd)):
        dirs.append((directory / ".claude" / "skills", "project"))
        dirs.append((directory / ".agents" / "skills", "project"))
    return dirs


def _plugin_components(cwd: str | os.PathLike[str], attr: str) -> list:
    """Plugin-provided skills/commands (ruling CE5), or ``[]`` when plugins are
    absent. Imported lazily: ``fluid.context.plugins`` imports this module."""
    from fluid.context.plugins import discover_plugins

    return [item for plugin in discover_plugins(cwd) for item in getattr(plugin, attr)]


def discover_skills(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_plugins: bool = True,
) -> list[Skill]:
    """Discover ``<skills-dir>/*/SKILL.md`` and parse name+description only.

    Bodies are left unread (``body_loaded=False``) — progressive disclosure:
    the ``Skill`` builtin loads a body on demand.

    Enabled Claude Code plugins contribute their own skills last, named
    ``<plugin>:<skill>`` so they can never shadow a user's own (CE5).
    """
    out: list[Skill] = []
    seen: set[str] = set()
    for base, scope in _skill_dirs(cwd, include_global):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            resolved = str(skill_md.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            fields, _ = parse_frontmatter(_read(skill_md))
            name = fields.get("name") or skill_md.parent.name
            out.append(
                Skill(
                    name=name,
                    description=fields.get("description", ""),
                    path=str(skill_md),
                    scope=scope,
                    body_loaded=False,
                )
            )
    if include_plugins:
        for skill in _plugin_components(cwd, "skills"):
            resolved = str(Path(skill.path).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(skill)
    return out


def discover_commands(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_plugins: bool = True,
) -> list[Command]:
    """Discover slash-command files: ``.claude/commands/*.md`` and
    ``.agents/commands/*.md``. The REPL executes these (``$ARGUMENTS`` substituted).

    Enabled plugins contribute ``<plugin>:<command>`` entries last (CE5)."""
    out: list[Command] = []
    seen: set[str] = set()
    bases: list[tuple[Path, str]] = []
    if include_global:
        bases.append((Path.home() / ".claude" / "commands", "global"))
    for directory in _walk_up_dirs(Path(cwd)):
        bases.append((directory / ".claude" / "commands", "project"))
        bases.append((directory / ".agents" / "commands", "project"))
    for base, scope in bases:
        if not base.is_dir():
            continue
        for cmd in sorted(base.glob("*.md")):
            resolved = str(cmd.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(Command(name=cmd.stem, path=str(cmd), scope=scope))
    if include_plugins:
        for command in _plugin_components(cwd, "commands"):
            resolved = str(Path(command.path).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(command)
    return out
