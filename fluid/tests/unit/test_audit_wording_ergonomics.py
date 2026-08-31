"""Audit batch: wording and ergonomics fixes, each grounded in a real session.

The failures these pin, in order:

* PowerShell written into Bash three times in one session — Git Bash ate the
  ``$``-sigils and the command ran with different text than the model wrote.
* ``git commit -m "$1"`` issued after the model predicted its own failure: the
  rule forbade the multi-line body without naming ``-F``/``--body-file``, and
  the scratch file a later session did write landed inside the repo and got
  committed by ``git add -A``.
* Twenty-two test commands piped through ``tail``: one came back as
  ``(no output) [exit code 0]`` and cost a four-minute re-run.
* Two sessions lost a cell to an unterminated string holding a long markdown
  body.
* A Write under a worktree's ``.git`` failed with a bare ``[WinError 183]``.
* Read's ``lineno<TAB>`` prefix pasted into four Edit calls in one cell.
* Note ``remove`` with no match aborted a code-mode cell that had just run a
  passing 35s test suite; retiring seven notes cost seven calls.
* Twelve model calls spent on ToolSearch from inside the kernel: the hint named
  ``list_tools``/``search_tools`` (top-level tool calls, not in-kernel
  functions), the results named wire names the kernel cannot import, and one
  bad name in ``select:`` threw away the whole call.
"""

from __future__ import annotations

import errno

import pytest

from fluid.codemode import vfs
from fluid.codemode._runner import _UNTERMINATED_HINT
from fluid.codemode.session import _RUN_PYTHON_DESC, CodeModeSession
from fluid.tools.builtins import bash as bash_mod
from fluid.tools.builtins import edit as edit_mod
from fluid.tools.builtins import read as read_mod
from fluid.tools.builtins import write as write_mod
from fluid.tools.builtins.note import handler as note_handler
from fluid.tools.builtins.tool_search import handler as tool_search_handler
from fluid.tools.builtins.write import handler as write_handler
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec
from fluid.tools.shell import resolve_shell


def _ctx(tmp_path) -> ToolCtx:
    return ToolCtx(cwd=str(tmp_path), session_id="s1")


# ─── 1. Bash names Git Bash and the PowerShell remedy ────────────────────────


def test_bash_description_warns_that_powershell_is_mangled() -> None:
    text = bash_mod.describe()
    assert "On Windows that shell is Git Bash, not PowerShell" in text
    assert "$env:NAME" in text and "MANGLED" in text
    # The remedy, not just the prohibition.
    assert "powershell -File script.ps1" in text
    assert "with Write" in text  # full surface: name the tool that makes the file


def test_bash_powershell_remedy_drops_the_write_mention_without_write() -> None:
    text = bash_mod.describe(frozenset({"Bash"}))
    assert "powershell -File script.ps1" in text
    assert "Write" not in text


# ─── 2. The multi-line-body rule carries its remedy ──────────────────────────


def test_code_mode_body_rule_names_the_file_route_and_steers_outside_the_repo() -> None:
    from fluid.cli.prompt import compose_code_mode_addendum

    for text in (_RUN_PYTHON_DESC, compose_code_mode_addendum()):
        assert "git commit -F <file>" in text
        assert "--body-file <file>" in text
        assert "OUTSIDE the repo working tree" in text
        assert "git add -A" in text


def test_body_rule_without_write_keeps_the_variable_route_only() -> None:
    from fluid.cli.prompt import compose_code_mode_addendum

    text = compose_code_mode_addendum(frozenset({"Bash"}))
    assert "triple-quoted variable" in text
    assert "Write" not in text


# ─── 3. A pipe that ate the output says so ───────────────────────────────────


