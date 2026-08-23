"""M2 unit tests — registry/inproc, Edit discipline, permissions, hooks."""

import sys
from pathlib import Path

from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.permissions.engine import PermissionEngine, plan_mode_tools
from fluid.permissions.rules import Rule, evaluate_rules
from fluid.tools.builtins import register_builtins
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry, ToolSpec


async def test_inproc_server_naming_and_dispatch() -> None:
    @tool("hello", "Say hi", {"type": "object", "properties": {"name": {"type": "string"}}})
    async def hello(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"hi {args['name']}"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("srv", [hello]))
    assert registry.get("mcp__srv__hello") is not None
    assert [s.name for s in registry.list_tools()] == ["mcp__srv__hello"]

    out = await registry.dispatch("mcp__srv__hello", {"name": "fluid"}, ToolCtx())
    assert out.text == "hi fluid"
    assert not out.is_error


async def test_inproc_accepts_plain_str_and_tooloutcome() -> None:
    from fluid.tools.registry import ToolOutcome

    @tool("s", "returns str", {"type": "object"})
    async def s(args: dict):
        return "plain string"

    @tool("o", "returns outcome", {"type": "object"})
    async def o(args: dict):
        return ToolOutcome(text="structured", ui={"x": 1})

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("srv", [s, o]))
    assert (await registry.dispatch("mcp__srv__s", {}, ToolCtx())).text == "plain string"
    outcome = await registry.dispatch("mcp__srv__o", {}, ToolCtx())
    assert outcome.text == "structured" and outcome.ui == {"x": 1}


async def test_edit_read_before_write_and_uniqueness(tmp_path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 1\nz = 2\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))

    # Edit before Read → refused.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "z = 2", "new_string": "z = 3"}, ctx)
    assert out.is_error and "must Read" in out.text

    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    # Non-unique old_string without replace_all → refused.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "= 1", "new_string": "= 9"}, ctx)
    assert out.is_error and "not unique" in out.text

    # replace_all succeeds.
    out = await registry.dispatch(
        "Edit", {"file_path": str(f), "old_string": "= 1", "new_string": "= 9", "replace_all": True}, ctx
    )
    assert not out.is_error
    assert f.read_text() == "x = 9\ny = 9\nz = 2\n"

    # Unique edit succeeds.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "z = 2", "new_string": "z = 3"}, ctx)
    assert not out.is_error and f.read_text().endswith("z = 3\n")


async def test_write_then_edit_allowed(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    f = tmp_path / "new.txt"
    await registry.dispatch("Write", {"file_path": str(f), "content": "hello"}, ctx)
    # Write registers the file as known, so a follow-up Edit is allowed.
    out = await registry.dispatch("Edit", {"file_path": str(f), "old_string": "hello", "new_string": "bye"}, ctx)
    assert not out.is_error and f.read_text() == "bye"


def test_permission_policies() -> None:
    safe = ToolSpec(name="Read", description="", input_schema={}, risk="safe")
    write = ToolSpec(name="Edit", description="", input_schema={}, risk="write")

    assert PermissionEngine("never_ask").evaluate(write, {}) == "allow"
    assert PermissionEngine("always_ask").evaluate(safe, {}) == "ask"
    on_write = PermissionEngine("on_write")
    assert on_write.evaluate(safe, {}) == "allow"
    assert on_write.evaluate(write, {}) == "ask"


def test_rules_last_match_wins_and_input_globs() -> None:
    rules = [
        Rule(tool="*", decision="allow"),
        Rule(tool="Bash", decision="ask"),
        Rule(tool="Bash", decision="deny", input_globs={"command": "rm *"}),
    ]
    assert evaluate_rules(rules, "Read", {}) == "allow"
    assert evaluate_rules(rules, "Bash", {"command": "ls"}) == "ask"
    assert evaluate_rules(rules, "Bash", {"command": "rm -rf /"}) == "deny"


def test_doom_loop_forces_ask_after_three() -> None:
    engine = PermissionEngine("never_ask")
    call = ("Bash", {"command": "flaky"})
    assert engine.note_execution(*call) is False  # 1
    assert engine.note_execution(*call) is False  # 2
    assert engine.note_execution(*call) is True   # 3 → force ask, then reset
    assert engine.note_execution(*call) is False  # streak reset
    # a distinct call resets the streak immediately
    assert engine.note_execution("Bash", {"command": "other"}) is False


def test_plan_mode_tools_filter() -> None:
    specs = [
        ToolSpec(name="Read", description="", input_schema={}, risk="safe"),
        ToolSpec(name="Bash", description="", input_schema={}, risk="write"),
        ToolSpec(name="Rm", description="", input_schema={}, risk="destructive"),
    ]
    kept = {t.name for t in plan_mode_tools(specs)}
    assert kept == {"Read"}


async def test_hook_modify_replaces_input() -> None:
    bus = HookBus()

    async def rewrite(event: str, payload: dict):
        return HookDecision(action="modify", replacement_input={"command": "safe"})

    bus.on(PRE_TOOL_USE, rewrite)
    decision = await bus.emit(PRE_TOOL_USE, {"name": "Bash", "input": {"command": "danger"}})
    assert decision.action == "modify"
    assert decision.replacement_input == {"command": "safe"}


def test_search_tools_ranks_by_match() -> None:
    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="Read", description="read a file", input_schema={}), _noop
    )
    registry.register_builtin(
        ToolSpec(name="Grep", description="search file contents by regex", input_schema={}), _noop
    )
    hits = [s.name for s in registry.search_tools("search file")]
    assert hits[0] == "Grep"  # matches both words, ranked first


