"""Tool-truth regressions from the six-session production audit.

Each test here stands for a call whose *output was believable and wrong*: a
filter that matched nothing and said "(no matches)", a command that succeeded
and said nothing at all, a search that reported 25 matches while printing 5.
The shared failure is not the bug — it is that the result gave the model no
reason to doubt it.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from fluid.store.base import SessionSearchHit
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins._util import GlobSyntaxError, compile_glob, glob_to_regex
from fluid.tools.registry import ToolCtx, ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtins(registry)
    return registry


def _tree(root) -> None:
    (root / ".fluid").mkdir()
    (root / ".fluid" / "notes.md").write_text("needle in the config tree\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("needle in the docs\n")
    (root / "app.py").write_text("needle in the code\n")


# ── negated globs: exclude, never match-nothing ──────────────────────────────


def test_compile_glob_negation_excludes() -> None:
    """`!x` is ripgrep's exclusion marker; compiling it as an include filter
    silently drops every file, which is how a clean "(no matches)" gets made."""
    excluded = compile_glob("!.fluid/**")
    assert not excluded.matches(".fluid/notes.md")
    assert excluded.matches("app.py") and excluded.matches("docs/guide.md")

    plain = compile_glob("*.py")
    assert plain.matches("app.py") and not plain.matches("docs/guide.md")


def test_compile_glob_negation_handles_brace_groups_with_slashes() -> None:
    excluded = compile_glob("!{.fluid/**,docs/**}")
    assert not excluded.matches(".fluid/notes.md")
    assert not excluded.matches("docs/guide.md")
    assert excluded.matches("app.py")


def test_glob_to_regex_supports_nested_and_spanning_alternation() -> None:
    rx = glob_to_regex("src/{a,{b,c}/deep}/*.ts")
    assert rx.match("src/a/x.ts") and rx.match("src/c/deep/x.ts")
    assert not rx.match("src/d/x.ts")


@pytest.mark.parametrize(
    "pattern, construct",
    [("{a,b", "'{'"), ("[abc", "'['"), ("!", "nothing to exclude")],
)
def test_unsupported_glob_syntax_raises_rather_than_matching_nothing(
    pattern: str, construct: str
) -> None:
    with pytest.raises(GlobSyntaxError) as exc:
        compile_glob(pattern)
    assert construct in str(exc.value)


def test_glob_to_regex_refuses_a_bare_negation() -> None:
    # The raw compiler has no way to express "exclude", so it must not pretend:
    # escaping the '!' into the regex is exactly the audited silent-miss.
    with pytest.raises(GlobSyntaxError):
        glob_to_regex("!vendor/**")


async def test_grep_fallback_negated_glob_excludes(tmp_path, monkeypatch) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # force the fallback

    out = await _registry().dispatch(
        "Grep", {"pattern": "needle", "glob": "!{.fluid/**}"}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    assert "python-fallback" in out.text
    assert "app.py" in out.text and "guide.md" in out.text
    assert "notes.md" not in out.text
    assert "(no matches)" not in out.text


async def test_grep_fallback_unsupported_glob_is_a_hard_error(
    tmp_path, monkeypatch
) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    out = await _registry().dispatch(
        "Grep", {"pattern": "needle", "glob": "{a,b"}, ToolCtx(cwd=str(tmp_path))
    )
    assert out.is_error
    assert "unsupported glob" in out.text and "'{'" in out.text
    assert "(no matches)" not in out.text


async def test_grep_rg_branch_passes_negation_through(tmp_path, monkeypatch) -> None:
    """The rg branch already had this right — it must keep handing rg the raw
    glob rather than the fallback's compiled form."""
    import asyncio as _asyncio

    from fluid.tools.builtins import grep as grep_mod

    seen: list[list[str]] = []

    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        seen.append(list(args))
        return _Proc()

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    await _registry().dispatch(
        "Grep", {"pattern": "needle", "glob": "!vendor/**"}, ToolCtx(cwd=str(tmp_path))
    )
    assert "--glob" in seen[0] and "!vendor/**" in seen[0]


# ── directory-prefixed globs anchor at the searched root ─────────────────────


def _nested_tree(root):
    """The audited shape: a search root buried under a gitignored ancestor."""
    (root / ".gitignore").write_text("/.fluid\n")
    (root / "outer.ts").write_text("const needleToken = 0;\n")
    nested = root / ".fluid" / "worktrees" / "x"
    src = nested / "frontend" / "src"
    src.mkdir(parents=True)
    (src / "a.ts").write_text("export const needleToken = 1;\n")
    (nested / "elsewhere.ts").write_text("const needleToken = 2;\n")
    return nested


