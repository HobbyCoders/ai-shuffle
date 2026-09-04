"""Round-6 audit: Edit and the triple-quoted old_string.

A model writing an Edit inside a code-mode cell writes ``old_string`` as a
triple-quoted block: indented to the cell's nesting, and ending at a newline.
Both were friction. The trailing newline left a phantom "" element that failed
the drift recovery's body check and then got reported as a content mismatch
against ``""``; the indentation needed the repair path twelve times in one
session, with three outright failures, while the repair note sat at the end of
the result as an aside. The read gate, meanwhile, asked for a *call* — and a
session satisfied it with ``_ = Read(..., limit=5)`` before failing four Edits.
"""

from fluid.tools.builtins import edit as edit_mod
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry


async def _read_then_edit(tmp_path, body: str, old: str, new: str):
    target = tmp_path / "mod.py"
    target.write_text(body, encoding="utf-8")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(target)}, ctx)
    out = await registry.dispatch(
        "Edit",
        {"file_path": str(target), "old_string": old, "new_string": new},
        ctx,
    )
    return target, out


_BODY = "class C:\n    def f(self):\n        a = 1\n        return a\n"


async def test_a_triple_quoted_old_string_with_drift_still_matches(tmp_path):
    # What a model writes inside a cell: the block is indented to the cell's
    # nesting AND ends at a newline, so ``split`` leaves a phantom "" element.
    old = "        def f(self):\n            a = 1\n"
    new = "        def f(self):\n            a = 2\n"
    target, out = await _read_then_edit(tmp_path, _BODY, old, new)

    assert not out.is_error, out.text
    assert target.read_text(encoding="utf-8") == (
        "class C:\n    def f(self):\n        a = 2\n        return a\n"
    )
    # And the repair note is the first thing the model reads.
    assert out.text.startswith("NOTE: old_string did not match")
    assert "column 0" in out.text


async def test_the_trailing_newline_survives_the_replacement(tmp_path):
    old = "    def f(self):\n        a = 1\n"
    new = "    def f(self):\n        a = 3\n"
    target, out = await _read_then_edit(tmp_path, _BODY, old, new)

    assert not out.is_error, out.text
    # An exact match: no line is merged and no blank line is introduced.
    assert target.read_text(encoding="utf-8") == (
        "class C:\n    def f(self):\n        a = 3\n        return a\n"
    )


def test_the_diagnostic_blames_indentation_not_a_phantom_empty_line() -> None:
    file_lines = _BODY.split("\n")
    old_lines = edit_mod._strip_trailing_blank(
        "            a = 1\n            return a\n".split("\n")
    )
    message = edit_mod._diagnose(file_lines, old_lines)

    assert 'your old_string has ""' not in message
    assert "indentation" in message


def test_strip_trailing_blank_leaves_real_blank_lines_alone() -> None:
    # "a\n\n" is a line, then a blank line: one element is phantom, one is not.
    assert edit_mod._strip_trailing_blank(["a", "", ""]) == ["a", ""]
    assert edit_mod._strip_trailing_blank(["a", "b"]) == ["a", "b"]
    assert edit_mod._strip_trailing_blank([""]) == [""]


async def test_the_read_gate_asks_for_the_content_not_the_call(tmp_path):
    target = tmp_path / "unread.py"
    target.write_text("x = 1\n", encoding="utf-8")
    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Edit",
        {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
        ToolCtx(cwd=str(tmp_path)),
    )

    assert out.is_error
    # The gate used to read as "make a Read call", which a session satisfied
    # with `_ = Read(..., limit=5)` and then failed four Edits.
    assert "copy old_string out of what it returns" in out.text
    assert "discard" in out.text


def test_edit_description_warns_about_the_indented_block() -> None:
    text = edit_mod.describe()
    assert "must start at column 0" in text
    from fluid.tools.fragments import READ_BEFORE_EDIT

    assert READ_BEFORE_EDIT in text
    assert "copied verbatim" in READ_BEFORE_EDIT
