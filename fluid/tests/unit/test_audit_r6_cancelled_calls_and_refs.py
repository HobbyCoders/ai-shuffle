"""Round-6 audit: the calls an aborted cell drops, and the refs it never names.

Two losses with one shape — the kernel knows something the model needs and does
not say it:

* a tool exception aborts the cell, and the calls queued *after* it vanish
  without a word (five audited sessions re-issued bookkeeping, commits and
  Monitor launches a turn later; one loss surfaced twelve model calls later);
* refs were announced only when stdout was truncated, and only for results big
  enough to have become refs in the first place — a cell that batched three
  whole-file Reads named one of them and cut the other two with no way back,
  while the notice called that one ref "this cell's full tool output".
"""

from pathlib import Path

import pytest

from fluid.codemode import _runner
from fluid.codemode.bridge import HostBridge
from fluid.codemode.projection import Projection
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.codemode.truncate import (
    ReadPointer,
    _recovery_clause,
    refs_footer,
    repeat_read_pointer,
    windowed_read_pointer,
)
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import read as read_tool
from fluid.tools.registry import ToolCtx, ToolRegistry

# ── 1. the calls the error cancelled ──────────────────────────────────────

_KNOWN = {"Read", "Edit", "Bash", "Note"}


def test_queued_calls_after_the_failure_are_found_by_name() -> None:
    code = (
        "from tools import Read, Edit, Note\n"      # 1
        "Read(file_path='a.py')\n"                  # 2
        "Edit(file_path='a.py')\n"                  # 3 — the one that raised
        "Edit(file_path='b.py')\n"                  # 4
        "Note(description='ledger row')\n"          # 5
    )
    found = _runner._tool_calls_after(code, 3, known=_KNOWN)

    assert [(name, label) for _line, name, label, _cond in found] == [
        ("Edit", "b.py"),
        ("Note", "ledger row"),
    ]
    assert not any(cond for *_rest, cond in found)


def test_a_call_behind_a_branch_or_a_def_is_only_possible() -> None:
    code = (
        "Edit(file_path='a.py')\n"                  # 1 — the one that raised
        "if ok:\n"                                  # 2
        "    Bash(command='git commit -m x')\n"      # 3
        "def helper():\n"                           # 4
        "    Edit(file_path='never.py')\n"           # 5
        "Note(description='done')\n"                # 6
    )
    found = _runner._tool_calls_after(code, 1, known=_KNOWN)
    by_name = {name: cond for _line, name, _label, cond in found}

    assert by_name == {"Bash": True, "Edit": True, "Note": False}


def test_calls_before_the_failure_are_not_reported() -> None:
    code = "Read(file_path='a.py')\nEdit(file_path='a.py')\n"
    assert _runner._tool_calls_after(code, 2, known=_KNOWN) == []
    # A cell that never parsed ran nothing; there is nothing to report either.
    assert _runner._tool_calls_after("def (:\n", 1, known=_KNOWN) == []


def test_a_python_name_that_is_not_a_projected_tool_is_ignored() -> None:
    code = "Edit(file_path='a.py')\nprint(len('x'))\nPath('y')\n"
    assert _runner._tool_calls_after(code, 1, known=_KNOWN) == []


# ── 2. refs: announced, complete, and honest about what they hold ─────────


def test_the_recovery_clause_names_each_result_not_the_cell() -> None:
    clause = _recovery_clause([(4, 23_978, "Read"), (5, 8_002, "Grep")])

    # The old wording called one ref "this cell's full tool output" when it was
    # one result of three.
    assert "This cell's full tool output" not in clause
    assert "_refs[4] Read (23,978 bytes)" in clause
    assert "_refs[5] Grep (8,002 bytes)" in clause


def test_the_clause_names_more_than_three_handles() -> None:
    clause = _recovery_clause([(i, 1_000 * i, "Read") for i in range(1, 9)])
    for i in range(1, 9):
        assert f"_refs[{i}] Read" in clause


def test_a_pair_without_a_tool_name_still_renders() -> None:
    assert "_refs[4] (27,696 bytes)" in _recovery_clause([(4, 27_696)])


def test_refs_footer_announces_handles_with_no_truncation() -> None:
    footer = refs_footer([(1, 23_978, "Read"), (2, 900, "Grep")])
    assert footer.startswith("[refs created this cell:")
    assert "_refs[1] Read (23,978 bytes)" in footer
    assert "_refs[2] Grep (900 bytes)" in footer
    assert refs_footer([]) == ""


