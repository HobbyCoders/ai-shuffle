"""Edit edges: empty old_string, uniqueness and read-window gates on the
fallback paths, the NUL-byte syntax gate and the failed-match diagnostics."""

from __future__ import annotations

from pathlib import Path

from fluid.tools import read_window
from fluid.tools.builtins import edit as edit_mod
from fluid.tools.builtins.edit import _derive_prefix, _describe_ws, _diagnose, _quote_span
from fluid.tools.registry import ToolCtx


def _ctx(path: Path) -> ToolCtx:
    ctx = ToolCtx(cwd=str(path.parent))
    ctx.read_files.add(str(path))
    return ctx


async def _edit(path: Path, ctx: ToolCtx | None = None, **kw):
    return await edit_mod.handler({"file_path": str(path), **kw}, ctx or _ctx(path))


async def test_empty_old_string_never_splices_through_the_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("abc\n")
    for replace_all in (False, True):
        outcome = await _edit(target, old_string="", new_string="X", replace_all=replace_all)
        assert outcome.is_error and "old_string is empty" in outcome.text
    assert target.read_text() == "abc\n"


async def test_empty_old_string_fills_an_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    target.write_text("")
    outcome = await _edit(target, old_string="", new_string="hello\n")
    assert not outcome.is_error
    assert target.read_text() == "hello\n"


async def test_missing_file_after_read(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    outcome = await _edit(target, old_string="a", new_string="b")
    assert outcome.is_error and outcome.text.startswith("file not found")


async def test_prefix_fallback_not_unique_and_outside_read_window(tmp_path: Path) -> None:
    target = tmp_path / "p.txt"
    target.write_text("alpha\nbeta\nalpha\nbeta\n" + "x\n" * 20 + "gamma\n")
    outcome = await _edit(target, old_string="1\talpha\n2\tbeta", new_string="z")
    assert outcome.is_error and "not unique (2 matches)" in outcome.text

    ctx = _ctx(target)
    read_window.record(ctx, str(target), 1, 5)
    outcome = await _edit(target, ctx, old_string="25\tgamma", new_string="delta")
    assert outcome.is_error and "outside the part of the file you have Read" in outcome.text
    assert "gamma" in target.read_text()


async def test_indent_fallback_not_unique_and_outside_read_window(tmp_path: Path) -> None:
    target = tmp_path / "i.py"
    target.write_text(
        "def f():\n    x = 1\n    y = 2\n\ndef g():\n    x = 1\n    y = 2\n"
        + "\n" * 20 + "def h():\n    z = 3\n    w = 4\n"
    )
    outcome = await _edit(target, old_string="x = 1\ny = 2", new_string="x = 9\ny = 9")
    assert outcome.is_error and "not unique (2 matches)" in outcome.text

    ctx = _ctx(target)
    read_window.record(ctx, str(target), 1, 5)
    outcome = await _edit(target, ctx, old_string="z = 3\nw = 4", new_string="z = 0\nw = 0")
    assert outcome.is_error and "outside the part of the file you have Read" in outcome.text


async def test_nul_byte_edit_to_python_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("x = 1\n")
    outcome = await _edit(target, old_string="x = 1", new_string="x = '\0'")
    assert outcome.is_error and "edit REJECTED" in outcome.text
    assert target.read_text() == "x = 1\n"


def test_derive_prefix_handles_dropped_and_mixed_indentation() -> None:
    # The model dropped a uniform 4-space prefix.
    assert _derive_prefix(["a", "  b"], ["    a", "      b"]) == ("    ", False)
    # Neither direction explains both lines.
    assert _derive_prefix(["  a", "b"], ["a", "  b"]) is None
    # Blank-only runs carry no information.
    assert _derive_prefix(["", "  "], ["x", "y"]) is None
    # Two different "extra" prefixes, but a consistent "missing" one fails too.
    assert _derive_prefix(["\ta", "  b"], ["a", "b"]) is None


def test_describe_ws_and_quote_span_bounds() -> None:
    assert _describe_ws("") == "no indentation"
    assert _describe_ws("\t  ") == "1 tab + 2 spaces"
    lines = ["y" * 200 for _ in range(60)]
    quoted = _quote_span(lines, 0, 60)
    assert "… [truncated]" in quoted and "… [20 more lines]" in quoted


def test_diagnose_names_content_line_endings_and_blank_old() -> None:
    assert "no line matches" in _diagnose(["a"], ["", "  "])
    # Old text runs past the end of the file.
    assert "<end of file>" in _diagnose(["a", "b"], ["b", "c"])
    # Every line matches once the CR is dropped — only line endings differ.
    assert "line endings or trailing whitespace" in _diagnose(["a\r", "b"], ["a", "b"])
    # Indentation differs first, content differs later: content wins the report.
    report = _diagnose(["  a", "b"], ["a", "c"])
    assert 'content: the file has "b"' in report and "indentation also differs" in report
