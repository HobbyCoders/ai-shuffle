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
  CLAUDE.md, AGENTS.md, AGENT.md
  .claude/CLAUDE.md, .agents/AGENTS.md

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
  * ``discover_skills`` returns ``Skill`` records with ``body_loaded=False``;
    the CLI advertises name+description in the system prompt and the ``Skill``
    builtin loads a body on demand (progressive disclosure).
  * ``discover_commands`` — the REPL runs ``/name args`` from a discovered
    command file with ``$ARGUMENTS`` substituted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Ordered per-directory candidates. Order is significant: it fixes the order
# blocks appear within a single scope and which file "wins" the byte-identical
# dedup (the earlier one is kept, the later one noted).
_CLAUDE_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md")
_AGENTS_NAMES = ("AGENTS.md", "AGENT.md", ".agents/AGENTS.md")


def _kind_for(rel_name: str) -> str:
    base = os.path.basename(rel_name)
    if base == "CLAUDE.md":
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
    scope: str = "project"  # "global" | "project"
    body_loaded: bool = False  # progressive disclosure: body read on demand


@dataclass(frozen=True, kw_only=True)
class Command:
    name: str
    path: str
    scope: str = "project"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _files_in_dir(directory: Path, scope: str) -> list[InstructionFile]:
    """Discover instruction files directly under ``directory`` in canonical order."""
    found: list[InstructionFile] = []
    for rel in (*_CLAUDE_NAMES, *_AGENTS_NAMES):
        p = directory / rel
        if p.is_file():
            found.append(
                InstructionFile(
                    path=str(p), scope=scope, kind=_kind_for(rel), content=_read(p)
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
    candidates = [
        (home / ".claude" / "CLAUDE.md", "claude"),
        (config_base / "fluid" / "AGENTS.md", "agents"),
        (home / ".codex" / "AGENTS.md", "agents"),
    ]
    out: list[InstructionFile] = []
    for path, kind in candidates:
        if path.is_file():
            out.append(
                InstructionFile(path=str(path), scope="global", kind=kind, content=_read(path))
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
    files = discover_instruction_files(
        cwd, include_global=include_global, include_project=include_project
    )
    blocks: list[str] = []
    # Track content already emitted per directory so an identical sibling
    # (CLAUDE.md vs AGENTS.md) is noted, not repeated.
    seen: dict[tuple[str, str], str] = {}  # (dir, content) -> emitting path
    for f in files:
        directory = str(Path(f.path).parent)
        key = (directory, f.content)
        prior = seen.get(key)
        if prior is not None and f.content.strip():
            blocks.append(
                f"# From: {f.path}\n# (byte-identical to {prior}; not repeated)"
            )
            continue
        seen[key] = f.path
        blocks.append(f"# From: {f.path}\n{f.content.rstrip()}")
    return "\n\n".join(blocks)


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


def discover_skills(
    cwd: str | os.PathLike[str], *, include_global: bool = True
) -> list[Skill]:
    """Discover ``<skills-dir>/*/SKILL.md`` and parse name+description only.

    Bodies are left unread (``body_loaded=False``) — progressive disclosure:
    the ``Skill`` builtin loads a body on demand.
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
    return out


def discover_commands(
    cwd: str | os.PathLike[str], *, include_global: bool = True
) -> list[Command]:
    """Discover slash-command files: ``.claude/commands/*.md`` and
    ``.agents/commands/*.md``. The REPL executes these (``$ARGUMENTS`` substituted)."""
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
    return out
