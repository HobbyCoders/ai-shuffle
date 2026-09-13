"""Fluid-native instruction, skill, and command discovery.

Fluid reads only its own ``AGENTS.md`` conventions during normal operation.
Provider-specific files are intentionally not discovered here: users can make
an explicit, non-destructive copy with :mod:`fluid.context.settings_import`.

What is discovered
------------------
Global (per user):
  ~/.fluid/AGENTS.md

Project (walk up from cwd to the git root, else the filesystem root):
  AGENTS.md, AGENTS.local.md, AGENT.md, .fluid/AGENTS.md, .fluid/AGENTS.local.md,
  .agents/AGENTS.md
  AGENTS.override.md replaces AGENTS.md in the same directory, and
  .fluid/AGENTS.override.md replaces .fluid/AGENTS.md. AGENTS.local.md layers
  after AGENTS.md (the gitignored personal file, CLAUDE.local.md's role).

Skills use ``~/.fluid/skills/<name>/SKILL.md`` globally and
``<directory>/.fluid/skills/<name>/SKILL.md`` (or the codex-native
``.agents/skills`` tree) at each project scope. They are separate from
code-mode's Python ``SkillStore`` files, even though both use the same default
global root. Plugin skills/commands join discovery namespaced
``<plugin>:<name>`` when the caller opts in (``include_plugins``).

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
# a scope. ``AGENTS.local.md`` follows ``AGENTS.md`` so personal rules layer
# over shared ones.
_AGENTS_NAMES = (
    "AGENTS.md",
    "AGENTS.local.md",
    "AGENT.md",
    ".fluid/AGENTS.md",
    ".fluid/AGENTS.local.md",
    ".agents/AGENTS.md",
)
_AGENTS_OVERRIDE = "AGENTS.override.md"
# Slot → the override file that takes it outright when present (codex
# convention: an override replaces, it does not stack).
_AGENTS_OVERRIDES = {
    "AGENTS.md": _AGENTS_OVERRIDE,
    ".fluid/AGENTS.md": ".fluid/" + _AGENTS_OVERRIDE,
}

# Per-file size caps. Instruction files enter the cached prompt prefix, so an
# unbounded AGENTS.md silently eats the window; the cap keeps the head (where
# the load-bearing rules usually live) and a slice of the tail, and the marker
# names the exact path so the model can Read the elided middle on demand —
# recoverable, never silently lossy. Nested files ride tool results every time
# a directory is first touched, so their cap is tighter.
INSTRUCTION_FILE_MAX_CHARS = 24_000
NESTED_FILE_MAX_CHARS = 8_000
_CAP_HEAD_RATIO = 0.7
_CAP_TAIL_RATIO = 0.2


def _cap_content(content: str, path: str, max_chars: int) -> str:
    """Head/tail truncation with a recovery marker naming ``path``."""
    if len(content) <= max_chars:
        return content
    head = content[: int(max_chars * _CAP_HEAD_RATIO)]
    tail = content[-int(max_chars * _CAP_TAIL_RATIO):]
    return (
        f"{head}\n\n[...instruction file truncated: kept {len(head)}+{len(tail)} "
        f"of {len(content)} chars. The middle is omitted — Read {path} for the "
        f"full text.]\n\n{tail}"
    )


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

    ``AGENTS.override.md`` takes ``AGENTS.md``'s slot outright, and
    ``.fluid/AGENTS.override.md`` takes ``.fluid/AGENTS.md``'s — the codex
    override convention replaces rather than stacks. ``AGENTS.local.md`` is
    untouched by an override: it layers after whichever file holds the slot.
    """
    names: list[str] = []
    for rel in _AGENTS_NAMES:
        override = _AGENTS_OVERRIDES.get(rel)
        if override is not None and (directory / override).is_file():
            names.append(override)
        else:
            names.append(rel)
    return tuple(names)


