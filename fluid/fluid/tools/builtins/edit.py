"""Edit — string replacement with read-before-write + uniqueness.

Enforces two of the tool-design-discipline rules:
- the file must have been Read this session (``ctx.read_files``);
- ``old_string`` must be unique in the file unless ``replace_all`` is set.

Matching is a cascade. An exact match wins; failing that, two shapes of
model transcription error are corrected rather than refused — a copied
``lineno<TAB>`` display prefix from Read, and a uniform leading-whitespace
offset (typically one extra tab, again from Read's separator). Anything else
fails with a diagnostic that names the line and the difference, because the
old terse "old_string not found in file" left models guessing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import read_text_preserving, resolve, write_text_with_newline
from fluid.tools.fragments import NO_VERIFY_READBACK, READ_BEFORE_EDIT
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

def describe(tools: frozenset[str] | None = None) -> str:
    """Edit's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    read_gate = (
        f"- {READ_BEFORE_EDIT} Without that Read the call fails.\n"
        if has("Read")
        else ""
    )
    prefix_clause = (
        " Do not include Read's line-number prefix (the leading number and "
        "tab) — that is display formatting, not file content.\n"
        if has("Read")
        else "\n"
    )
    verify_clause = (
        f"- After a successful Edit, {NO_VERIFY_READBACK}"
        if has("Read")
        else "- A successful Edit is confirmed by its result; a failed match "
        "would have returned an error instead."
    )
    return (
        "Perform an exact string replacement in an existing file. When you "
        "say you will edit or update file content, invoke Edit immediately.\n"
        "\n"
        "Rules:\n"
        + read_gate +
        "- old_string must match the file byte-for-byte, including "
        "indentation and line breaks."
        + prefix_clause +
        "- old_string must be unique in the file; if it matches more than "
        "once the edit fails. Keep it minimal — the changed lines plus just "
        "enough surrounding context to be unique; excess context wastes "
        "tokens and multiplies mismatch risk. Pass replace_all=true to "
        "change every occurrence instead (useful for renames).\n"
        "- If old_string differs only by a uniform indentation offset or a "
        "copied line-number prefix, Edit corrects it and tells you; any "
        "other mismatch fails with a diagnostic.\n"
        + verify_clause
    )


SPEC = ToolSpec(
    name="Edit",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or cwd-relative path of the file to modify.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact existing text to replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text (must differ from old_string).",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default false).",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    risk="write",
)

# Read renders text as ``lineno<TAB>content``; models sometimes copy the whole
# rendered line back into old_string.
_LINE_PREFIX = re.compile(r"^\d+\t")

_FALLBACK_NOTE = (
    " — note: matched after correcting leading whitespace/line-number prefix; "
    "copy text exactly from Read next time"
)


def _strip_line_prefixes(text: str) -> str | None:
    """Drop Read's ``lineno<TAB>`` prefix from every line that carries one.

    Returns ``None`` when no line carries one (nothing to correct).
    """
    lines = text.split("\n")
    if not any(_LINE_PREFIX.match(line) for line in lines):
        return None
    return "\n".join(_LINE_PREFIX.sub("", line) for line in lines)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _body(line: str) -> str:
    return line.rstrip("\r").lstrip()


def _describe_ws(ws: str) -> str:
    tabs = ws.count("\t")
    spaces = ws.count(" ")
    parts = []
    if tabs:
        parts.append(f"{tabs} tab{'s' if tabs != 1 else ''}")
    if spaces:
        parts.append(f"{spaces} space{'s' if spaces != 1 else ''}")
    if not parts:
        return "no indentation"
    return " + ".join(parts)


def _derive_prefix(old_lines: list[str], file_lines: list[str]) -> tuple[str, bool] | None:
    """Find the single whitespace prefix that separates two aligned runs.

    Returns ``(prefix, old_has_extra)`` when one string ``P`` explains every
    line — either ``old_indent == P + file_indent`` (the model added ``P``,
    the grok ``lineno<TAB>`` case) or ``file_indent == P + old_indent`` (the
    model dropped ``P``). Blank lines carry no indentation information and are
    skipped. ``None`` means the drift is not uniform.
    """
    extra: str | None = None
    missing: str | None = None
    extra_ok = missing_ok = True
    saw_content = False
    for old_line, file_line in zip(old_lines, file_lines):
        if not _body(old_line):
            continue
        saw_content = True
        o, f = _indent(old_line), _indent(file_line.rstrip("\r"))
        if extra_ok:
            if o.endswith(f):
                cand = o[: len(o) - len(f)]
                if extra is None:
                    extra = cand
                elif extra != cand:
                    extra_ok = False
            else:
                extra_ok = False
        if missing_ok:
            if f.endswith(o):
                cand = f[: len(f) - len(o)]
                if missing is None:
                    missing = cand
                elif missing != cand:
                    missing_ok = False
            else:
                missing_ok = False
    if not saw_content:
        return None
    if extra_ok and extra:
        return extra, True
    if missing_ok and missing:
        return missing, False
    if (extra_ok and extra == "") or (missing_ok and missing == ""):
        return "", True
    return None


def _apply_prefix(lines: list[str], prefix: str, old_has_extra: bool) -> list[str]:
    """Re-indent replacement lines to the file's convention."""
    if not prefix:
        return list(lines)
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif old_has_extra:
            out.append(line[len(prefix):] if line.startswith(prefix) else line)
        else:
            out.append(prefix + line)
    return out


