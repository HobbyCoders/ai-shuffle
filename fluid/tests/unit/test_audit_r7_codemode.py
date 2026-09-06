"""Round-7 production audit — the code-mode half.

Four failures, all of them things the kernel knew and did not say:

* a kernel recycled between turns produced no signal, so the next cell raised
  a bare ``NameError: name 'Note' is not defined. Did you mean: 'None'?`` for a
  name that had worked one turn earlier (four sessions; one re-billed a 52k
  uncached prefix twice recovering from it);
* the ``SyntaxError`` hint spent 600 characters on heredocs for a cell whose
  actual fault was the tool call's own ``"}`` left on the last line;
* ``re = Read(...)`` in one cell, ``re.match`` seven hundred sequence numbers
  later, and a hint about dicts-versus-strings;
* a ``run_python`` cell declaring ``timeout: 30`` sat three hours on a hung
  browser MCP call, with no result, no TurnComplete, and a session that still
  presented as running.
"""

from __future__ import annotations

import asyncio

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import stub_source
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.tools.builtins.grep import SPEC as GREP_SPEC


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(result="ok")


@pytest.fixture
async def kernel():
    k = SubprocessKernel()
    await k.start()
    await k.set_vfs({"Grep": stub_source(GREP_SPEC, "Grep")})
    try:
        yield k
    finally:
        await k.stop()


# ─── 1. hints the kernel can give from what it already knows ────────────────


async def test_nameerror_for_a_mounted_tool_says_the_kernel_restarted(kernel) -> None:
    result = await kernel.exec("Grep(pattern='x')", timeout=20, on_host_call=_no_tools)
    assert result.error
    assert "is a tool mounted in this kernel" in result.error
    assert "from tools import Grep" in result.error
    assert "the kernel has restarted since" in result.error


async def test_nameerror_for_an_ordinary_name_gets_no_tool_hint(kernel) -> None:
    result = await kernel.exec("undefined_thing + 1", timeout=20, on_host_call=_no_tools)
    assert result.error and "mounted in this kernel" not in result.error


async def test_syntax_hint_leads_with_the_stray_argument_envelope(kernel) -> None:
    # The exact shape of an audited session's first tool call of the day.
    result = await kernel.exec('r = 1\nprint(r)"}', timeout=20, on_host_call=_no_tools)
    assert result.error
    assert "argument envelope" in result.error
    # And it does not pay for the heredoc essay, which did not apply.
    assert "heredoc" not in result.error


async def test_unterminated_string_still_mentions_the_envelope_first(kernel) -> None:
    result = await kernel.exec('x = "no end', timeout=20, on_host_call=_no_tools)
    assert result.error
    envelope = result.error.index("argument envelope")
    assert envelope < result.error.index("heredoc")


async def test_shadowed_stdlib_module_is_named(kernel) -> None:
    # `re` is pre-bound; a cell rebinding it to a tool result shadows it.
    await kernel.exec("re = 'a tool result'", timeout=20, on_host_call=_no_tools)
    result = await kernel.exec("re.match('a', 'b')", timeout=20, on_host_call=_no_tools)
    assert result.error
    assert "`re` in this kernel is bound to a tool result" in result.error
    assert "import re" in result.error
    # The old hint pointed at dict-vs-str, which was not the fault.
    assert "never a dict" not in result.error


async def test_an_actual_string_attribute_error_keeps_the_old_hint(kernel) -> None:
    result = await kernel.exec("'text'.no_such_attr", timeout=20, on_host_call=_no_tools)
    assert result.error and "never a dict" in result.error


async def test_re_and_json_are_both_pre_imported(kernel) -> None:
    result = await kernel.exec(
        "print(re.match('a', 'abc').group(0), json.dumps([1]))",
        timeout=20,
        on_host_call=_no_tools,
    )
    assert not result.error
    assert "a [1]" in result.stdout


# ─── 2. a kernel that died between turns ────────────────────────────────────


async def test_a_kernel_that_died_between_turns_restores_and_says_so() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("kept = 41", timeout=20, on_host_call=_no_tools)
        # Kill it the way an eviction or an OOM would, between turns.
        proc = k._proc
        proc.kill()
        await proc.wait()

        result = await k.exec("kept + 1", timeout=20, on_host_call=_no_tools)
        assert not result.error, result.error
        # The checkpoint is replayed on this path now, so the value survives...
        assert result.value_repr == "42"
        # ...and the model is told the kernel is not the one it left behind.
        assert "kernel restarted since your last cell" in result.notes
        # Once said, it is not repeated on every later cell.
        again = await k.exec("kept + 2", timeout=20, on_host_call=_no_tools)
        assert again.notes == ""
    finally:
        await k.stop()


