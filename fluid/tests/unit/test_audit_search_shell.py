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


async def test_bash_nonempty_success_gains_no_status_noise(tmp_path) -> None:
    out = await _bash(tmp_path, "print('ok')")
    assert not out.is_error
    assert out.text.strip() == "ok"
    assert "exit code" not in out.text


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
    assert "ses_a" in second.text  # the results still come back in full

    other = await _registry().dispatch("SessionSearch", {"query": "billing"}, ctx)
    assert "identical search already run" not in other.text
