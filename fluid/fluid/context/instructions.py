"""Fluid-native instruction, skill, and command discovery.

Fluid reads only its own ``AGENTS.md`` conventions during normal operation.
Provider-specific files are intentionally not discovered here: users can make
an explicit, non-destructive copy with :mod:`fluid.context.settings_import`.

What is discovered
------------------
Global (per user):
  ~/.fluid/AGENTS.md

Project (walk up from cwd to the git root, else the filesystem root):
  AGENTS.md, AGENT.md, AGENTS.override.md, .agents/AGENTS.md
  AGENTS.override.md replaces root-level AGENTS.md in the same directory.

Skills use ``~/.fluid/skills/<name>/SKILL.md`` globally and
``<directory>/.agents/skills/<name>/SKILL.md`` at each project scope. They are
separate from code-mode's Python ``SkillStore`` files, even though both use the
same default global root.

Every instruction block carries a ``# From: <path>`` provenance header. Global
entries come first, followed by project entries from git root to cwd.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ordered per-directory candidates. Order fixes the order blocks appear within
# a scope. ``AGENTS.override.md`` replaces only the root AGENTS.md slot.
_AGENTS_NAMES = ("AGENTS.md", "AGENT.md", ".agents/AGENTS.md")
_AGENTS_OVERRIDE = "AGENTS.override.md"


def _kind_for(rel_name: str) -> str:
    return "agent" if os.path.basename(rel_name) == "AGENT.md" else "agents"


@dataclass(frozen=True, kw_only=True)
class InstructionFile:
    path: str
    scope: str  # "global" | "project" | "nested"
    kind: str  # "agents" | "agent"
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

    return _map_unfenced_lines(text, lambda line: _expand_outside_code_spans(line, _expand_token))


def _map_unfenced_lines(text: str, fn) -> str:
    """Apply ``fn`` per line, passing through fenced code blocks (``` / ~~~)."""
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
        out.append(fn(line))
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
    """Discover Fluid-native instruction files under ``directory``."""
    found: list[InstructionFile] = []
    for rel in _agents_names_for(directory):
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
    """The single Fluid global instruction file: ``~/.fluid/AGENTS.md``.

    XDG is reserved for TOML configuration. Legacy Claude/Codex instruction
    files remain inert until explicitly imported.
    """
    path = Path.home() / ".fluid" / "AGENTS.md"
    if not path.is_file():
        return []
    return [
        InstructionFile(
            path=str(path),
            scope="global",
            kind="agents",
            content=expand_imports(_read(path), path),
        )
    ]


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

    When the agent touches ``<subdir>/foo.py``, ``<subdir>``'s own native
    AGENTS files apply on top of the project stack. This returns those files;
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
    AGENTS-family file separately instead of only the merged stack.
    """
    files = discover_instruction_files(
        cwd, include_global=include_global, include_project=include_project
    )
    blocks: list[tuple[InstructionFile, str]] = []
    # Track content already emitted per directory so identical native siblings
    # are noted rather than repeated.
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
    byte-identical dedup note when two native instruction files in the same
    directory carry identical content.
    """
    return "\n\n".join(
        block
        for _, block in build_instruction_blocks(
            cwd, include_global=include_global, include_project=include_project
        )
    )


# ─── frontmatter (tiny hand-rolled splitter — no yaml dep) ────────────────────


def _unquote(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


def _frontmatter_value(raw: str) -> str | list[str]:
    """One frontmatter value: an inline ``[a, b, c]`` array, else a scalar.

    Minimal by design (ruling CE6) — an agent's ``tools: [Read, Grep]`` is the
    only list shape the ecosystem actually writes, and a real YAML dependency
    would buy nothing else the harness reads. A value that is not bracketed is
    returned exactly as before, so every existing caller is unaffected.
    """
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        return [item for part in value[1:-1].split(",") if (item := _unquote(part))]
    return _unquote(value)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` frontmatter and parse simple ``key: value`` lines.

    Returns ``(fields, body)``. Only flat ``key: value`` pairs are understood —
    enough for a skill's ``name``/``description`` (~100-token) header — plus
    inline ``[a, b]`` arrays (see :func:`_frontmatter_value`), so an agent's
    ``tools:`` list parses without a YAML dependency.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, Any] = {}
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
                fields[key] = _frontmatter_value(value)
    body = "\n".join(lines[body_start:])
    return fields, body


def _skill_dirs(cwd: str | os.PathLike[str], include_global: bool) -> list[tuple[Path, str]]:
    """Native markdown skill roots, deliberately distinct from ``SkillStore``.

    ``SkillStore`` may place Python modules directly in ``~/.fluid/skills``;
    this scan matches only ``<name>/SKILL.md`` and will never treat those modules
    as prompt skills.
    """
    dirs: list[tuple[Path, str]] = []
    if include_global:
        dirs.append((Path.home() / ".fluid" / "skills", "global"))
    for directory in _walk_up_dirs(Path(cwd)):
        dirs.append((directory / ".agents" / "skills", "project"))
    return dirs