def _files_in_dir(directory: Path, scope: str) -> list[InstructionFile]:
    """Discover Fluid-native instruction files under ``directory``.

    Content is @-import-expanded, screened for prompt-injection patterns
    (:mod:`fluid.context.screening` — these files enter the prompt verbatim,
    so a poisoned one must be caught before injection, not after), and capped
    to the per-scope size limit with a Read-back marker.
    """
    from fluid.context.screening import screen

    limit = NESTED_FILE_MAX_CHARS if scope == "nested" else INSTRUCTION_FILE_MAX_CHARS
    found: list[InstructionFile] = []
    for rel in _agents_names_for(directory):
        p = directory / rel
        if p.is_file():
            content = screen(expand_imports(_read(p), p), str(p))
            found.append(
                InstructionFile(
                    path=str(p),
                    scope=scope,
                    kind=_kind_for(rel),
                    content=_cap_content(content, str(p), limit),
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
    from fluid.context.screening import screen

    path = Path.home() / ".fluid" / "AGENTS.md"
    if not path.is_file():
        return []
    content = screen(expand_imports(_read(path), path), str(path))
    return [
        InstructionFile(
            path=str(path),
            scope="global",
            kind="agents",
            content=_cap_content(content, str(path), INSTRUCTION_FILE_MAX_CHARS),
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


_BLOCK_INDICATOR = re.compile(r"^([>|])([+-]?)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _block_scalar(lines: list[str], style: str, chomp: str) -> str:
    """Text of a ``|`` (literal) or ``>`` (folded) block.

    Lines are dedented by the shallowest indent among them. Literal keeps
    every line break; folded joins lines with spaces and turns a blank line
    into a paragraph break. Trailing line breaks follow the chomping
    indicator: ``-`` drops them, ``+`` keeps them all, neither keeps one.
    """
    kept = list(lines)
    trailing = 0
    while kept and not kept[-1].strip():
        kept.pop()
        trailing += 1
    if not kept:
        return ""
    base = min(_indent(line) for line in kept if line.strip())
    body = [line[base:] if line.strip() else "" for line in kept]
    if style == "|":
        text = "\n".join(body)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for line in body:
            if line.strip():
                current.append(line.strip())
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    if chomp == "-":
        return text
    if chomp == "+":
        return text + "\n" * (trailing + 1)
    return text + "\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` frontmatter and parse its flat ``key: value`` fields.

    Returns ``(fields, body)``. Understood without a YAML dependency: scalar
    values, inline ``[a, b]`` arrays (see :func:`_frontmatter_value`), block
    scalars (``>``, ``>-``, ``>+``, ``|``, ``|-``, ``|+``), and a plain value
    continued on more-indented lines (joined with spaces). A line is a new
    field only when it sits at the first field's indent; a deeper line always
    belongs to the field above it, so a wrapped description that happens to
    contain ``tools: Bash`` cannot become a field.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = len(lines)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    block = lines[1:end]
    body = "\n".join(lines[end + 1:])
    fields: dict[str, Any] = {}
    base: int | None = None
    i = 0
    while i < len(block):
        raw = block[i]
        if not raw.strip():
            i += 1
            continue
        indent = _indent(raw)
        if base is None:
            base = indent
        key, sep, rest = raw.partition(":")
        key = key.strip()
        if indent > base or not sep or not key:
            i += 1  # a continuation with no field above it, or not a field at all
            continue
        j = i + 1
        while j < len(block) and (not block[j].strip() or _indent(block[j]) > base):
            j += 1
        continuation = block[i + 1:j]
        rest = rest.strip()
        indicator = _BLOCK_INDICATOR.match(rest)
        if indicator:
            fields[key] = _block_scalar(continuation, indicator.group(1), indicator.group(2))
        else:
            parts = [rest] if rest else []
            parts.extend(line.strip() for line in continuation if line.strip())
            fields[key] = _frontmatter_value(" ".join(parts))
        i = j
    return fields, body


def _plugins_for(
    cwd: str | os.PathLike[str], plugins: list[Any] | None
) -> list[Any]:
    """Resolve the plugin list for a discovery call.

    ``None`` means "discover them" (lazy import — :mod:`fluid.context.plugins`
    imports this module, so the dependency must stay one-way at import time);
    a caller that already discovered plugins passes them to avoid re-reading
    the settings stack per index.
    """
    if plugins is not None:
        return list(plugins)
    from fluid.context.plugins import discover_plugins

    return discover_plugins(cwd)


def _skills_in(base: Path, scope: str) -> dict[str, Skill]:
    """``<base>/*/SKILL.md`` keyed by skill name (frontmatter, else dir name).

    Deliberately distinct from ``SkillStore``, which may place Python modules
    directly in ``~/.fluid/skills``: only ``<name>/SKILL.md`` counts, so those
    modules never become prompt skills. Within one directory the first name
    in sorted order keeps the key.
    """
    out: dict[str, Skill] = {}
    if not base.is_dir():
        return out
    for skill_md in sorted(base.glob("*/SKILL.md")):
        fields, _ = parse_frontmatter(_read(skill_md))
        name = fields.get("name") or skill_md.parent.name
        out.setdefault(
            name,
            Skill(
                name=name,
                description=fields.get("description", ""),
                path=str(skill_md),
                scope=scope,
                body_loaded=False,
            ),
        )
    return out


def _commands_in(base: Path, scope: str) -> dict[str, Command]:
    """``<base>/*.md`` keyed by file stem (non-recursive)."""
    out: dict[str, Command] = {}
    if not base.is_dir():
        return out
    for cmd in sorted(base.glob("*.md")):
        out.setdefault(cmd.stem, Command(name=cmd.stem, path=str(cmd), scope=scope))
    return out


def skill_search_roots(
    cwd: str | os.PathLike[str], *, include_global: bool = True,
    include_project: bool = True,
) -> list[tuple[Path, str]]:
    """Skill directories in discovery order, paired with their setting scope."""
    roots: list[tuple[Path, str]] = []
    if include_project:
        for directory in _walk_up_dirs(Path(cwd)):
            roots.extend((
                (directory / ".agents" / "skills", "project"),
                (directory / ".fluid" / "skills", "project"),
            ))
    if include_global:
        roots.append((Path.home() / ".fluid" / "skills", "global"))
    return roots


def discover_skills(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
    include_plugins: bool = False,
    plugins: list[Any] | None = None,
) -> list[Skill]:
    """Discover ``<skills-dir>/*/SKILL.md`` and parse name+description only.

    One entry per name, sorted by name. Precedence mirrors
    :func:`fluid.context.agents.discover_agents`: a project skill beats a
    global one of the same name, the project directory nearest the cwd beats an
    outer one, and in one directory ``.fluid/skills`` beats ``.agents/skills``.

    Bodies are left unread (``body_loaded=False``) — progressive disclosure:
    the ``Skill`` builtin loads a body on demand. ``include_plugins`` appends
    enabled plugins' skills, namespaced ``<plugin>:<name>`` (pass ``plugins``
    to reuse an already-discovered list). Provider homes are never read.
    """
    by_name: dict[str, Skill] = {}
    for root, scope in skill_search_roots(
        cwd, include_global=include_global, include_project=include_project,
    ):
        found = _skills_in(root, scope)
        if scope == "project":
            by_name.update(found)
        else:
            for name, skill in found.items():
                by_name.setdefault(name, skill)
    out = [by_name[name] for name in sorted(by_name)]
    if include_plugins:
        from fluid.context.plugins import plugin_skills

        out.extend(
            s for s in plugin_skills(_plugins_for(cwd, plugins)) if s.name not in by_name
        )
    return out


def discover_commands(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_project: bool = True,
    include_plugins: bool = False,
    plugins: list[Any] | None = None,
) -> list[Command]:
    """Discover native slash-command files from ``.fluid`` / ``.agents``.

    Same one-per-name precedence as :func:`discover_skills`: project over
    global, nearest directory over outer, ``.fluid`` over ``.agents``; sorted
    by name. The REPL executes these via :func:`render_command`.
    ``include_plugins`` appends enabled plugins' commands, namespaced
    ``<plugin>:<name>``.
    """
    by_name: dict[str, Command] = {}
    if include_project:
        for directory in _walk_up_dirs(Path(cwd)):
            by_name.update(_commands_in(directory / ".agents" / "commands", "project"))
            by_name.update(_commands_in(directory / ".fluid" / "commands", "project"))
    if include_global:
        for name, cmd in _commands_in(Path.home() / ".fluid" / "commands", "global").items():
            by_name.setdefault(name, cmd)
    out = [by_name[name] for name in sorted(by_name)]
    if include_plugins:
        from fluid.context.plugins import plugin_commands

        out.extend(
            c for c in plugin_commands(_plugins_for(cwd, plugins)) if c.name not in by_name
        )
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