# ── 2b. the pointer is a pointer, and says so to code ─────────────────────


def test_repeat_pointer_leads_with_the_slice_expression() -> None:
    text = repeat_read_pointer(7, 55_123)
    assert text.startswith("unchanged since your earlier read")
    assert "text = _refs[7]; print(text[:2000])" in text
    assert "55,123 bytes" in text


def test_the_pointer_refuses_to_be_read_as_content() -> None:
    pointer = repeat_read_pointer(7, 55_123)

    # It is still a string: printing it, comparing it and formatting it work.
    assert isinstance(pointer, str)
    assert "_refs[7]" in f"{pointer}"
    # …but the two operations that silently consume it AS the file raise, and
    # the error carries the expression that does work.
    with pytest.raises(TypeError) as excinfo:
        pointer.splitlines()
    assert "_refs[7]" in str(excinfo.value)
    with pytest.raises(TypeError):
        list(pointer)
    with pytest.raises(TypeError):
        pointer.split("\n")


def test_the_pointer_repr_carries_the_guidance() -> None:
    # A bare tool call in a cell's last expression reaches the model through
    # repr(), so the repr may not swallow the text.
    rendered = repr(windowed_read_pointer(3, 10, 49, None))
    assert rendered.startswith("<ReadPointer #3:")
    assert "_refs[3].splitlines()[49:]" in rendered
    assert ReadPointer("x", key=2, size=9).key == 2


# ── 4. end to end, through a real kernel ──────────────────────────────────


class _MiniLoop:
    """The minimal ``loop`` surface ``CodeModeSession.run_python`` reads."""

    def __init__(self, registry) -> None:
        self.registry = registry
        self.ctx = ToolCtx()
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)

    async def _await_permission(self, *a, **k):  # pragma: no cover - never asked
        return "allow", None, None


def _bookkeeping_registry():
    from fluid.tools.inproc import create_inproc_server, tool

    @tool("Read", "Read a file.",
          {"type": "object", "properties": {"file_path": {"type": "string"}},
           "required": ["file_path"]})
    async def read(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"body of {args['file_path']}"}]}

    @tool("Edit", "Edit a file.", risk="write",
          input_schema={"type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"]})
    async def edit(args: dict) -> dict:
        if args["file_path"].endswith("missing.py"):
            return {"content": [{"type": "text", "text": "no such file"}],
                    "is_error": True}
        return {"content": [{"type": "text", "text": f"edited {args['file_path']}"}]}

    @tool("Note", "Record a note.",
          {"type": "object", "properties": {"description": {"type": "string"}},
           "required": ["description"]})
    async def note(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "noted"}]}

    return _as_builtins(ToolRegistry(), [read, edit, note])


def _as_builtins(registry, tools):
    """Round 9: in-process server errors are soft results in code mode, so
    the deliberately failing Edit below is registered as a BUILTIN, whose
    error still aborts the cell (what the manifest is for)."""
    from fluid.tools.registry import ToolOutcome, ToolSpec

    for t in tools:
        async def handler(input, ctx, _fn=t.handler):
            raw = await _fn(input)
            text = "".join(
                part.get("text", "") for part in raw.get("content", [])
                if isinstance(part, dict)
            )
            return ToolOutcome(text=text, is_error=bool(raw.get("is_error") or raw.get("isError")))
        registry.register_builtin(
            ToolSpec(name=t.name, description=t.description,
                     input_schema=t.input_schema, risk=t.risk),
            handler,
        )
    return registry


async def test_an_aborted_cell_names_the_calls_it_never_attempted() -> None:
    from fluid.codemode.session import CodeModeSession

    registry = _bookkeeping_registry()
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    code = (
        "from tools import Read, Edit, Note\n"
        "Read(file_path='a.py')\n"
        "Edit(file_path='a.py')\n"
        "Edit(file_path='missing.py')\n"   # raises ToolError; the cell aborts
        "Edit(file_path='c.py')\n"
        "Note(description='ledger row')\n"
    )
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30}, loop=loop
        )
    finally:
        await session.aclose()

    assert outcome.is_error
    line = next(
        l for l in outcome.text.splitlines() if l.startswith("never attempted")
    )
    # Numbered on from the host's completed-call manifest (1. Read, 2. Edit,
    # 3. Edit FAILED), so the two lists read as one sequence.
    assert "4. Edit(c.py)" in line
    assert "5. Note(ledger row)" in line
    assert "already on disk" in line