def _find_runs(file_lines: list[str], old_lines: list[str]) -> list[tuple[int, str, bool]]:
    """Locate runs of file lines that match ``old_lines`` modulo indentation."""
    n = len(old_lines)
    runs: list[tuple[int, str, bool]] = []
    if n == 0 or n > len(file_lines):
        return runs
    bodies = [_body(line) for line in old_lines]
    for start in range(len(file_lines) - n + 1):
        window = file_lines[start:start + n]
        if any(_body(w) != b for w, b in zip(window, bodies)):
            continue
        derived = _derive_prefix(old_lines, window)
        if derived is None:
            continue
        runs.append((start, derived[0], derived[1]))
    return runs


def _diagnose(file_lines: list[str], old_lines: list[str]) -> str:
    """Explain, in the model's terms, why ``old_string`` did not match."""
    first_idx = next((i for i, line in enumerate(old_lines) if _body(line)), None)
    if first_idx is None:
        return (
            "old_string not found in file (no line matches the first line of "
            "old_string). Re-Read the file and copy the text exactly."
        )
    target = _body(old_lines[first_idx])
    hit = next((i for i, line in enumerate(file_lines) if _body(line) == target), None)
    if hit is None:
        return (
            "old_string not found in file (no line matches the first line of "
            "old_string). Re-Read the file and copy the text exactly."
        )
    start = max(0, hit - first_idx)
    for i, old_line in enumerate(old_lines):
        pos = start + i
        file_line = file_lines[pos].rstrip("\r") if pos < len(file_lines) else None
        old_line = old_line.rstrip("\r")
        if file_line == old_line:
            continue
        if file_line is not None and _body(file_line) == _body(old_line):
            return (
                f"old_string not found in file. Closest match starts at line "
                f"{start + 1} but the leading whitespace differs: the file line "
                f"starts with {_describe_ws(_indent(file_line))}, your old_string "
                f"line starts with {_describe_ws(_indent(old_line))}. Read returns "
                '"lineno<TAB>content" — the tab after the line number is display '
                "formatting, not file content. Re-Read the file and copy the text "
                "exactly."
            )
        shown = "<end of file>" if file_line is None else _body(file_line)[:80]
        return (
            f"old_string not found in file. Closest match starts at line "
            f"{start + 1}; line {pos + 1} differs: file has \"{shown}\", "
            f"old_string has \"{_body(old_line)[:80]}\""
        )
    return (
        "old_string not found in file. Re-Read the file and copy the text exactly."
    )


def _result(path: Path, n: int, note: str) -> ToolOutcome:
    return ToolOutcome(
        text=f"edited {path} ({n} replacement{'s' if n != 1 else ''}){note}"
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if str(path) not in ctx.read_files:
        return ToolOutcome(
            text=f"must Read {path} before editing it", is_error=True
        )
    if not path.exists():
        return ToolOutcome(text=f"file not found: {path}", is_error=True)

    old = input["old_string"]
    new = input["new_string"]
    if old == new:
        return ToolOutcome(
            text="old_string and new_string are identical", is_error=True
        )
    replace_all = bool(input.get("replace_all", False))
    # Match on LF-normalized text so a model quoting CRLF or LF both work,
    # then write back with the file's own line ending and no translation.
    content, newline = read_text_preserving(path)
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")

    # 1. Exact match.
    count = content.count(old)
    if count:
        if count > 1 and not replace_all:
            return ToolOutcome(
                text=f"old_string is not unique ({count} matches); pass replace_all or add context",
                is_error=True,
            )
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        write_text_with_newline(path, updated, newline)
        return _result(path, count if replace_all else 1, "")

    # 2. The model copied Read's ``lineno<TAB>`` display prefix.
    stripped_old = _strip_line_prefixes(old)
    if stripped_old is not None and stripped_old != new:
        count = content.count(stripped_old)
        if count:
            if count > 1 and not replace_all:
                return ToolOutcome(
                    text=f"old_string is not unique ({count} matches); pass replace_all or add context",
                    is_error=True,
                )
            stripped_new = _strip_line_prefixes(new)
            repl = stripped_new if stripped_new is not None else new
            updated = (
                content.replace(stripped_old, repl)
                if replace_all
                else content.replace(stripped_old, repl, 1)
            )
            write_text_with_newline(path, updated, newline)
            return _result(path, count if replace_all else 1, _FALLBACK_NOTE)

    # 3. Uniform leading-whitespace drift.
    file_lines = content.split("\n")
    old_lines = old.split("\n")
    runs = _find_runs(file_lines, old_lines)
    if runs:
        if len(runs) > 1 and not replace_all:
            return ToolOutcome(
                text=f"old_string is not unique ({len(runs)} matches); pass replace_all or add context",
                is_error=True,
            )
        chosen = runs if replace_all else runs[:1]
        new_lines = new.split("\n")
        span = len(old_lines)
        for start, prefix, old_has_extra in reversed(chosen):
            file_lines[start:start + span] = _apply_prefix(new_lines, prefix, old_has_extra)
        write_text_with_newline(path, "\n".join(file_lines), newline)
        return _result(path, len(chosen), _FALLBACK_NOTE)

    # 4. No match — say why.
    diag_lines = (stripped_old if stripped_old is not None else old).split("\n")
    return ToolOutcome(text=_diagnose(file_lines, diag_lines), is_error=True)