async def _noop(input, ctx):  # pragma: no cover - registration placeholder
    return "ok"


# --- Grep / Glob: bounded walks, error surfacing --------------------------


def _tree(tmp_path):
    """A small repo-shaped tree with one needle in a pruned directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha needle here\nplain\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("needle in deps\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "COMMIT_EDITMSG").write_text("needle in git\n")
    return tmp_path


async def test_grep_fallback_prunes_heavy_directories(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)

    _tree(tmp_path)
    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))

    assert not out.is_error
    assert out.text.startswith("searched via python-fallback under ")
    assert "app.py" in out.text
    assert "node_modules" not in out.text
    assert "COMMIT_EDITMSG" not in out.text


async def test_grep_fallback_skips_binary_files(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)

    (tmp_path / "text.txt").write_text("needle\n")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00\x01\x02more needle\n")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))

    assert "text.txt" in out.text
    assert "blob.bin" not in out.text


async def test_grep_fallback_caps_matches_and_marks_truncation(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)

    (tmp_path / "many.txt").write_text("needle\n" * 500)
    # A second file that would also match: the cap must stop before reaching it.
    (tmp_path / "zzz_later.txt").write_text("needle\n")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))

    hits = [ln for ln in out.text.splitlines() if ln.endswith(":needle")]
    assert len(hits) == grep_mod.MAX_MATCHES
    assert (
        f"[search truncated: match cap {grep_mod.MAX_MATCHES} reached — "
        "pass a higher limit or narrow the pattern]"
    ) in out.text
    assert "zzz_later.txt" not in out.text


async def test_grep_fallback_honours_explicit_limit(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)
    (tmp_path / "many.txt").write_text("needle\n" * 500)

    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch("Grep", {"pattern": "needle", "limit": 7}, ctx)
    hits = [ln for ln in out.text.splitlines() if ln.endswith(":needle")]
    assert len(hits) == 7
    assert "[search truncated: match cap 7 reached — " in out.text

    # A limit above the default lifts the cap, not just lowers it. (Line count
    # is compared loosely: the 30KB byte-truncation may drop some middle lines.)
    out = await registry.dispatch("Grep", {"pattern": "needle", "limit": 300}, ctx)
    hits = [ln for ln in out.text.splitlines() if ln.endswith(":needle")]
    assert len(hits) > grep_mod.MAX_MATCHES
    assert "[search truncated: match cap 200" not in out.text


async def test_grep_fallback_honours_glob_filter(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "a.md").write_text("needle\n")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Grep", {"pattern": "needle", "glob": "*.py"}, ToolCtx(cwd=str(tmp_path))
    )
    assert "a.py" in out.text and "a.md" not in out.text


async def test_grep_fallback_deadline_returns_partial(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(grep_mod, "FALLBACK_DEADLINE_S", 0.0)
    (tmp_path / "a.txt").write_text("needle\n")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))

    assert not out.is_error
    assert "[search truncated: deadline reached after" in out.text


async def test_glob_deadline_returns_partial(tmp_path, monkeypatch) -> None:
    from fluid.tools.builtins import glob as glob_mod

    monkeypatch.setattr(glob_mod, "DEADLINE_S", 0.0)
    (tmp_path / "a.py").write_text("x")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Glob", {"pattern": "*.py"}, ToolCtx(cwd=str(tmp_path)))

    assert "[search truncated: deadline reached after" in out.text


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._out, self._err = stdout, stderr
        self.killed = False

    async def communicate(self):
        return self._out, self._err

    def kill(self) -> None:  # pragma: no cover - only on the timeout path
        self.killed = True

    async def wait(self):  # pragma: no cover - only on the timeout path
        return self.returncode


async def test_grep_rg_error_is_surfaced_and_args_are_safe(tmp_path, monkeypatch) -> None:
    import asyncio as _asyncio

    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    captured: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return _FakeProc(2, b"", b"regex parse error: unclosed group")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Grep", {"pattern": "-foo(", "no_ignore": True}, ToolCtx(cwd=str(tmp_path))
    )

    assert out.is_error
    assert "rg error: regex parse error" in out.text
    assert "searched via rg under" in out.text
    # `--` guards a pattern that starts with '-'; -uu comes from no_ignore.
    assert captured[captured.index("--") + 1] == "-foo("
    assert "-uu" in captured
    assert "--max-count" in captured and "--max-filesize" in captured


async def test_grep_rg_limit_is_global_across_files(tmp_path, monkeypatch) -> None:
    """``--max-count`` is per-file; the cap the model asked for is global."""
    import asyncio as _asyncio

    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    captured: list[str] = []
    # Two files, two matches each: rg honoured --max-count per file, so four
    # lines come back even though the caller asked for three in total.
    stdout = b"".join(
        f"/repo/{name}:{n}:needle\n".encode() for name in ("a.py", "b.py") for n in (1, 2)
    )

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return _FakeProc(0, stdout, b"")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Grep", {"pattern": "needle", "limit": 3}, ToolCtx(cwd=str(tmp_path))
    )

    assert not out.is_error
    hits = [ln for ln in out.text.splitlines() if ln.endswith(":needle")]
    assert len(hits) == 3
    assert (
        "[showing first 3 of 4 matches — pass a higher limit or narrow the pattern]"
        in out.text
    )
    # The per-file bound is still handed to rg, set from the same limit.
    assert captured[captured.index("--max-count") + 1] == "3"


async def test_grep_rg_under_limit_has_no_note(tmp_path, monkeypatch) -> None:
    import asyncio as _asyncio

    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(0, b"/repo/a.py:1:needle\n", b"")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))
    assert out.text.endswith("/repo/a.py:1:needle")
    assert "showing first" not in out.text


async def test_grep_rg_no_matches_is_not_an_error(tmp_path, monkeypatch) -> None:
    import asyncio as _asyncio

    from fluid.tools.builtins import grep as grep_mod

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(1, b"", b"")

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Grep", {"pattern": "zzz"}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error and "(no matches)" in out.text


async def test_glob_matches_and_prunes(tmp_path) -> None:
    _tree(tmp_path)
    (tmp_path / "src" / "notes.md").write_text("x")

    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))

    out = await registry.dispatch("Glob", {"pattern": "**/*.py"}, ctx)
    assert not out.is_error
    assert "app.py" in out.text and "notes.md" not in out.text

    # Bare-name patterns match at any depth; pruned trees stay out entirely.
    out = await registry.dispatch("Glob", {"pattern": "*.js"}, ctx)
    assert "(no matches)" in out.text


async def test_glob_caps_results(tmp_path) -> None:
    from fluid.tools.builtins import glob as glob_mod

    total = glob_mod.MAX_RESULTS + 25
    for i in range(total):
        (tmp_path / f"f{i:04d}.txt").write_text("x")

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Glob", {"pattern": "*.txt"}, ToolCtx(cwd=str(tmp_path)))

    paths = [ln for ln in out.text.splitlines() if ln.endswith(".txt")]
    assert len(paths) == glob_mod.MAX_RESULTS
    assert (
        f"[showing newest {glob_mod.MAX_RESULTS} of {total} matched files — "
        "pass a higher limit or narrow the pattern]"
    ) in out.text


async def test_glob_sorts_newest_first_before_capping(tmp_path) -> None:
    """The cap must keep the NEWEST files, not whichever the walk saw first."""
    import os

    names = [f"f{i:02d}.txt" for i in range(10)]
    for i, name in enumerate(names):
        path = tmp_path / name
        path.write_text("x")
        # f00 is oldest, f09 newest — the walk order (alphabetical) is the
        # reverse of the wanted order, so a sort-after-cap keeps the wrong set.
        os.utime(path, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))

    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Glob", {"pattern": "*.txt", "limit": 3}, ToolCtx(cwd=str(tmp_path))
    )

    paths = [ln for ln in out.text.splitlines() if ln.endswith(".txt")]
    assert [Path(p).name for p in paths] == ["f09.txt", "f08.txt", "f07.txt"]
    assert (
        "[showing newest 3 of 10 matched files — "
        "pass a higher limit or narrow the pattern]"
    ) in out.text


async def test_grep_and_glob_limits_are_clamped(tmp_path) -> None:
    from fluid.tools.builtins import glob as glob_mod
    from fluid.tools.builtins import grep as grep_mod

    for mod, default in ((grep_mod, grep_mod.MAX_MATCHES), (glob_mod, glob_mod.MAX_RESULTS)):
        assert mod._clamp_limit(None) == default
        assert mod._clamp_limit("junk") == default
        assert mod._clamp_limit(0) == 1
        assert mod._clamp_limit(-5) == 1
        assert mod._clamp_limit(10**9) == mod.MAX_LIMIT
        assert mod._clamp_limit(mod.MAX_LIMIT + 1) == 5000
        assert mod._clamp_limit(42) == 42

    # And the clamp is what the tool actually applies: 0 yields one result.
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x")
    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch(
        "Glob", {"pattern": "*.txt", "limit": 0}, ToolCtx(cwd=str(tmp_path))
    )
    assert len([ln for ln in out.text.splitlines() if ln.endswith(".txt")]) == 1
    assert "[showing newest 1 of 2 matched files — " in out.text


async def test_edit_tolerates_uniform_extra_indent(tmp_path) -> None:
    f = tmp_path / "tabbed.py"
    f.write_text("def f():\n\ta = 1\n\tb = 2\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    # Every old_string line carries one tab too many (Read's lineno separator).
    out = await registry.dispatch(
        "Edit",
        {
            "file_path": str(f),
            "old_string": "\t\ta = 1\n\t\tb = 2",
            "new_string": "\t\ta = 9\n\t\tb = 8",
        },
        ctx,
    )
    assert not out.is_error, out.text
    assert "note:" in out.text
    assert f.read_text() == "def f():\n\ta = 9\n\tb = 8\n"


async def test_edit_tolerates_copied_line_prefix(tmp_path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 1\nz = 2\nw = 3\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit",
        {"file_path": str(f), "old_string": "3\tz = 2\n4\tw = 3", "new_string": "z = 5\nw = 6"},
        ctx,
    )
    assert not out.is_error, out.text
    assert "note:" in out.text
    assert f.read_text() == "x = 1\ny = 1\nz = 5\nw = 6\n"


async def test_edit_indent_mismatch_error_explains(tmp_path) -> None:
    f = tmp_path / "tabbed.py"
    f.write_text("\ta = 1\n\tb = 2\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    # Inconsistent drift: first line gains a tab, second does not.
    out = await registry.dispatch(
        "Edit",
        {"file_path": str(f), "old_string": "\t\ta = 1\n\tb = 2", "new_string": "\t\ta = 9\n\tb = 8"},
        ctx,
    )
    assert out.is_error
    assert "leading whitespace differs" in out.text
    assert "Re-Read" in out.text
    assert f.read_text() == "\ta = 1\n\tb = 2\n"


async def test_edit_content_mismatch_error_names_line(tmp_path) -> None:
    f = tmp_path / "words.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit",
        {"file_path": str(f), "old_string": "alpha\nbetaX", "new_string": "alpha\nbetaY"},
        ctx,
    )
    assert out.is_error
    assert "line 2 differs" in out.text
    assert "betaX" in out.text


async def test_edit_preserves_crlf(tmp_path) -> None:
    f = tmp_path / "crlf.py"
    f.write_bytes(b"def f():\r\n\ta = 1\r\n\tb = 2\r\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit",
        {
            "file_path": str(f),
            "old_string": "\t\ta = 1\n\t\tb = 2",
            "new_string": "\t\ta = 9\n\t\tb = 8",
        },
        ctx,
    )
    assert not out.is_error, out.text
    assert f.read_bytes() == b"def f():\r\n\ta = 9\r\n\tb = 8\r\n"


async def test_edit_identical_strings_rejected(tmp_path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolCtx(cwd=str(tmp_path))
    await registry.dispatch("Read", {"file_path": str(f)}, ctx)

    out = await registry.dispatch(
        "Edit", {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 1"}, ctx
    )
    assert out.is_error and "identical" in out.text
    assert f.read_text() == "x = 1\n"


async def test_bash_strips_ansi(tmp_path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    code = "import sys; sys.stdout.write('\x1b[31mred\x1b[0m plain')"
    out = await registry.dispatch(
        "Bash", {"command": f'"{sys.executable}" -c "{code}"'}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    assert "\x1b" not in out.text
    assert out.text.strip() == "red plain"
