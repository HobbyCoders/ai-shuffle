"""A failed tool call should hand back the thing that fixes it.

Three recoveries used to cost an extra round-trip each: a failed Edit sent the
model back to re-Read the file, a mistyped path sent it to Glob, and a Grep at
a directory that does not exist surfaced ripgrep's error under a "searched
via rg under ..." header that reads like a result. Each one now answers itself.
"""

from __future__ import annotations

from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtins(registry)
    return registry


# ── Edit ─────────────────────────────────────────────────────────────────


async def test_failed_edit_quotes_the_file_back(tmp_path) -> None:
    f = tmp_path / "app.svelte"
    f.write_text("\tconst a = 1;\n\tconst b = 2;\n\tconst c = 3;\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit",
        {
            "file_path": str(f),
            "old_string": "\tconst a = 1;\n\tconst b = 9;",
            "new_string": "\tconst a = 0;\n\tconst b = 9;",
        },
        ctx,
    )
    assert out.is_error
    assert "line 2 content" in out.text
    assert "<<<FILE\n\tconst a = 1;\n\tconst b = 2;\n>>>" in out.text
    assert "no re-Read" in out.text


async def test_content_mismatch_wins_over_an_earlier_indent_difference(tmp_path) -> None:
    """The old diagnostic reported only the first difference.

    When that first difference was whitespace, the model fixed the indentation,
    retried, and failed again on the content line it was never told about.
    """
    f = tmp_path / "app.svelte"
    f.write_text("\tif (open) return 0;\n\tconst b = 2;\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit",
        {
            "file_path": str(f),
            "old_string": "\t\tif (open) return 0;\n\tconst b = 99;",
            "new_string": "x",
        },
        ctx,
    )
    assert out.is_error
    assert "line 2 content" in out.text  # the real blocker, not the indent
    assert "indentation also differs" in out.text


async def test_read_gate_names_the_call_to_make(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch(
        "Edit", {"file_path": str(f), "old_string": "a = 1", "new_string": "a = 2"}, ctx
    )
    assert out.is_error
    assert f'Read(file_path="{f}")' in out.text
    assert "one cell" in out.text


async def test_identical_strings_explain_the_likely_cause(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit", {"file_path": str(f), "old_string": "a = 1", "new_string": "a = 1"}, ctx
    )
    assert out.is_error
    assert "already landed" in out.text


# ── Read ─────────────────────────────────────────────────────────────────


async def test_read_recognises_a_grep_result_line(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch("Read", {"file_path": f"{f}:1:a = 1"}, ctx)
    assert out.is_error
    assert "Grep result line" in out.text
    assert f'the path part is "{f}"' in out.text


async def test_read_suggests_the_near_miss(tmp_path) -> None:
    (tmp_path / "WorkLogGroup.svelte").write_text("x\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch(
        "Read", {"file_path": str(tmp_path / "WorkLogGrup.svelte")}, ctx
    )
    assert out.is_error
    assert "did you mean" in out.text
    assert "WorkLogGroup.svelte" in out.text


async def test_read_says_when_the_parent_is_missing_too(tmp_path) -> None:
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    out = await registry.dispatch(
        "Read", {"file_path": str(tmp_path / "nope" / "app.py")}, ctx
    )
    assert out.is_error
    assert "parent directory" in out.text


# ── Grep ─────────────────────────────────────────────────────────────────


async def test_grep_rejects_a_missing_root_cleanly(tmp_path) -> None:
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    out = await registry.dispatch(
        "Grep", {"pattern": "x", "path": str(tmp_path / "nope" / "src")}, ctx
    )
    assert out.is_error
    assert out.text.startswith("path does not exist:")
    # No "searched via rg under ..." header to mistake for a result.
    assert "searched via" not in out.text


# ── glob semantics agree across engines ──────────────────────────────────


def test_glob_compiler_accepts_brace_alternation() -> None:
    """ripgrep honours `*.{py,ts}`; the Python paths have to as well.

    While they did not, the same Grep call filtered correctly on a machine with
    ripgrep and matched nothing on one without — and "no matches" reads as a
    fact about the tree, so the model narrows and retries instead of suspecting
    the tool.
    """
    from fluid.tools.builtins._util import glob_to_regex

    rx = glob_to_regex("*.{py,md}")
    assert rx.match("a.py") and rx.match("nested/a.md")
    assert not rx.match("a.ts")
    # A brace spanning a separator is not alternation — left as literal text.
    assert glob_to_regex("{src/a,b}.py").match("{src/a,b}.py")


async def test_grep_fallback_honours_brace_globs(tmp_path, monkeypatch) -> None:
    import shutil

    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.md").write_text("needle\n")
    (tmp_path / "c.ts").write_text("needle\n")
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # force the fallback

    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    out = await registry.dispatch(
        "Grep", {"pattern": "needle", "path": str(tmp_path), "glob": "*.{py,md}"}, ctx
    )
    assert not out.is_error
    assert "python-fallback" in out.text
    assert "a.py" in out.text and "b.md" in out.text
    assert "c.ts" not in out.text


async def test_grep_fallback_honours_a_pathful_glob(tmp_path, monkeypatch) -> None:
    import shutil

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("needle\n")
    (tmp_path / "a.ts").write_text("needle\n")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    out = await registry.dispatch(
        "Grep", {"pattern": "needle", "path": str(tmp_path), "glob": "src/**/*.ts"}, ctx
    )
    assert not out.is_error
    assert "src" in out.text
    assert out.text.count("needle") == 1
