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

A Python target gets one more gate before the bytes land: the result must
still parse. Edit tells the model not to Read the file back, so a botched
replacement (a truncated condition, a stray ``:w``, a dangling block) is
otherwise discovered at the next import — often many turns later, with the
cause long out of view. See :func:`_syntax_rejection`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from fluid.tools import read_window
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
        "- In code mode, a triple-quoted old_string must start at column 0: "
        "the cell's own indentation becomes part of the string, and so does "
        "the newline before the closing quotes.\n"
        "- Edits to a .py file that currently parses are syntax-checked "
        "before anything is written: a result that would not parse is "
        "rejected, the file is left untouched, and the error names the "
        "line.\n"
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

# Lands as the result's FIRST line, not a trailing aside. A twelve-repair
# session shows the aside being skimmed past: the same indented triple-quoted
# old_string kept coming back, and three of those attempts failed outright.
_FALLBACK_NOTE = (
    "NOTE: old_string did not match the file as written — it matched only "
    "after correcting a uniform indentation offset / a copied line-number "
    "prefix. Copy old_string verbatim from the Read output next time; a "
    "triple-quoted old_string must start at column 0, because the cell's own "
    "indentation becomes part of the string."
)


def _strip_line_prefixes(text: str) -> str | None:
    """Drop Read's ``lineno<TAB>`` prefix from every line that carries one.

    Returns ``None`` when no line carries one (nothing to correct).
    """
    lines = text.split("\n")
    if not any(_LINE_PREFIX.match(line) for line in lines):
        return None
    return "\n".join(_LINE_PREFIX.sub("", line) for line in lines)


def _ends_blank(lines: list[str]) -> bool:
    """Whether ``lines`` carries the empty element a trailing newline leaves."""
    return len(lines) > 1 and lines[-1] == ""


def _strip_trailing_blank(lines: list[str]) -> list[str]:
    """Drop that phantom element — it corresponds to no line in the file."""
    return lines[:-1] if _ends_blank(lines) else lines


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


# The failed-match diagnostic quotes the file back verbatim so the retry needs
# no second Read. Bounded so a huge old_string cannot drag the whole file along.
_QUOTE_MAX_LINES = 40
_QUOTE_MAX_CHARS = 4_000


def _quote_span(file_lines: list[str], start: int, span: int) -> str:
    """The file's own text for the closest-match window, ready to copy.

    Rendered raw — no ``lineno<TAB>`` prefixes — precisely because that prefix
    is what models keep copying into ``old_string`` by mistake. Line numbers
    ride in the surrounding prose instead.
    """
    shown = min(span, _QUOTE_MAX_LINES)
    end = min(len(file_lines), start + shown)
    body = "\n".join(line.rstrip("\r") for line in file_lines[start:end])
    if len(body) > _QUOTE_MAX_CHARS:
        body = body[:_QUOTE_MAX_CHARS] + "\n… [truncated]"
    if span > _QUOTE_MAX_LINES:
        body += f"\n… [{span - _QUOTE_MAX_LINES} more lines]"
    return (
        f"The file's own text at lines {start + 1}-{end} is below — copy it "
        "verbatim into old_string (no line-number prefixes; no re-Read "
        f"needed):\n<<<FILE\n{body}\n>>>"
    )


def _diagnose(file_lines: list[str], old_lines: list[str]) -> str:
    """Explain, in the model's terms, why ``old_string`` did not match.

    Two things matter more than naming the first differing character. One: the
    *first* difference is often whitespace while the real blocker is a content
    mismatch further down — reporting only the whitespace sends the model back
    for a re-Read that then fails the same way. So the whole window is scanned
    and a content difference wins the report. Two: whatever the cause, the
    correction is the file's actual text, so it is quoted inline and the retry
    costs no extra call.
    """
    first_idx = next((i for i, line in enumerate(old_lines) if _body(line)), None)
    if first_idx is None:
        return (
            "old_string not found in file (no line matches the first line of "
            "old_string). Re-Read the file and copy the text exactly, "
            'stripping Read\'s "lineno<TAB>" prefix — that tab is display '
            "formatting, not file content."
        )
    target = _body(old_lines[first_idx])
    hit = next((i for i, line in enumerate(file_lines) if _body(line) == target), None)
    if hit is None:
        return (
            "old_string not found in file (no line matches the first line of "
            "old_string). Re-Read the file and copy the text exactly — "
            'without Read\'s "lineno<TAB>" prefix, which is display '
            "formatting, not file content — or Grep for the text first to "
            "confirm it is in this file at all."
        )
    start = max(0, hit - first_idx)
    quote = _quote_span(file_lines, start, len(old_lines))

    ws_only: str | None = None
    for i, old_line in enumerate(old_lines):
        pos = start + i
        file_line = file_lines[pos].rstrip("\r") if pos < len(file_lines) else None
        old_line = old_line.rstrip("\r")
        if file_line == old_line:
            continue
        if file_line is not None and _body(file_line) == _body(old_line):
            if ws_only is None:
                ws_only = (
                    f"line {pos + 1} indentation: the file has "
                    f"{_describe_ws(_indent(file_line))}, your old_string has "
                    f"{_describe_ws(_indent(old_line))}"
                )
            continue
        shown = "<end of file>" if file_line is None else _body(file_line)[:80]
        detail = (
            f'line {pos + 1} content: the file has "{shown}", your old_string '
            f'has "{_body(old_line)[:80]}"'
        )
        if ws_only is not None:
            detail += f" (indentation also differs — {ws_only})"
        return (
            "old_string not found in file. Closest match starts at line "
            f"{start + 1}; {detail}.\n{quote}"
        )
    if ws_only is not None:
        return (
            "old_string not found in file. Closest match starts at line "
            f"{start + 1}; every line matches except indentation, and the drift "
            "is not uniform so it could not be corrected automatically "
            f"({ws_only}). Read renders text as \"lineno<TAB>content\" — that "
            f"tab is display formatting, not file content.\n{quote}"
        )
    return (
        "old_string not found in file — line endings or trailing whitespace "
        f"differ. {quote}"
    )