async def test_a_cell_that_fails_with_nothing_queued_says_nothing() -> None:
    from fluid.codemode.session import CodeModeSession

    registry = _bookkeeping_registry()
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1",
            input={"code": "from tools import Edit\nEdit(file_path='missing.py')\n",
                   "timeout": 30},
            loop=loop,
        )
    finally:
        await session.aclose()

    assert outcome.is_error
    assert "never attempted" not in outcome.text


class _Kernel:
    """One kernel + one bridge over the real Read tool."""

    def __init__(self, cwd: Path) -> None:
        registry = ToolRegistry()
        registry.register_builtin(read_tool.SPEC, read_tool.handler)
        self._registry = registry
        self._bridge = HostBridge(
            registry,
            PermissionEngine("never_ask"),
            ctx=ToolCtx(cwd=str(cwd)),
            doom_loop=False,
            result_budget=KernelConfig().ref_threshold,
        )
        self._kernel = SubprocessKernel(KernelConfig())

    async def __aenter__(self):
        await self._kernel.start()
        await self._kernel.set_vfs(Projection(self._registry.list_tools()).sources())
        return self

    async def __aexit__(self, *exc) -> None:
        await self._kernel.stop()

    async def run(self, code: str):
        return await self._kernel.exec(
            code, timeout=30, on_host_call=self._bridge.on_host_call
        )


def _fill(path: Path, marker: str, lines: int) -> str:
    path.write_text(
        "".join(f"{marker} {i} " + "s" * 60 + "\n" for i in range(lines)),
        encoding="utf-8",
    )
    return repr(str(path))


async def test_every_result_of_an_overrunning_cell_stays_recoverable(tmp_path):
    big = _fill(tmp_path / "big.txt", "big", 400)      # well over the threshold
    mid = _fill(tmp_path / "mid.txt", "mid", 90)       # under it, over SPILL_MIN
    small = _fill(tmp_path / "small.txt", "small", 70)

    async with _Kernel(tmp_path) as k:
        out = await k.run(
            "from tools.Read import Read\n"
            f"a = Read(file_path={big})\n"
            f"b = Read(file_path={mid})\n"
            f"c = Read(file_path={small})\n"
            "print(a)\nprint(b)\nprint(c)\n"
        )
        assert out.error is None
        assert "stdout truncated" in out.stdout
        # All three results are named — not just the one big enough to have
        # become a ref on its own.
        for key in (1, 2, 3):
            assert f"_refs[{key}] Read (" in out.stdout

        # …and the sub-threshold ones really are in the kernel.
        back = await k.run("(_refs[2].splitlines()[0][:10], 'small 0' in _refs[3])")
        assert back.error is None
        assert "mid 0" in back.value_repr and "True" in back.value_repr


async def test_a_cell_under_the_cap_still_announces_its_refs(tmp_path):
    big = _fill(tmp_path / "big.txt", "big", 400)

    async with _Kernel(tmp_path) as k:
        out = await k.run(
            f"from tools.Read import Read\na = Read(file_path={big})\nprint(len(a))\n"
        )
        assert out.error is None
        assert "stdout truncated" not in out.stdout
        assert "refs created this cell: _refs[1] Read (" in out.stdout


async def test_a_cell_with_no_refs_gets_no_footer(tmp_path):
    async with _Kernel(tmp_path) as k:
        out = await k.run("print('hello')\n")
        assert out.stdout == "hello\n"


async def test_a_missing_file_used_as_text_numbers_on_after_the_read(tmp_path) -> None:
    # A missing Read returns a MissingFile and counts as a completed call (the
    # host lists it as "1. Read(...) FAILED"). The MissingPathError raised
    # when the cell then uses it as text is not a host call, so the queued
    # calls pick up at 2, not 3.
    from fluid.codemode.session import CodeModeSession
    from fluid.tools.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    session = CodeModeSession(registry)
    loop = _MiniLoop(registry)
    loop.ctx = ToolCtx(cwd=str(tmp_path))
    code = (
        "from tools import Read, Glob\n"
        "text = Read(file_path='absent.txt')\n"
        "text.splitlines()\n"
        "Glob(pattern='*.py')\n"
    )
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 30}, loop=loop
        )
    finally:
        await session.aclose()

    assert outcome.is_error
    assert "MissingPathError" in outcome.text
    assert "1. Read(absent.txt) FAILED" in outcome.text
    line = next(
        l for l in outcome.text.splitlines() if l.startswith("never attempted")
    )
    assert "2. Glob(*.py)" in line, line
