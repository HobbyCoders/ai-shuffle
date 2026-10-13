"""A failed tool call should hand back the thing that fixes it.

Three recoveries used to cost an extra round-trip each: a failed Edit sent the
model back to re-Read the file, a mistyped path sent it to Glob, and a Grep at
a directory that does not exist surfaced ripgrep's error under a "searched
via rg under ..." header that reads like a result. Each one now answers itself.

The same seam carries the repeat-work notes: a Read of a file nothing has
touched since the last one, and a search whose inputs were already searched,
still return in full but say so.
"""

from __future__ import annotations

import shutil

import pytest

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


# ── Read: the unchanged-file note ────────────────────────────────────────

_UNCHANGED = "unchanged since your earlier read this session"


async def test_second_read_of_an_untouched_file_says_so(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("a = 1\nb = 2\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    first = await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    assert _UNCHANGED not in first.text

    second = await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    assert not second.is_error
    assert _UNCHANGED in second.text
    # The note is additive: the content is still all there.
    assert "1\ta = 1" in second.text and "2\tb = 2" in second.text


async def test_a_changed_file_reads_clean(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    f.write_text("a = 1\nb = 2\nc = 3\n")  # an edit anywhere moves mtime/size
    out = await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    assert _UNCHANGED not in out.text


async def test_a_different_window_is_not_a_repeat(tmp_path) -> None:
    f = tmp_path / "app.py"
    f.write_text("".join(f"line {i}\n" for i in range(1, 21)))
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    await registry.dispatch("Read", {"file_path": str(f), "offset": 1, "limit": 5}, ctx)
    shifted = await registry.dispatch(
        "Read", {"file_path": str(f), "offset": 6, "limit": 5}, ctx
    )
    assert _UNCHANGED not in shifted.text
    widened = await registry.dispatch(
        "Read", {"file_path": str(f), "offset": 1, "limit": 10}, ctx
    )
    assert _UNCHANGED not in widened.text
    # The original window is still remembered as its own entry.
    again = await registry.dispatch(
        "Read", {"file_path": str(f), "offset": 1, "limit": 5}, ctx
    )
    assert _UNCHANGED in again.text


async def test_a_failed_read_records_nothing(tmp_path) -> None:
    f = tmp_path / "app.py"
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))

    missing = await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    assert missing.is_error
    assert _UNCHANGED not in missing.text
    assert not ctx.read_seen

    f.write_text("a = 1\n")
    first = await registry.dispatch("Read", {"file_path": str(f)}, ctx)
    assert not first.is_error
    assert _UNCHANGED not in first.text


# ── Grep: the repeat-search note ─────────────────────────────────────────

_REPEAT_SEARCH = "identical search already run this session"


async def test_the_same_search_twice_says_so(tmp_path) -> None:
    (tmp_path / "a.py").write_text("needle\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "needle", "path": str(tmp_path)}

    first = await registry.dispatch("Grep", dict(call), ctx)
    assert not first.is_error
    assert _REPEAT_SEARCH not in first.text

    second = await registry.dispatch("Grep", dict(call), ctx)
    assert not second.is_error
    assert _REPEAT_SEARCH in second.text
    # The rows are byte-identical to a copy already in the conversation, so
    # the pointer REPLACES them rather than riding underneath a second copy.
    assert "a.py" not in second.text
    assert "searched via" in second.text  # the header still locates the first
    assert len(second.text) < len(first.text) + 400


async def test_a_changed_search_input_is_not_a_repeat(tmp_path) -> None:
    (tmp_path / "a.py").write_text("needle\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "needle", "path": str(tmp_path)}

    await registry.dispatch("Grep", dict(call), ctx)
    for changed in (
        {**call, "pattern": "haystack"},
        {**call, "glob": "*.py"},
        {**call, "no_ignore": True},
        {**call, "limit": 10},
    ):
        out = await registry.dispatch("Grep", changed, ctx)
        assert _REPEAT_SEARCH not in out.text, changed


@pytest.fixture(params=["rg", "python"])
def grep_engine(request, monkeypatch) -> str:
    """Run a Grep test once per engine; the repeat check sits after both."""
    if request.param == "rg":
        if shutil.which("rg") is None:
            pytest.skip("ripgrep is not installed")
    else:
        from fluid.tools.builtins import grep as grep_mod

        real_which = grep_mod.shutil.which
        monkeypatch.setattr(
            grep_mod.shutil, "which",
            lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k),
        )
    return request.param


async def test_a_repeat_search_after_an_edit_returns_the_new_result(
    tmp_path, grep_engine
) -> None:
    # The repeat pointer used to be keyed by the search input alone, so a
    # verification Grep after an Edit got "the same N lines are above" and
    # was sent back to the pre-edit listing.
    f = tmp_path / "a.py"
    f.write_text("foo = 1\nfoo = 2\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "foo", "path": str(tmp_path)}

    first = await registry.dispatch("Grep", dict(call), ctx)
    assert not first.is_error and "a.py" in first.text
    assert not (await registry.dispatch("Read", {"file_path": str(f)}, ctx)).is_error
    edited = await registry.dispatch(
        "Edit",
        {"file_path": str(f), "old_string": "foo = 1\nfoo = 2\n", "new_string": "bar = 1\n"},
        ctx,
    )
    assert not edited.is_error, edited.text

    second = await registry.dispatch("Grep", dict(call), ctx)
    assert _REPEAT_SEARCH not in second.text
    assert "(no matches)" in second.text


async def test_a_repeat_search_sees_a_new_file(tmp_path, grep_engine) -> None:
    (tmp_path / "a.py").write_text("foo = 1\n")
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "foo", "path": str(tmp_path)}

    await registry.dispatch("Grep", dict(call), ctx)
    (tmp_path / "b.py").write_text("foo = 2\nfoo = 3\n")
    second = await registry.dispatch("Grep", dict(call), ctx)
    assert _REPEAT_SEARCH not in second.text
    assert "b.py" in second.text

    # The changed result is now the one a true repeat is compared with.
    third = await registry.dispatch("Grep", dict(call), ctx)
    assert _REPEAT_SEARCH in third.text
    assert "b.py" not in third.text


async def test_a_failed_search_records_nothing(tmp_path) -> None:
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    call = {"pattern": "x", "path": str(tmp_path / "nope")}

    first = await registry.dispatch("Grep", dict(call), ctx)
    assert first.is_error
    assert not ctx.grep_seen
    second = await registry.dispatch("Grep", dict(call), ctx)
    assert _REPEAT_SEARCH not in second.text


# ── Grep ─────────────────────────────────────────────────────────────────


async def test_grep_rejects_a_missing_root_cleanly(tmp_path) -> None:
    registry, ctx = _registry(), ToolCtx(cwd=str(tmp_path))
    out = await registry.dispatch(
        "Grep", {"pattern": "x", "path": str(tmp_path / "nope" / "src")}, ctx
    )
    assert out.is_error and out.soft_error and out.error_code == "missing_path"
    assert out.text.startswith("(path missing:")
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
    # A brace spanning a separator is alternation too, as it is under ripgrep.
    spanning = glob_to_regex("{src/a,b}.py")
    assert spanning.match("src/a.py") and spanning.match("b.py")
    assert not spanning.match("{src/a,b}.py")


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
