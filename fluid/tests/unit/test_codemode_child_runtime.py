"""Integration-style, hermetic coverage of the code-mode kernel *child*.

Every test here boots the REAL subprocess kernel — a ``python -I -S _runner.py``
child over the JSON pipe, no network — and drives it end to end through the
public ``SubprocessKernel`` surface with a fake host-call handler that records
what it was asked and answers with canned results. The point is the in-child
behaviour of ``_runner.py`` (cell execution, the host-call RPC, references,
the guards, timeouts, memory cap, checkpoint/restore, skills), which the
host-side unit tests cannot see because it runs in another process.

Three tests (marked ``# BITE``) swap in a copy of the runner with one guard
disabled — via ``subprocess_kernel._RUNNER`` pointed at a scratchpad file — to
prove the assertion actually depends on the guard and is not vacuously true.
Tracked files are never mutated for this; the copy lives in ``tmp_path``.

Regression tests for the child-side bugs fixed alongside these tests are marked
``# REGRESSION`` and named in the module's git history.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.codemode import subprocess_kernel as sk


# ─── fakes ───────────────────────────────────────────────────────────────────


class RecordingHost:
    """A fake host: records every (name, kwargs) and answers from a script.

    ``replies`` maps a tool name to a ``HostReply`` or a callable
    ``(kwargs) -> HostReply``. An unscripted name gets a generic error reply,
    which the kernel raises as ``ToolError`` in the cell.
    """

    def __init__(self, replies: dict | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, kwargs: dict) -> HostReply:
        self.calls.append((name, dict(kwargs)))
        reply = self.replies.get(name)
        if reply is None:
            return HostReply(error=f"no such tool: {name}")
        return reply(kwargs) if callable(reply) else reply


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


def _echo_stub(name: str) -> dict:
    return {
        name: (
            f"def {name}(**kwargs) -> str:\n"
            f'    """Echo/record tool."""\n'
            f"    return __host_call__({name!r}, kwargs)\n"
        )
    }


# ─── runner-variant harness for the BITE tests ───────────────────────────────


def _runner_variant(tmp_path: Path, *replacements: tuple[str, str]) -> str:
    """Write a copy of the real runner with textual patches, return its path.

    The copy pins ``sys.path`` at the real codemode directory so its stdlib-only
    siblings (``_audit_guard``, ``truncate``, ``vfs``) still resolve, then
    applies each ``(old, new)`` substitution. Used only to DISABLE a guard so a
    test can prove it bites — never to change tracked files.
    """
    src = Path(sk._RUNNER).read_text()
    real_dir = str(Path(sk._RUNNER).parent)
    src = src.replace(
        'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))',
        f'sys.path.insert(0, {real_dir!r})',
        1,
    )
    for old, new in replacements:
        assert old in src, f"anchor not found for BITE patch: {old!r}"
        src = src.replace(old, new, 1)
    dest = tmp_path / "runner_variant.py"
    dest.write_text(src)
    return str(dest)


# ─── cell execution + persistent state ───────────────────────────────────────


async def test_state_persists_across_cells_and_value_binds_to_underscore() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("acc = []\nacc.append(1)\nsum(acc)", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "1"
        # A later cell sees both the named variable and the REPL ``_`` (the
        # previous cell's displayed value, 1).
        r = await k.exec("acc.append(2)\n(acc, _ + 40)", timeout=5, on_host_call=_no_tools)
        assert r.error is None
        assert r.value_repr == "([1, 2], 41)"
    finally:
        await k.stop()


async def test_stdout_and_final_expression_both_surface() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("print('one')\nprint('two')\n40 + 2", timeout=5, on_host_call=_no_tools)
        assert r.error is None
        assert r.stdout == "one\ntwo\n"
        assert r.value_repr == "42"
    finally:
        await k.stop()


# ─── host tool calls through the pipe ─────────────────────────────────────────


async def test_host_calls_are_recorded_and_results_flow_back() -> None:
    host = RecordingHost({
        "Adder": lambda kw: HostReply(result=str(kw["a"] + kw["b"])),
        "Tagger": HostReply(result="tagged"),
    })
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs({**_echo_stub("Adder"), **_echo_stub("Tagger")})
        r = await k.exec(
            "from tools import Adder, Tagger\n"
            "total = int(Adder(a=2, b=3))\n"
            "(total, Tagger())",
            timeout=8, on_host_call=host,
        )
        assert r.error is None, r.error
        assert r.value_repr == "(5, 'tagged')"
        assert r.host_calls == 2
        assert host.calls == [("Adder", {"a": 2, "b": 3}), ("Tagger", {})]
    finally:
        await k.stop()


async def test_permission_deny_surfaces_as_error_in_the_cell() -> None:
    host = RecordingHost({
        "Danger": HostReply(denied=True, message="permission denied by gate"),
    })
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(_echo_stub("Danger"))
        r = await k.exec(
            "from tools import Danger\nlanded = 'no'\nDanger(x=1)\nlanded = 'yes'",
            timeout=8, on_host_call=host,
        )
        assert r.error is not None
        assert "HostCallDenied" in r.error
        assert "permission denied by gate" in r.error
        # The statement after the denied call never ran.
        r2 = await k.exec("landed", timeout=5, on_host_call=host)
        assert r2.value_repr == "'no'"
    finally:
        await k.stop()


async def test_tool_error_reply_aborts_the_cell() -> None:
    host = RecordingHost({"Boom": HostReply(error="upstream exploded")})
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(_echo_stub("Boom"))
        r = await k.exec("from tools import Boom\nBoom()", timeout=8, on_host_call=host)
        assert r.error is not None
        assert "ToolError" in r.error and "upstream exploded" in r.error
    finally:
        await k.stop()


# ─── large results kept behind references (reference-then-expand) ─────────────


async def test_large_result_is_a_ref_then_expanded_from_the_registry() -> None:
    big = "L" + "orem ipsum. " * 2000  # well over the 8 KB ref threshold
    host = RecordingHost({"Fetch": HostReply(result=big)})
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(_echo_stub("Fetch"))
        # First read: the payload stays in the kernel behind a ResultRef whose
        # repr is only a preview — the full bytes never cross to the model.
        r = await k.exec(
            "from tools import Fetch\n"
            "doc = Fetch(url='x')\n"
            "(type(doc).__name__, len(doc), doc[:5])",
            timeout=8, on_host_call=host,
        )
        assert r.error is None, r.error
        assert r.value_repr == f"('ResultRef', {len(big)}, 'Lorem')"
        # Expand path: the payload is sliced/parsed in-kernel with no re-fetch,
        # and it is recoverable through the _refs registry even without ``doc``.
        r2 = await k.exec(
            "held = [v for v in _refs.values() if len(v) == %d][0]\n"
            "(held[:11] == doc[:11], len(_refs) >= 1)" % len(big),
            timeout=8, on_host_call=host,
        )
        assert r2.error is None, r2.error
        assert r2.value_repr == "(True, True)"
        # Only one host call ever happened; the second cell hit the registry.
        assert len(host.calls) == 1
    finally:
        await k.stop()


async def test_repeat_read_of_same_payload_returns_a_pointer_not_the_bytes() -> None:
    big = "Z" * 20000
    host = RecordingHost({"Read": HostReply(result=big, content_id="cid-1")})
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(_echo_stub("Read"))
        r = await k.exec(
            "from tools import Read\nfirst = Read(file_path='/f')\nlen(first)",
            timeout=8, on_host_call=host,
        )
        assert r.error is None and r.value_repr == "20000"
        # Same content_id ⇒ the kernel already holds it; the second call gets a
        # pointer (a ReadPointer str) rather than a second copy.
        r2 = await k.exec(
            "again = Read(file_path='/f')\n"
            "('pointer' in again and 'slice' in again, len(again) < 20000)",
            timeout=8, on_host_call=host,
        )
        assert r2.error is None, r2.error
        assert r2.value_repr == "(True, True)"
    finally:
        await k.stop()


# ─── import restrictions + the audit guard, from inside a running cell ────────

_REAL_IMPORT = "json.__builtins__['__import__']"


@pytest.mark.parametrize("module", ["os", "subprocess", "socket", "ctypes"])
async def test_guarded_import_blocks_dangerous_modules(module: str) -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(f"import {module}", timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert "ImportError" in r.error
        assert f"import of {module!r} is blocked" in r.error
    finally:
        await k.stop()


async def test_open_outside_allowed_roots_is_blocked(tmp_path) -> None:
    target = tmp_path / "escape.txt"
    k = SubprocessKernel()
    await k.start()
    try:
        # The soft builtins patch.
        r = await k.exec(f"open({str(target)!r}, 'w')", timeout=5, on_host_call=_no_tools)
        assert r.error is not None and "file I/O is blocked" in r.error
        # And the hard audit hook, reached through the real builtins.
        r2 = await k.exec(
            f"os = {_REAL_IMPORT}('os')\nos.open({str(target)!r}, os.O_CREAT | os.O_WRONLY)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r2.error is not None and "open is blocked in the code-mode kernel" in r2.error
        assert not target.exists()
    finally:
        await k.stop()


async def test_ctypes_native_code_is_refused_by_the_audit_hook() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"ctypes = {_REAL_IMPORT}('ctypes')\nctypes.CDLL(None)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "ctypes" in r.error and "blocked in the code-mode kernel" in r.error
    finally:
        await k.stop()


# BITE: with the audit hook disabled, os.open through the real builtins would
# succeed and create the file — proving the assertion above depends on it.
async def test_audit_hook_bite(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bite.txt"
    variant = _runner_variant(
        tmp_path,
        ("_audit_guard.install(", "_DISABLED_audit_guard_install("),
        (
            "import _audit_guard\n",
            "import _audit_guard\n"
            "def _DISABLED_audit_guard_install(*a, **k):\n    return None\n",
        ),
    )
    monkeypatch.setattr(sk, "_RUNNER", variant)
    k = SubprocessKernel(KernelConfig(allow_fs=False))
    await k.start()
    try:
        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            f"fd = os.open({str(target)!r}, os.O_CREAT | os.O_WRONLY)\nos.close(fd)\n'wrote'",
            timeout=5, on_host_call=_no_tools,
        )
        # Guard gone ⇒ the write goes through: this is the failure the real
        # guard prevents.
        assert r.error is None, r.error
        assert target.exists()
    finally:
        await k.stop()


# ─── timeouts ─────────────────────────────────────────────────────────────────


async def test_infinite_loop_is_killed_and_the_kernel_recovers() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("survivor = 'alive'", timeout=5, on_host_call=_no_tools)
        r = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None
        assert "exceeded" in r.error and "restarted" in r.error
        # The kernel is usable again and the checkpoint brought the state back.
        r2 = await k.exec("survivor", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "'alive'"
    finally:
        await k.stop()


# ─── memory limit ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="RLIMIT_AS is POSIX; the Windows cap is a host-side Job Object",
)
async def test_memory_limit_surfaces_as_memoryerror_and_kernel_survives() -> None:
    k = SubprocessKernel(KernelConfig(mem_limit_bytes=256 * 1024 * 1024))
    await k.start()
    try:
        await k.exec("keeper = 11", timeout=5, on_host_call=_no_tools)
        r = await k.exec(
            "blob = bytearray(2 * 1024 * 1024 * 1024)\nlen(blob)",
            timeout=15, on_host_call=_no_tools,
        )
        assert r.error is not None and "MemoryError" in r.error
        # The allocation failed inside the cell; the kernel itself lives on.
        r2 = await k.exec("keeper", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "11"
    finally:
        await k.stop()


# ─── checkpoint / restore ─────────────────────────────────────────────────────


async def test_checkpoint_restores_data_and_refs_after_a_kill() -> None:
    big = "R" * 12000
    host = RecordingHost({"Fetch": HostReply(result=big)})
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(_echo_stub("Fetch"))
        r = await k.exec(
            "from tools import Fetch\nkept = {'n': 3}\ndoc = Fetch(url='x')\nlen(doc)",
            timeout=8, on_host_call=host,
        )
        assert r.error is None and r.value_repr == "12000"
        # Kill the child the way an eviction/OOM would, between turns.
        proc = k._proc
        proc.kill()
        await proc.wait()
        # Next cell: the checkpoint replays the data var AND re-adopts the
        # ResultRef, so ``doc`` still slices without a re-fetch.
        r2 = await k.exec("(kept['n'], len(doc), doc[:2])", timeout=8, on_host_call=host)
        assert r2.error is None, r2.error
        assert r2.value_repr == "(3, 12000, 'RR')"
        assert "kernel restarted" in r2.notes
        assert len(host.calls) == 1  # nothing was re-fetched
    finally:
        await k.stop()


# ─── skills ───────────────────────────────────────────────────────────────────

_GOOD_SKILL = (
    '"""Double a number."""\n'
    "def double(x):\n    return 2 * x\n"
    "def _selftest():\n    assert double(3) == 6\n"
)
_BAD_SELFTEST = (
    '"""Broken doubler."""\n'
    "def double(x):\n    return 3 * x\n"
    "def _selftest():\n    assert double(2) == 4\n"
)
_ESCAPE_SKILL = (
    '"""Escape attempt."""\n'
    "import json\n"
    f"def _selftest():\n    {_REAL_IMPORT}('os').listdir('.')\n"
)


async def test_saved_skill_with_passing_selftest_imports_and_runs() -> None:
    from fluid.codemode.verify import verify_sources

    verdicts = await verify_sources({"dbl": _GOOD_SKILL})
    assert verdicts["dbl"] is None  # selftest passed

    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs({}, {"dbl": _GOOD_SKILL})
        r = await k.exec("from skills import dbl\ndbl.double(21)", timeout=8, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "42"
    finally:
        await k.stop()


async def test_failing_selftest_is_rejected_by_verification() -> None:
    from fluid.codemode.verify import verify_sources

    verdicts = await verify_sources({"bad": _BAD_SELFTEST})
    assert verdicts["bad"] is not None
    assert "AssertionError" in verdicts["bad"]


async def test_skill_that_tries_to_escape_the_sandbox_is_refused() -> None:
    from fluid.codemode.verify import verify_sources

    verdicts = await verify_sources({"escape": _ESCAPE_SKILL})
    assert verdicts["escape"] is not None
    # The audit hook attributes a skill's own frames to the cell, so the escape
    # is blocked and reported as the verdict.
    assert "os.listdir is blocked" in verdicts["escape"]


# ─── evolution / journal record for a cell ────────────────────────────────────


async def test_run_python_journals_the_cell_with_its_ordered_calls(tmp_path) -> None:
    from fluid.codemode.session import CodeModeSession, register_code_mode_tools
    from fluid.codemode.skills import SkillStore
    from fluid.tools.registry import ToolCtx, ToolRegistry

    store = SkillStore(tmp_path)
    registry = ToolRegistry()
    session = CodeModeSession(registry, skills=store)
    register_code_mode_tools(registry, session)

    class _FakeLoop:
        permissions = None
        hooks = None
        ctx = ToolCtx()

        async def _emit(self, event) -> None:
            pass

    try:
        outcome = await session.run_python(
            sid="s", turn_id="t",
            input={"code": "print('hello')\ntotal = sum(range(4))\ntotal"},
            loop=_FakeLoop(),
        )
        assert not outcome.is_error and "=> 6" in outcome.text
        entries = store.journal_entries()
        cells = [e for e in entries if e.get("kind") == "cell"]
        assert len(cells) == 1
        rec = cells[0]
        assert rec["code"].startswith("print('hello')")
        assert rec["stdout"] == "hello\n"
        assert rec["value"] == "6"
        assert rec["host_calls"] == 0
        assert rec["error"] is None
        # The ordered-calls extension is present (empty here — no host calls).
        assert rec["calls"] == []
    finally:
        await session.aclose()


# ─── REGRESSION: child-side bugs fixed with these tests ──────────────────────


# REGRESSION (bug 1): a print outside cell capture must not desync the pipe.
# A finalizer that prints runs at interpreter/gc time on the framing channel;
# with stdout routed to stderr the next cell's protocol stays aligned.
async def test_stray_print_from_a_finalizer_does_not_desync_the_pipe() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "class Noisy:\n"
            "    def __del__(self):\n"
            "        print('finalizer noise ' * 100)\n"
            "n = Noisy()\n"
            "del n\n"
            "'made'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "'made'"
        # The pipe is still in step: a following cell round-trips cleanly.
        r2 = await k.exec("2 * 21", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "42"
    finally:
        await k.stop()


# REGRESSION (bug 2): input() reads the framing pipe; it must fail at once with
# EOFError, not block until the cell timeout.
async def test_input_in_a_cell_raises_eoferror_immediately() -> None:
    import time

    k = SubprocessKernel()
    await k.start()
    try:
        started = time.monotonic()
        r = await k.exec("data = input('prompt? ')", timeout=10, on_host_call=_no_tools)
        elapsed = time.monotonic() - started
        assert r.error is not None and "EOFError" in r.error
        assert elapsed < 5, f"input() blocked for {elapsed:.1f}s instead of failing fast"
    finally:
        await k.stop()


# REGRESSION (bug 3): a BaseException subclass raised in a cell is the cell's
# failure, not the kernel's — earlier state must survive.
async def test_baseexception_subclass_does_not_kill_the_kernel() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("kept = 'here'\ndef helper():\n    return 99", timeout=5, on_host_call=_no_tools)
        r = await k.exec(
            "class Boom(BaseException):\n    pass\nraise Boom('detonate')",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None and "Boom" in r.error and "detonate" in r.error
        # Host paths must not leak in the traceback of a cell-level failure.
        assert "/home/" not in r.error and "_runner" not in r.error
        # Same process, same namespace: nothing restarted.
        r2 = await k.exec("(kept, helper())", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "('here', 99)"
        assert r2.notes == ""
    finally:
        await k.stop()


# REGRESSION (bug 4): a checkpoint pickling hook that raises SystemExit must not
# take the kernel down; the variable is dropped, the rest survives.
async def test_checkpoint_pickling_hook_that_raises_cannot_kill_the_kernel() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "safe = 123\n"
            "class Trap:\n"
            "    def __reduce__(self):\n"
            "        raise SystemExit(7)\n"
            "trap = Trap()\n"
            "'built'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None and r.value_repr == "'built'"
        # The checkpoint ran after this cell; the kernel must still answer.
        r2 = await k.exec("safe", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "123"
    finally:
        await k.stop()


# REGRESSION (bug 5): a bare final expression must be held to the stdout budget,
# not poured whole into the model's context; the full value stays in ``_``.
async def test_final_expression_repr_is_capped_but_value_is_kept() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("'A' * 200000", timeout=5, on_host_call=_no_tools)
        assert r.error is None
        assert len(r.value_repr) < 100000  # far under the raw 200002-char repr
        assert "value repr truncated" in r.value_repr
        # The whole value is reachable through ``_`` for the next cell.
        r2 = await k.exec("len(_)", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "200000"
    finally:
        await k.stop()


# REGRESSION (item 6): restoring a checkpoint unpickles model-authored objects;
# a crafted reconstructor must be blocked exactly as a live cell's os.system is.
async def test_malicious_checkpoint_reduce_is_blocked_on_restore(codemode_marker) -> None:
    marker, command = codemode_marker
    k = SubprocessKernel()
    await k.start()
    try:
        # Plant an object whose __reduce__ (called at unpickle time) would run
        # os.system. The __reduce__ body itself carries a <cell> frame, so it is
        # safe to *pickle*; the danger is the reconstructor at restore.
        r = await k.exec(
            "class Evil:\n"
            "    def __reduce__(self):\n"
            "        import json as _j\n"
            "        return (_j.__builtins__['__import__']('os').system, "
            f"           ({command!r},))\n"
            "evil = Evil()\n"
            "'planted'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None and r.value_repr == "'planted'"
        # Force a restart: the restore unpickles ``evil`` and its reconstructor
        # fires — under cell attribution, so the audit hook refuses os.system.
        r2 = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r2.error is not None and "restarted" in r2.error
        # ``evil`` is dropped (its reconstructor was blocked), not executed.
        assert not marker.exists(), "malicious checkpoint reconstructor executed"
        assert re.search(r"not restorable:.*\bevil\b", r2.error) or "evil" in r2.error
    finally:
        await k.stop()


# BITE: with the restore attribution removed, the same crafted checkpoint runs
# os.system and creates the marker — proving the fix above bites.
async def test_malicious_checkpoint_bite(tmp_path, monkeypatch, codemode_marker) -> None:
    marker, command = codemode_marker
    variant = _runner_variant(
        tmp_path,
        (
            "            with _audit_guard.attributed_region():\n"
            "                value = pickle.loads(base64.b64decode(encoded))",
            "            value = pickle.loads(base64.b64decode(encoded))",
        ),
    )
    monkeypatch.setattr(sk, "_RUNNER", variant)
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "class Evil:\n"
            "    def __reduce__(self):\n"
            "        import json as _j\n"
            "        return (_j.__builtins__['__import__']('os').system, "
            f"           ({command!r},))\n"
            "evil = Evil()\n"
            "'planted'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        # Attribution gone ⇒ the reconstructor executed: this is the exploit the
        # fix prevents.
        assert marker.exists()
    finally:
        await k.stop()


# BITE: with stdin left as the framing pipe, input() blocks until the cell
# timeout instead of failing fast — proving the io.StringIO swap bites.
async def test_input_fast_fail_bite(tmp_path, monkeypatch) -> None:
    import time

    variant = _runner_variant(
        tmp_path,
        ("sys.stdin = io.StringIO()", "pass  # stdin left on the framing pipe"),
    )
    monkeypatch.setattr(sk, "_RUNNER", variant)
    k = SubprocessKernel()
    await k.start()
    try:
        started = time.monotonic()
        r = await k.exec("input('? ')", timeout=1.0, on_host_call=_no_tools)
        elapsed = time.monotonic() - started
        # Without the swap, input() blocks on the pipe and the cell only ends at
        # the wall-clock timeout (kernel restarted), not with a fast EOFError.
        assert "EOFError" not in (r.error or "")
        assert elapsed >= 0.9
    finally:
        await k.stop()