@pytest.mark.asyncio
async def test_empty_result_after_a_tail_pipe_blames_the_pipe(tmp_path) -> None:
    if not resolve_shell().posix:
        pytest.skip("no POSIX shell available on this machine")
    out = await bash_mod.handler(
        {"command": "true | tail -20"}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error
    assert "(no output) [exit code 0]" in out.text
    assert "the pipe may have consumed the output" in out.text
    assert "re-run it without the pipe" in out.text


@pytest.mark.asyncio
async def test_plain_silent_success_is_left_alone(tmp_path) -> None:
    if not resolve_shell().posix:
        pytest.skip("no POSIX shell available on this machine")
    out = await bash_mod.handler({"command": "true"}, ToolCtx(cwd=str(tmp_path)))
    assert out.text == "(no output) [exit code 0]"


# ─── 4. The unterminated-string hint names the long-prose case ───────────────


def test_unterminated_hint_covers_a_long_markdown_argument() -> None:
    assert "heredoc" in _UNTERMINATED_HINT
    assert "markdown report" in _UNTERMINATED_HINT
    assert "triple-quoted variable" in _UNTERMINATED_HINT
    assert "a delimiter the text does not itself contain" in _UNTERMINATED_HINT
    assert "git commit -F <file>" in _UNTERMINATED_HINT
    assert "outside the repo working tree" in _UNTERMINATED_HINT


# ─── 5. Write translates the worktree .git failure ───────────────────────────


@pytest.mark.asyncio
async def test_write_under_a_worktree_git_file_explains_itself(tmp_path) -> None:
    """In a worktree ``.git`` is a FILE holding a ``gitdir:`` pointer, so
    ``mkdir(parents=True)`` hits a file where it wants a directory. Raw, that
    is ``[WinError 183] Cannot create a file when that file already exists``."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")

    out = await write_handler(
        {"file_path": str(worktree / ".git" / "body.md"), "content": "hi"},
        _ctx(tmp_path),
    )
    assert out.is_error
    assert "exists as a file, not a directory" in out.text
    assert "gitdir:" in out.text
    assert "OUTSIDE the repo working tree" in out.text


def test_write_failure_inside_a_git_directory_says_why() -> None:
    exc = PermissionError(errno.EACCES, "Permission denied")
    from pathlib import Path

    message = write_mod._write_failure(Path("/repo/.git/objects/tmp"), exc)
    assert "inside git's internal .git directory" in message
    assert "temp directory outside the repo" in message


def test_write_failure_elsewhere_keeps_the_plain_reason() -> None:
    from pathlib import Path

    exc = OSError(errno.ENOSPC, "No space left on device")
    message = write_mod._write_failure(Path("/tmp/out.txt"), exc)
    assert message.startswith("cannot write ")
    assert "No space left on device" in message
    assert ".git" not in message


@pytest.mark.asyncio
async def test_write_still_writes(tmp_path) -> None:
    target = tmp_path / "sub" / "a.txt"
    out = await write_handler(
        {"file_path": str(target), "content": "x"}, _ctx(tmp_path)
    )
    assert not out.is_error and target.read_text() == "x"


# ─── 6. The line-number prefix is named where it is produced ─────────────────


def test_read_contract_says_to_strip_the_line_prefix_for_edit() -> None:
    text = read_mod.describe()
    assert "strip the line-number prefix" in text
    assert "Edit's old_string" in text
    # Without Edit on the surface the clause must not name it.
    assert "Edit" not in read_mod.describe(frozenset({"Read"}))


def test_edit_no_match_message_names_the_line_prefix() -> None:
    file_lines = ["def f():", "    return 1"]
    # No line matches at all.
    message = edit_mod._diagnose(file_lines, ["nothing like this"])
    assert "lineno<TAB>" in message
    # First line matches, later content does not.
    message = edit_mod._diagnose(file_lines, ["def f():", "    return 2"])
    assert "no line-number prefixes" in message  # the quoted file text says so


# ─── 7. Note: idempotent removal, batch removal ──────────────────────────────


@pytest.mark.asyncio
async def test_note_remove_no_match_is_a_soft_error(tmp_path) -> None:
    """A hard error raises inside a code-mode cell and throws away every call
    after it — one audited session lost a passing 35s vitest run to this."""
    ctx = _ctx(tmp_path)
    await note_handler({"action": "add", "texts": ["step one"]}, ctx)

    near = await note_handler({"action": "remove", "text": "step"}, ctx)
    assert near.is_error and near.soft_error

    far = await note_handler({"action": "remove", "text": "nothing like it"}, ctx)
    assert far.is_error and far.soft_error
    assert "action=list" in far.text


@pytest.mark.asyncio
async def test_note_remove_takes_a_batch(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await note_handler(
        {"action": "add", "texts": ["one", "two", "three", "four"]}, ctx
    )
    out = await note_handler({"action": "remove", "texts": ["one", "three"]}, ctx)
    assert not out.is_error
    assert "removed 2 notes" in out.text and "2 remain" in out.text
    listing = await note_handler({"action": "list"}, ctx)
    assert "two" in listing.text and "four" in listing.text
    assert "\n- one" not in listing.text and "\n- three" not in listing.text


@pytest.mark.asyncio
async def test_note_remove_batches_newline_separated_text_too(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await note_handler({"action": "add", "texts": ["one", "two"]}, ctx)
    out = await note_handler({"action": "remove", "text": "one\ntwo"}, ctx)
    assert not out.is_error and "removed 2 notes" in out.text
    assert "(empty)" in out.text


@pytest.mark.asyncio
async def test_note_partial_batch_removal_still_succeeds(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await note_handler({"action": "add", "texts": ["one", "two"]}, ctx)
    out = await note_handler({"action": "remove", "texts": ["one", "ghost"]}, ctx)
    assert not out.is_error
    assert "removed 1 note" in out.text
    assert 'no note read exactly "ghost"' in out.text
    listing = await note_handler({"action": "list"}, ctx)
    assert "two" in listing.text


@pytest.mark.asyncio
async def test_note_remove_with_nothing_at_all_still_errors(tmp_path) -> None:
    out = await note_handler({"action": "remove"}, _ctx(tmp_path))
    assert out.is_error and "requires text or texts" in out.text


@pytest.mark.asyncio
async def test_note_no_match_message_lists_every_needle(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await note_handler({"action": "add", "texts": ["one"]}, ctx)
    out = await note_handler({"action": "remove", "texts": ["ghost", "wraith"]}, ctx)
    assert out.is_error and out.soft_error
    assert '"ghost" or "wraith"' in out.text


# ─── 8. Discovery from inside the kernel ─────────────────────────────────────


def test_vfs_says_list_tools_is_not_an_in_kernel_function() -> None:
    package = vfs._VfsPackage("tools", "doc", lambda: ["Read", "Grep"])
    with pytest.raises(AttributeError) as excinfo:
        package.list_tools
    message = str(excinfo.value)
    assert "top-level tool call, not an in-kernel function" in message
    assert "dir(tools)" in message

    with pytest.raises(AttributeError) as excinfo:
        package.Nope
    assert "`dir(tools)` lists what is mounted here" in str(excinfo.value)


def test_vfs_missing_skill_points_at_dir_skills() -> None:
    package = vfs._VfsPackage("skills", "doc", lambda: ["greet"])
    with pytest.raises(AttributeError) as excinfo:
        package.absent
    assert "skills has no skill 'absent'" in str(excinfo.value)
    assert "`dir(skills)` lists what is mounted here" in str(excinfo.value)


def test_vfs_package_still_lists_its_leaves() -> None:
    package = vfs._VfsPackage("tools", "doc", lambda: ["Read", "Grep"])
    assert {"Read", "Grep"} <= set(dir(package))
    assert package.__all__ == ["Grep", "Read"]


# ─── 8b. ToolSearch: partial select, kernel-shaped results ───────────────────


def _search_registry() -> ToolRegistry:
    from fluid.tools.registry import _Entry

    registry = ToolRegistry()
    for server, tool in (("browser", "navigate"), ("browser", "screenshot")):
        spec = ToolSpec(
            name=f"mcp__{server}__{tool}",
            description=f"{tool} via {server}",
            input_schema={"type": "object", "properties": {}},
            source="mcp",
        )
        registry._entries[spec.name] = _Entry(spec=spec, server=server)
    return registry


@pytest.mark.asyncio
async def test_select_loads_the_valid_names_and_reports_the_rest() -> None:
    registry = _search_registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await tool_search_handler(
        {"query": "select:mcp__browser__navigate,mcp__browser__nope"}, ctx
    )
    assert not out.is_error
    assert "mcp__browser__navigate" in out.text
    assert "Not loaded — no such tool: mcp__browser__nope" in out.text
    assert "mcp__browser__navigate" in {
        s.name for s in registry.list_tools(loaded_only=True)
    }


@pytest.mark.asyncio
async def test_select_with_no_valid_name_at_all_is_still_an_error() -> None:
    registry = _search_registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await tool_search_handler({"query": "select:mcp__browser__nope"}, ctx)
    assert out.is_error and "unknown tool(s): mcp__browser__nope" in out.text


@pytest.mark.asyncio
async def test_tool_search_from_the_kernel_prints_the_import_line() -> None:
    registry = _search_registry()
    ctx = ToolCtx(extra={"tool_registry": registry, "in_kernel_call": True})
    out = await tool_search_handler(
        {"query": "select:mcp__browser__navigate"}, ctx
    )
    assert not out.is_error
    assert "from tools.navigate import navigate" in out.text
    assert "(wire name: mcp__browser__navigate)" in out.text
    # The JSON-surface promise is false in a cell: the tool is importable now.
    assert "callable from your next reply" not in out.text


@pytest.mark.asyncio
async def test_tool_search_on_the_json_surface_is_unchanged() -> None:
    registry = _search_registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await tool_search_handler(
        {"query": "select:mcp__browser__navigate"}, ctx
    )
    assert "callable from your next reply" in out.text
    assert "from tools.navigate import navigate" not in out.text


# ─── 8c. The kernel marks its own calls so ToolSearch can tell ───────────────


class _FakeLoop:
    """The bits of ``NativeLoop`` that ``CodeModeSession.run_python`` touches."""

    permissions = None
    hooks = None

    def __init__(self) -> None:
        self.ctx = ToolCtx()

    async def _emit(self, _event: object) -> None:
        return None

    async def _await_permission(self, *_args: object) -> tuple[str, None, None]:
        return ("allow", None, None)


@pytest.mark.asyncio
async def test_in_kernel_calls_are_marked_on_the_ctx_and_unmarked_after() -> None:
    async def _echo(_input: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=f"in_kernel={ctx.extra.get('in_kernel_call')!r}")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Echo", description="Echo the marker.",
            input_schema={"type": "object", "properties": {}}, source="builtin",
        ),
        _echo,
    )
    session = CodeModeSession(registry)
    loop = _FakeLoop()
    try:
        out = await session.run_python(
            sid="s1", turn_id="t1", loop=loop,
            input={"code": "print(__host_call__('Echo', {}))"},
        )
        assert not out.is_error, out.text
        assert "in_kernel=True" in out.text
    finally:
        await session.aclose()
    # Restored: a later JSON-path call must not inherit the marker.
    assert "in_kernel_call" not in loop.ctx.extra