_HAS_RG = shutil.which("rg") is not None


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
async def test_grep_rg_prefixed_glob_anchors_at_the_searched_root(
    tmp_path, monkeypatch
) -> None:
    """rg matches --glob against its own cwd, not the search target.

    Handed an absolute root while the process sat in an ancestor directory,
    `frontend/src/**` anchored at that ancestor and matched nothing — 46 of 66
    searches in the audited session came back as a believable "(no matches)".
    """
    nested = _nested_tree(tmp_path)
    monkeypatch.chdir(tmp_path)  # the ancestor the glob used to anchor against

    out = await _registry().dispatch(
        "Grep",
        {"pattern": "needleToken", "glob": "frontend/src/**/*.ts", "path": str(nested)},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text
    assert "searched via rg" in out.text
    assert "(no matches)" not in out.text
    assert "a.ts" in out.text
    # The glob still filters: siblings outside frontend/src are not hits.
    assert "elsewhere.ts" not in out.text and "outer.ts" not in out.text


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
async def test_grep_rg_match_paths_stay_absolute(tmp_path, monkeypatch) -> None:
    """Running rg inside the root must not leak `./a.ts` into the output —
    models paste these lines straight back into Read."""
    nested = _nested_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    out = await _registry().dispatch(
        "Grep", {"pattern": "needleToken", "path": str(nested)}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    hits = [ln for ln in out.text.splitlines() if ":1:" in ln or ":2:" in ln]
    assert hits, out.text
    for line in hits:
        path = line.rsplit(":", 2)[0]
        assert Path(path).is_absolute(), line
        assert Path(path).exists(), line


async def test_grep_fallback_prefixed_glob_anchors_at_the_searched_root(
    tmp_path, monkeypatch
) -> None:
    """The Python fallback must mean the same thing by the same glob."""
    nested = _nested_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    out = await _registry().dispatch(
        "Grep",
        {"pattern": "needleToken", "glob": "frontend/src/**/*.ts", "path": str(nested)},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text
    assert "python-fallback" in out.text
    assert "(no matches)" not in out.text
    assert "a.ts" in out.text
    assert "elsewhere.ts" not in out.text and "outer.ts" not in out.text


# ── the no-result sentinels are strings, and the docs say so ─────────────────


async def test_no_result_sentinels_are_truthy_so_docs_teach_the_membership_test(
    tmp_path,
) -> None:
    """`if out:` cannot distinguish "no matches" from matches — the sentinel is
    a non-empty string. The tool descriptions promise the membership test
    instead, so the promised idiom has to work against real output."""
    ctx = ToolCtx(cwd=str(tmp_path))
    (tmp_path / "app.py").write_text("nothing interesting here\n")

    grep_out = (await _registry().dispatch("Grep", {"pattern": "zzz-absent"}, ctx)).text
    glob_out = (await _registry().dispatch("Glob", {"pattern": "*.nope"}, ctx)).text
    bash_out = (await _bash(tmp_path, "pass")).text

    # The trap the docs now warn about: every one of these is truthy.
    assert grep_out and glob_out and bash_out

    # The documented idioms, exactly as written in the descriptions.
    assert "(no matches)" in grep_out
    assert "(no matches)" in glob_out
    assert "[exit code" in bash_out and "(no output)" in bash_out

    from fluid.tools.builtins import bash as bash_mod
    from fluid.tools.builtins import glob as glob_mod
    from fluid.tools.builtins import grep as grep_mod

    for text in (grep_mod.describe(), bash_mod.describe()):
        assert "`if out:`" in text
    assert "`'(no matches)' in out`" in grep_mod.describe()
    assert "`'[exit code' in out`" in bash_mod.describe()
    assert "`(no matches)`" in glob_mod.describe()


async def test_glob_recursive_file_semantics_match_description(tmp_path) -> None:
    from fluid.tools.builtins import glob

    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "nested" / "b.py").write_text("b")
    expected = {
        "*.py": {"src/a.py", "src/nested/b.py"},
        "src/*.py": {"src/a.py"},
        "src/**/*.py": {"src/a.py", "src/nested/b.py"},
    }
    for pattern, paths in expected.items():
        result = await glob.handler({"pattern": pattern}, ToolCtx(cwd=str(tmp_path)))
        assert not result.is_error
        assert set(result.text.splitlines()[1:]) == paths
    assert "recursive file search, not a directory listing" in glob.describe()
    assert "Directories themselves are not returned" in glob.describe()


def test_ask_user_description_requires_a_blocking_decision_round_trip():
    from fluid.tools.builtins.ask_user import describe

    text = describe()
    assert "reversible default" in text
    assert "call AskUser and wait for the answer before dependent work" in text
    assert "does not grant permission" in text


async def test_glob_description_states_the_string_return_shape(tmp_path) -> None:
    """The old wording ("returns matching paths ... sorted") read as a list, so
    `len(paths)` counted characters and `paths[:300]` cut mid-path."""
    from fluid.tools.builtins import glob as glob_mod

    text = glob_mod.describe()
    assert "newline-joined string" in text and "not a list" in text
    assert "splitlines" in text

    _tree(tmp_path)
    out = await _registry().dispatch("Glob", {"pattern": "**/*.md"}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error, out.text
    lines = out.text.splitlines()
    assert lines[0].startswith("globbed under ")
    assert sorted(lines[1:]) == [".fluid/notes.md", "docs/guide.md"]


async def test_glob_tool_negated_pattern_excludes(tmp_path) -> None:
    _tree(tmp_path)

    out = await _registry().dispatch(
        "Glob", {"pattern": "!{.fluid/**,docs/**}"}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    assert "app.py" in out.text
    assert "notes.md" not in out.text and "guide.md" not in out.text


async def test_glob_tool_unsupported_pattern_is_a_hard_error(tmp_path) -> None:
    _tree(tmp_path)

    out = await _registry().dispatch(
        "Glob", {"pattern": "src/[unclosed"}, ToolCtx(cwd=str(tmp_path))
    )
    assert out.is_error
    assert "unsupported glob" in out.text and "'['" in out.text
    assert "(no matches)" not in out.text


# ── bash: an empty result still has to report its status ─────────────────────


async def _bash(tmp_path, code: str):
    return await _registry().dispatch(
        "Bash",
        {"command": f'"{sys.executable}" -c "{code}"'},
        ToolCtx(cwd=str(tmp_path)),
    )


async def test_bash_silent_success_states_exit_code(tmp_path) -> None:
    # The audited failure: `gh pr merge` returned a bare "(no output)", which
    # says nothing about whether the merge happened — so it got run again.
    out = await _bash(tmp_path, "pass")
    assert not out.is_error
    assert out.text == "(no output) [exit code 0]"


async def test_bash_silent_failure_still_states_exit_code(tmp_path) -> None:
    out = await _bash(tmp_path, "import sys; sys.exit(4)")
    assert out.is_error and out.soft_error
    assert out.text == "(no output) [exit code 4]"


async def test_bash_nonempty_success_separates_output_from_status(tmp_path) -> None:
    out = await _bash(tmp_path, "print('ok')")
    assert not out.is_error
    assert out.text.startswith('<untrusted-tool-result source="Bash stdout/stderr">')
    assert "ok" in out.text.split('</untrusted-tool-result>')[0].splitlines()
    assert out.text.endswith('</untrusted-tool-result>\n[exit code 0]')


# ── session search: say what is on screen ────────────────────────────────────


def _hit(session_id: str, score: int, texts: list[tuple[str, str]]) -> SessionSearchHit:
    return SessionSearchHit(
        session_id=session_id,
        score=score,
        last_ts=1_700_000_000.0,
        texts=texts,
        title=None,
    )


def _fake_search(hits: list[SessionSearchHit]):
    def search(terms, *, limit=5, exclude_session_id=None):
        return hits

    return search


async def test_session_search_header_reports_shown_of_total() -> None:
    hits = [_hit(f"ses_{i}", 25 - i, [("user", f"terraform run {i}")]) for i in range(25)]
    ctx = ToolCtx(session_id="cur", extra={"session_search": _fake_search(hits)})

    out = await _registry().dispatch("SessionSearch", {"query": "terraform"}, ctx)
    assert not out.is_error
    header = out.text.splitlines()[0]
    assert "25 session(s) matched" in header
    assert "showing the best 5" in header
    assert sum(1 for ln in out.text.splitlines() if ln.startswith("session ")) == 5


async def test_session_search_header_omits_the_split_when_all_are_shown() -> None:
    ctx = ToolCtx(
        session_id="cur",
        extra={"session_search": _fake_search([_hit("ses_a", 2, [("user", "terraform")])])},
    )
    out = await _registry().dispatch("SessionSearch", {"query": "terraform"}, ctx)
    assert out.text.splitlines()[0] == "1 session(s) matched; best first:"


async def test_session_search_dedupes_repeated_snippets() -> None:
    boilerplate = "terraform boilerplate header repeated verbatim"
    hits = [_hit("ses_a", 4, [
        ("user", boilerplate),
        ("assistant", boilerplate),
        ("user", boilerplate),
        ("assistant", "terraform plan is finally clean"),
    ])]
    ctx = ToolCtx(session_id="cur", extra={"session_search": _fake_search(hits)})

    out = await _registry().dispatch("SessionSearch", {"query": "terraform"}, ctx)
    snippets = [ln for ln in out.text.splitlines() if ln.startswith("  [")]
    assert len(snippets) == 2
    assert sum(1 for ln in snippets if boilerplate in ln) == 1
    # The freed budget reaches a hit the 3-snippet cap would have hidden.
    assert any("finally clean" in ln for ln in snippets)


async def test_session_search_flags_a_repeated_query() -> None:
    hits = [_hit("ses_a", 2, [("user", "terraform")])]
    ctx = ToolCtx(session_id="cur", extra={"session_search": _fake_search(hits)})

    first = await _registry().dispatch("SessionSearch", {"query": "terraform"}, ctx)
    assert "identical search already run" not in first.text

    second = await _registry().dispatch("SessionSearch", {"query": "terraform"}, ctx)
    assert "identical search already run" in second.text
    # The listing is not repeated — the pointer stands in for it.
    assert "ses_a" not in second.text
    assert "session(s) matched" in second.text  # the header still locates it

    other = await _registry().dispatch("SessionSearch", {"query": "billing"}, ctx)
    assert "identical search already run" not in other.text