# ── post-edit syntax gate (Python targets) ───────────────────────────────────

_PY_SUFFIXES = frozenset({".py", ".pyi"})


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: NUL bytes in source
        return False
    return True


def _syntax_rejection(path: Path, original: str, candidate: str) -> ToolOutcome | None:
    """Refuse an edit that would leave a parseable Python file unparseable.

    Asymmetric on purpose: a file that does not parse *before* the edit is a
    file being repaired, and the repair may well take more than one Edit, so
    only the parses-then-breaks transition is refused. Nothing is written when
    it is — the model gets the SyntaxError, its line and that line's text, and
    can fix ``new_string`` without a Read.
    """
    if path.suffix.lower() not in _PY_SUFFIXES or not _parses(original):
        return None
    try:
        ast.parse(candidate)
    except ValueError as exc:  # NUL bytes and friends — no line to name
        return ToolOutcome(text=_syntax_message(path, None, str(exc), None), is_error=True)
    except SyntaxError as exc:
        return ToolOutcome(
            text=_syntax_message(path, exc.lineno, exc.msg, candidate), is_error=True
        )
    return None


def _syntax_message(
    path: Path, lineno: int | None, detail: str, candidate: str | None
) -> str:
    where = f" at line {lineno}" if lineno else ""
    quoted = ""
    if candidate is not None and lineno:
        lines = candidate.split("\n")
        if 1 <= lineno <= len(lines):
            quoted = f"\nThe offending line reads:\n{lines[lineno - 1].rstrip()}"
    return (
        f"edit REJECTED and nothing was written: the result would not parse as "
        f"Python — SyntaxError{where}: {detail}.{quoted}\n"
        f"{path} is unchanged. Fix new_string (a truncated condition, a stray "
        "editor keystroke and an unclosed block are the usual causes) and "
        "reissue the Edit."
    )


def _result(path: Path, n: int, note: str) -> ToolOutcome:
    line = f"edited {path} ({n} replacement{'s' if n != 1 else ''})"
    return ToolOutcome(text=f"{note}\n{line}" if note else line)


# ─── read-window gate ────────────────────────────────────────────────────────
# ``ctx.read_files`` proves the file was Read; the windows prove WHICH LINES
# were. An audited code-mode cell ran ``_ = Read(path, limit=5)`` to unlock the
# gate and then edited from memory; the edit missed, and the turn went on a
# diagnosis a real Read would have made unnecessary. So the lines an edit
# replaces must fall inside a delivered window — where windows are on record
# at all (a resumed history from before they existed still passes on the
# file-level ledger alone).


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _span_at(content: str, idx: int, old: str) -> tuple[int, int]:
    """Inclusive 1-based line span of ``old`` found at ``idx``."""
    start = content.count("\n", 0, idx) + 1
    return start, start + max(_line_count(old) - 1, 0)


def _spans_of(content: str, old: str, replace_all: bool) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    idx = content.find(old)
    while idx != -1:
        spans.append(_span_at(content, idx, old))
        if not replace_all:
            break
        idx = content.find(old, idx + max(len(old), 1))
    return spans


def _window_rejection(
    ctx: ToolCtx, path: Path, spans: list[tuple[int, int]]
) -> ToolOutcome | None:
    for start, end in spans:
        if read_window.covers(ctx, str(path), start, end):
            continue
        seen = read_window.describe(ctx, str(path))
        where = f"line {start}" if start == end else f"lines {start}-{end}"
        offset = max(1, start - 5)
        limit = end - offset + 6
        return ToolOutcome(
            text=(
                f"edit REJECTED and nothing was written: old_string sits at "
                f"{where} of {path}, outside the part of the file you have Read "
                f"this session (lines {seen}). Call "
                f'Read(file_path="{path}", offset={offset}, limit={limit}) and '
                "copy old_string out of what it returns, byte-for-byte — an "
                "old_string recalled from memory or assembled from Grep output "
                "is how an edit lands on the wrong text. A Read whose result "
                "you discard does not count as having seen those lines."
            ),
            is_error=True,
        )
    return None