def discover_skills(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_plugins: bool = False,
) -> list[Skill]:
    """Discover ``<skills-dir>/*/SKILL.md`` and parse name+description only.

    Bodies are left unread (``body_loaded=False``) — progressive disclosure:
    the ``Skill`` builtin loads a body on demand. ``include_plugins`` is retained
    as an ignored compatibility argument; provider plugin homes are never read.
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
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_plugins: bool = False,
) -> list[Command]:
    """Discover native slash-command files from ``.fluid`` / ``.agents``.

    The REPL executes these via :func:`render_command`. ``include_plugins`` is
    retained as an ignored compatibility argument.
    """
    out: list[Command] = []
    seen: set[str] = set()
    bases: list[tuple[Path, str]] = []
    if include_global:
        bases.append((Path.home() / ".fluid" / "commands", "global"))
    for directory in _walk_up_dirs(Path(cwd)):
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


# ─── slash-command rendering ─────────────────────────────────────────────────

# ``$ARGUMENTS`` or ``$<n>`` in one alternation: a single pass, so argument
# values that themselves contain ``$1`` are never re-substituted.
_SUBST_RE = re.compile(r"\$ARGUMENTS\b|\$(\d+)")

# Inline bash: ``!`command```, single line, non-empty.
_BASH_RE = re.compile(r"!`([^`\n]+)`")

# Positional-argument tokenizer: whitespace-separated words, with double- or
# single-quoted grouping. Deliberately no backslash escaping (unlike shlex) so
# Windows paths pass through intact.
_ARG_TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')

INLINE_BASH_TIMEOUT_S = 30.0


def split_command_args(arg: str) -> list[str]:
    """Split a slash-command argument string into positional arguments."""
    return [
        next(g for g in match.groups() if g is not None)
        for match in _ARG_TOKEN_RE.finditer(arg)
    ]


def _bash_allowed(allowed: Any) -> bool:
    """Claude Code gates ``!`cmd``` execution on frontmatter ``allowed-tools``
    granting Bash (e.g. ``Bash(git status:*)``); mirror that as a whole-file
    yes/no rather than per-pattern matching."""
    items = allowed if isinstance(allowed, list) else [allowed]
    return any("bash" in str(item).lower() for item in items if item)


def _run_inline_bash(command: str, cwd: str, timeout: float) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.stdout.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        return f"[command timed out after {timeout:g}s]"
    except OSError:
        return "[command failed to run]"


def _file_references(body: str) -> list[str]:
    """``@path`` tokens in ``body``, in order, deduped — same placement rules as
    instruction imports (start-of-line/whitespace-preceded, outside code)."""
    refs: list[str] = []

    def _record(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw not in refs:
            refs.append(raw)
        return match.group(0)

    _map_unfenced_lines(body, lambda line: _expand_outside_code_spans(line, _record))
    return refs


def render_command_blocks(
    text: str,
    arg: str,
    *,
    cwd: str | os.PathLike[str],
    timeout: float = INLINE_BASH_TIMEOUT_S,
) -> list[str]:
    """Render a slash-command file into prompt blocks for one turn (Claude
    Code conventions). Never raises — anything unresolvable stays literal.

    * ``$ARGUMENTS`` → the full argument string; ``$1``/``$2``… → positional
      arguments (quoted grouping honored, missing → empty string).
    * ``!`cmd``` → replaced with the command's combined output, run via the
      shell in ``cwd`` — only when frontmatter ``allowed-tools`` grants Bash,
      and never inside fenced code blocks.
    * ``@path`` (cwd-relative, ``~``/absolute ok, outside code) → the file's
      contents follow the body as their own block under a provenance header;
      the token itself stays in place so the prose still reads.

    The body is ``blocks[0]``; each ``@file`` dump is its own block. Keeping
    them separate lets the compaction view stub an oversize dump without ever
    hiding the instruction text (each block becomes one Text content block —
    intake stubbing is per-block).
    """
    fields, body = parse_frontmatter(text)
    args = split_command_args(arg)

    def _sub(match: re.Match[str]) -> str:
        if match.group(1) is None:
            return arg
        index = int(match.group(1))
        return args[index - 1] if 1 <= index <= len(args) else ""

    body = _SUBST_RE.sub(_sub, body)

    # Collect @-references before bash runs so command output cannot mint new ones.
    refs = _file_references(body)

    if _bash_allowed(fields.get("allowed-tools")):
        body = _map_unfenced_lines(
            body,
            lambda line: _BASH_RE.sub(
                lambda m: _run_inline_bash(m.group(1), str(cwd), timeout), line
            ),
        )

    blocks = [body.strip()]
    for ref in refs:
        target = _import_target(ref, Path(cwd))
        try:
            content = _read_opt(target) if target.is_file() else None
        except (OSError, ValueError):
            content = None
        if content is not None:
            blocks.append(f"Contents of {ref}:\n{content.rstrip()}")
    return blocks


def render_command(
    text: str,
    arg: str,
    *,
    cwd: str | os.PathLike[str],
    timeout: float = INLINE_BASH_TIMEOUT_S,
) -> str:
    """:func:`render_command_blocks` joined to one string, for callers that
    want the flat prompt (tests, plain-text hosts)."""
    return "\n\n".join(render_command_blocks(text, arg, cwd=cwd, timeout=timeout))