async def test_a_first_exec_carries_no_restart_notice() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        result = await k.exec("1 + 1", timeout=20, on_host_call=_no_tools)
        assert result.notes == ""
    finally:
        await k.stop()


async def test_an_in_cell_timeout_still_reports_inline_not_twice() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        slow = await k.exec("sum(range(10**9))", timeout=0.5, on_host_call=_no_tools)
        assert slow.error and "kernel restarted" in slow.error
        # The error already said it; the next cell must not say it again.
        assert slow.notes == ""
        after = await k.exec("1 + 1", timeout=20, on_host_call=_no_tools)
        assert after.notes == ""
    finally:
        await k.stop()


# ─── 3. a host call that never returns ──────────────────────────────────────


async def test_a_hung_host_call_is_cancelled_at_the_bound() -> None:
    """The audited hang: a browser MCP call that never answered.

    The cell declared ``timeout: 30`` and sat three hours; nothing above it
    could end the turn because the compute budget is not charged while a host
    call is in flight.
    """
    k = SubprocessKernel(KernelConfig(host_call_timeout=0.3))
    await k.start()
    await k.set_vfs({"Grep": stub_source(GREP_SPEC, "Grep")})
    cancelled = asyncio.Event()

    async def hang(name: str, kwargs: dict) -> HostReply:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return HostReply(result="never")

    try:
        result = await asyncio.wait_for(
            k.exec(
                "from tools import Grep\nGrep(pattern='x')",
                timeout=20,
                on_host_call=hang,
            ),
            timeout=30,
        )
        assert result.error
        assert "did not return within" in result.error
        assert "Grep" in result.error
        assert cancelled.is_set()
        # The cell died, not the kernel: the namespace is intact.
        assert not (await k.exec("1 + 1", timeout=20, on_host_call=_no_tools)).error
    finally:
        await k.stop()


async def test_time_spent_waiting_on_a_human_is_not_charged() -> None:
    """A permission prompt or an in-kernel AskUser may take as long as it takes."""
    k = SubprocessKernel(KernelConfig(host_call_timeout=0.4))
    await k.start()
    await k.set_vfs({"Grep": stub_source(GREP_SPEC, "Grep")})
    waited = 0.0

    async def slow_but_human(name: str, kwargs: dict) -> HostReply:
        nonlocal waited
        # Three times the bound, all of it declared as time spent on a human.
        for _ in range(6):
            await asyncio.sleep(0.2)
            waited += 0.2
        return HostReply(result="approved")

    try:
        result = await asyncio.wait_for(
            k.exec(
                "from tools import Grep\nprint(Grep(pattern='x'))",
                timeout=20,
                on_host_call=slow_but_human,
                host_wait_credit=lambda: waited,
            ),
            timeout=30,
        )
        assert not result.error, result.error
        assert "approved" in result.stdout
    finally:
        await k.stop()


async def test_host_call_bound_never_undercuts_the_declared_cell_timeout() -> None:
    k = SubprocessKernel(KernelConfig(host_call_timeout=5))
    assert k._host_bound(30) == 30      # the cell asked for more
    assert k._host_bound(1) == 5        # the floor holds
    unbounded = SubprocessKernel(KernelConfig(host_call_timeout=0))
    assert unbounded._host_bound(30) == 0


# ─── 4. read_tool_docs: say when the stub is probably still in context ───────


def _docs_session():
    from fluid.codemode.session import CodeModeSession
    from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

    async def _ran(input: dict, ctx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="mcp__browser__browser_navigate",
            description="Open a URL in the browser.",
            input_schema={"type": "object", "properties": {}},
            source="mcp",
        ),
        _ran,
    )
    return CodeModeSession(registry)


def test_repeat_read_tool_docs_says_the_stub_is_probably_still_held() -> None:
    session = _docs_session()
    first = session.render_docs("browser_navigate")
    assert "def browser_navigate(" in first
    assert "already read" not in first

    second = session.render_docs("browser_navigate")
    assert "You already read browser_navigate's docs" in second
    # The stub still comes back — a compaction may genuinely have dropped it.
    assert "def browser_navigate(" in second


def test_run_python_description_does_not_promise_persistence_across_turns() -> None:
    from fluid.codemode.session import _RUN_PYTHON_DESC

    assert "state survives across calls" not in _RUN_PYTHON_DESC
    assert "not a guarantee across TURNS" in _RUN_PYTHON_DESC
    assert "re-import and carry on" in _RUN_PYTHON_DESC