def _shift_windows(
    ctx: ToolCtx, path: Path, spans: list[tuple[int, int]], new_lines: int
) -> None:
    """Keep the windows honest after a write: bottom-most replacement first."""
    for start, end in sorted(spans, reverse=True):
        read_window.shift(ctx, str(path), start, end - start + 1, new_lines)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if str(path) not in ctx.read_files:
        return ToolOutcome(
            text=(
                f"must Read {path} before editing it — call "
                f'Read(file_path="{path}") first and copy old_string out of '
                "what it returns, byte-for-byte and without the line-number "
                "prefix. In code mode the Read and the Edit fit in one cell, "
                "so this costs no extra turn. A Read whose text you discard "
                "(`_ = Read(..., limit=5)`) unlocks the gate and nothing else "
                "— the match is still byte-for-byte against the file."
            ),
            is_error=True,
        )
    if not path.exists():
        return ToolOutcome(text=f"file not found: {path}", is_error=True)

    old = input["old_string"]
    new = input["new_string"]
    if old == new:
        return ToolOutcome(
            text=(
                "old_string and new_string are identical — this edit would "
                "change nothing. If you meant to re-apply an earlier edit it "
                "has almost certainly already landed; Read the file to confirm "
                "before retrying."
            ),
            is_error=True,
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
                is_error=True, soft_error=True,
            )
        spans = _spans_of(content, old, replace_all)
        rejected = _window_rejection(ctx, path, spans)
        if rejected is not None:
            return rejected
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        rejected = _syntax_rejection(path, content, updated)
        if rejected is not None:
            return rejected
        write_text_with_newline(path, updated, newline)
        _shift_windows(ctx, path, spans, _line_count(new))
        return _result(path, count if replace_all else 1, "")

    # 2. The model copied Read's ``lineno<TAB>`` display prefix.
    stripped_old = _strip_line_prefixes(old)
    if stripped_old is not None and stripped_old != new:
        count = content.count(stripped_old)
        if count:
            if count > 1 and not replace_all:
                return ToolOutcome(
                    text=f"old_string is not unique ({count} matches); pass replace_all or add context",
                    is_error=True, soft_error=True,
                )
            stripped_new = _strip_line_prefixes(new)
            repl = stripped_new if stripped_new is not None else new
            spans = _spans_of(content, stripped_old, replace_all)
            rejected = _window_rejection(ctx, path, spans)
            if rejected is not None:
                return rejected
            updated = (
                content.replace(stripped_old, repl)
                if replace_all
                else content.replace(stripped_old, repl, 1)
            )
            rejected = _syntax_rejection(path, content, updated)
            if rejected is not None:
                return rejected
            write_text_with_newline(path, updated, newline)
            _shift_windows(ctx, path, spans, _line_count(repl))
            return _result(path, count if replace_all else 1, _FALLBACK_NOTE)

    # 3. Uniform leading-whitespace drift.
    file_lines = content.split("\n")
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    # A triple-quoted old_string ends at a newline, so ``split`` leaves a
    # phantom "" element that matches no real file line — which used to fail
    # _find_runs' body check and kill this recovery even when a uniform
    # indentation drift was the only thing wrong. Dropped from both sides
    # together, so the replacement keeps the same trailing newline it had.
    if _ends_blank(old_lines) and _ends_blank(new_lines):
        old_lines, new_lines = old_lines[:-1], new_lines[:-1]
    runs = _find_runs(file_lines, old_lines)
    if runs:
        if len(runs) > 1 and not replace_all:
            return ToolOutcome(
                text=f"old_string is not unique ({len(runs)} matches); pass replace_all or add context",
                is_error=True, soft_error=True,
            )
        chosen = runs if replace_all else runs[:1]
        span = len(old_lines)
        spans = [(start + 1, start + span) for start, _, _ in chosen]
        rejected = _window_rejection(ctx, path, spans)
        if rejected is not None:
            return rejected
        for start, prefix, old_has_extra in reversed(chosen):
            file_lines[start:start + span] = _apply_prefix(new_lines, prefix, old_has_extra)
        updated = "\n".join(file_lines)
        rejected = _syntax_rejection(path, content, updated)
        if rejected is not None:
            return rejected
        write_text_with_newline(path, updated, newline)
        _shift_windows(ctx, path, spans, len(new_lines))
        return _result(path, len(chosen), _FALLBACK_NOTE)

    # 4. No match — say why.
    # The phantom trailing element goes here too: reported as content it read
    # as `your old_string has ""` and sent the model hunting for a difference
    # that was really just indentation.
    diag_lines = _strip_trailing_blank(
        (stripped_old if stripped_old is not None else old).split("\n")
    )
    # Soft in code mode (round 9): "old_string not found" is a result the
    # cell can read and react to; aborting the cell lost every later call in
    # it. The text says the edit did NOT land; nothing on disk changed.
    return ToolOutcome(
        text="Edit NOT applied — " + _diagnose(file_lines, diag_lines),
        is_error=True, soft_error=True,
    )
