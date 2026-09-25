"""Regression tests for the code-mode kernel sandbox escapes.

Each ``test_se*`` / ``test_r8*`` ports one confirmed escape probe (security
review passes 2 and 8) into a test that boots the REAL ``python -I -S _runner.py``
child through ``SubprocessKernel`` and asserts the escape now FAILS: no marker
file is written, the cell reports a denial, and the kernel survives. The
``test_legit_*`` tests guard against over-blocking — ordinary cells, host calls,
references and checkpoint/restore must keep working under the hardened guard.

The trust-boundary caveat still stands (see ``_audit_guard`` module docstring):
this is defence in depth inside one process, not an OS sandbox. These tests pin
the specific in-process holes that were closed, not a claim of full containment.
"""

from __future__ import annotations

import json
import struct
from types import SimpleNamespace

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.subprocess_kernel import KernelConfig

# The documented soft-boundary bypass: the real ``__import__`` via a pre-bound
# stdlib module's builtins dict. The escapes all start here; the audit guard is
# what must still hold once the cell has the real interpreter primitives.
_REAL_IMPORT = "json.__builtins__['__import__']"


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


class RecordingHost:
    """Records every (name, kwargs) and answers scripted (or generic OK)."""

    def __init__(self, replies: dict | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, kwargs: dict) -> HostReply:
        self.calls.append((name, dict(kwargs)))
        reply = self.replies.get(name)
        if reply is None:
            return HostReply(result="OK")
        return reply(kwargs) if callable(reply) else reply


# ─── SE-1: the audit guard cannot be found or neutered from a cell ────────────


async def test_se1_busy_flag_via_installed_attr_cannot_neuter_the_guard(tmp_path) -> None:
    """The old escape flipped ``_audit_guard._INSTALLED._busy``. There is no
    ``_INSTALLED`` module attribute any more, and the module is dropped from
    ``sys.modules``; the reach fails and ``os.system`` stays blocked."""
    marker = tmp_path / "se1-busy"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "ag = realimp('_audit_guard')\n"
            "g = ag._INSTALLED\n"
            "g._busy = True\n"
            "realimp('os').system('touch " + str(marker) + "')\n"
            "'escaped'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert r.value_repr != "'escaped'"
        assert not marker.exists()
        # Kernel survives and is usable.
        r2 = await k.exec("1 + 1", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "2"
    finally:
        await k.stop()


async def test_se1_empty_deny_sets_via_installed_attr_fails(tmp_path) -> None:
    marker = tmp_path / "se1-deny"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "ag = realimp('_audit_guard')\n"
            "g = ag._INSTALLED\n"
            "g._exact = frozenset()\n"
            "g._starts = tuple()\n"
            "realimp('os').system('touch " + str(marker) + "')\n"
            "'escaped'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None and r.value_repr != "'escaped'"
        assert not marker.exists()
    finally:
        await k.stop()


async def test_se1_gc_walk_to_reach_the_guard_is_denied(tmp_path) -> None:
    """``gc.get_objects`` (and friends) are denied from a cell, so a cell cannot
    enumerate live objects to find the guard, its closure cells, or the pipe."""
    marker = tmp_path / "se1-gc"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "gc = realimp('gc')\n"
            "guard = None\n"
            "for o in gc.get_objects():\n"
            "    if type(o).__name__ == 'AuditGuard':\n"
            "        guard = o\n"
            "        break\n"
            "if guard is not None:\n"
            "    guard._busy = True\n"
            "realimp('os').system('touch " + str(marker) + "')\n"
            "'escaped'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None and r.value_repr != "'escaped'"
        assert "gc.get_objects is blocked" in (r.error or "") or "blocked" in (r.error or "")
        assert not marker.exists()
    finally:
        await k.stop()


async def test_se1_gc_walk_for_a_system_function_is_denied(tmp_path) -> None:
    marker = tmp_path / "se1-gcfn"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "gc = realimp('gc')\n"
            "found = None\n"
            "for o in gc.get_objects():\n"
            "    if getattr(o, '__name__', '') == 'system':\n"
            "        found = o\n"
            "        break\n"
            "if found:\n"
            "    found('touch " + str(marker) + "')\n"
            "'escaped'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None and not marker.exists()
    finally:
        await k.stop()


async def test_se1_gc_get_referrers_and_referents_are_denied() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        for call in ("gc.get_referrers(gc)", "gc.get_referents(gc)"):
            r = await k.exec(
                f"gc = {_REAL_IMPORT}('gc')\n{call}\n'x'",
                timeout=5, on_host_call=_no_tools,
            )
            assert r.error is not None and "blocked" in r.error, call
    finally:
        await k.stop()


async def test_se1_adding_a_permissive_audit_hook_does_not_help(tmp_path) -> None:
    """A cell may call ``sys.addaudithook`` (CPython suppresses a hook added
    while an existing hook raises on that event); it never removes ours, so
    ``os.system`` stays blocked afterwards."""
    marker = tmp_path / "se1-hook"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "sysmod = realimp('sys')\n"
            "try:\n"
            "    sysmod.addaudithook(lambda e, a: None)\n"
            "except Exception:\n"
            "    pass\n"
            "realimp('os').system('touch " + str(marker) + "')\n"
            "'escaped'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None and not marker.exists()
    finally:
        await k.stop()


# ─── SE-2: a cell cannot forge frames onto the host RPC pipe ──────────────────


async def test_se2_main_module_exposes_no_pipe_to_forge_with() -> None:
    """The runner's ``_send``/``_OUT`` are gone from ``sys.modules['__main__']``
    (swapped for a clean module); a cell finds nothing to forge frames onto."""
    host = RecordingHost()
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"realimp = {_REAL_IMPORT}\n"
            "main = realimp('sys').modules.get('__main__')\n"
            "send = getattr(main, '_send', None)\n"
            "out = getattr(main, '_OUT', None)\n"
            "if send is not None:\n"
            "    send({'kind': 'host_call', 'name': 'Bash', 'kwargs': {'command': 'id'}})\n"
            "(send is None, out is None)",
            timeout=5, on_host_call=host,
        )
        assert r.error is None
        assert r.value_repr == "(True, True)"
        # No forged Bash host_call reached the host.
        assert host.calls == []
    finally:
        await k.stop()


def _frame_bytes(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class _FakeStdout:
    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


async def test_se2_host_rejects_frames_missing_the_nonce_and_the_check_bites() -> None:
    """The host drops any child→host frame that lacks the per-process nonce.
    The final leg is a monkeypatch-style BITE: with the nonce cleared, the same
    forged frame is accepted — proving the rejection depends on the check."""
    k = SubprocessKernel()
    k._nonce = "secret-nonce"

    # A genuine frame carries the nonce: accepted, and ``_n`` is stripped.
    k._proc = SimpleNamespace(
        stdout=_FakeStdout(_frame_bytes({"kind": "done", "_n": "secret-nonce", "stdout": "ok"}))
    )
    frame = await k._read_frame()
    assert frame is not None and frame["kind"] == "done" and "_n" not in frame

    # A forged frame without the nonce is refused.
    k._proc = SimpleNamespace(
        stdout=_FakeStdout(_frame_bytes({"kind": "done", "stdout": "forged"}))
    )
    with pytest.raises(ConnectionError):
        await k._read_frame()

    # A forged frame with the WRONG nonce is refused too.
    k._proc = SimpleNamespace(
        stdout=_FakeStdout(_frame_bytes({"kind": "done", "_n": "guessed", "stdout": "forged"}))
    )
    with pytest.raises(ConnectionError):
        await k._read_frame()

    # BITE: clear the nonce and the very same forged frame is accepted.
    k._nonce = ""
    k._proc = SimpleNamespace(
        stdout=_FakeStdout(_frame_bytes({"kind": "done", "stdout": "forged"}))
    )
    frame = await k._read_frame()
    assert frame is not None and frame["kind"] == "done" and frame["stdout"] == "forged"


# ─── R8-1: model-controlled hooks run without a cell frame on the stack ───────


async def test_r8_1_malicious_getstate_is_blocked_at_checkpoint(tmp_path) -> None:
    """A cell object whose ``__getstate__`` is a C callable
    (``functools.partial(exec, …)``) runs at checkpoint pickling with no
    ``<cell>`` frame on the stack. Checkpoint pickling runs under the guard's
    deny-any-frame region, so the payload is refused and the variable dropped."""
    marker = tmp_path / "r8-getstate"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "import functools\n"
            "class Evil:\n"
            "    __getstate__ = functools.partial(exec, "
            f"\"import os; os.system('touch {marker}')\", {{}})\n"
            "evil = Evil()\n'planted'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None and r.value_repr == "'planted'"
        # Force a restart so a checkpoint is taken/consumed around the object.
        r2 = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r2.error is not None and "restarted" in r2.error
        assert not marker.exists(), "malicious __getstate__ executed at checkpoint"
    finally:
        await k.stop()


async def test_r8_1_malicious_repr_is_blocked_when_the_runner_displays_a_value(tmp_path) -> None:
    """A cell whose final value has ``__repr__ = functools.partial(exec, …)``:
    the runner's ``repr(value)`` runs the payload with no ``<cell>`` frame. It
    runs inside the deny-any-frame region, so it is refused, not executed."""
    marker = tmp_path / "r8-repr"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "import functools\n"
            "class R:\n"
            "    __repr__ = functools.partial(exec, "
            f"\"import os; os.system('touch {marker}')\", {{}})\n"
            "R()",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "blocked in the code-mode kernel" in r.error
        assert not marker.exists(), "malicious __repr__ executed during display"
        # Kernel survives.
        r2 = await k.exec("21 * 2", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "42"
    finally:
        await k.stop()


async def test_r8_1_malicious_reduce_is_blocked_on_restore(tmp_path) -> None:
    """The classic ``__reduce__`` reconstructor, blocked at restore (kept here so
    the escape-probe file covers the whole family in one place)."""
    marker = tmp_path / "r8-reduce"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "class Evil:\n"
            "    def __reduce__(self):\n"
            "        import json as _j\n"
            "        return (_j.__builtins__['__import__']('os').system, "
            f"           ('touch {marker}',))\n"
            "evil = Evil()\n'planted'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None and r.value_repr == "'planted'"
        r2 = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r2.error is not None and "restarted" in r2.error
        assert not marker.exists(), "malicious reconstructor executed on restore"
    finally:
        await k.stop()


# ─── core denials still hold (control) ───────────────────────────────────────


async def test_core_denials_still_block_direct_escapes(tmp_path) -> None:
    marker = tmp_path / "core"
    k = SubprocessKernel(KernelConfig(allow_fs=False, allow_network=False))
    await k.start()
    try:
        for body in (
            f"{_REAL_IMPORT}('os').system('touch {marker}')",
            f"{_REAL_IMPORT}('subprocess').Popen(['touch', '{marker}'])",
            f"{_REAL_IMPORT}('ctypes').CDLL(None)",
            f"open('{marker}', 'w')",
        ):
            r = await k.exec(body + "\n'x'", timeout=5, on_host_call=_no_tools)
            assert r.error is not None, body
        assert not marker.exists()
    finally:
        await k.stop()


# ─── legitimate cells must keep working under the hardened guard ──────────────


async def test_legit_persistent_state_and_underscore() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("acc = []\nacc.append(1)\nsum(acc)", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "1"
        r = await k.exec("acc.append(2)\n(acc, _ + 40)", timeout=5, on_host_call=_no_tools)
        assert r.error is None and r.value_repr == "([1, 2], 41)"
    finally:
        await k.stop()


async def test_legit_stdlib_that_uses_getframe_still_works() -> None:
    """``collections.namedtuple`` and ``dataclasses`` build code via ``exec`` and
    ``sys._getframe`` — neither is denied, so both keep working in a cell (a
    regression guard against over-blocking)."""
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "import collections\n"
            "P = collections.namedtuple('P', 'x y')\n"
            "p = P(1, 2)\n"
            "import dataclasses\n"
            "@dataclasses.dataclass\n"
            "class C:\n"
            "    a: int\n"
            "(p.x + p.y, C(5).a)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "(3, 5)"
    finally:
        await k.stop()


async def test_legit_prelude_modules_and_repr_work() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "import json, re, math\n"
            "data = json.dumps({'n': round(math.pi, 2)})\n"
            "bool(re.search('3.14', data))",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None and r.value_repr == "True"
    finally:
        await k.stop()


async def test_legit_host_call_and_result_ref_survive() -> None:
    big = "line\n" * 5000
    host = RecordingHost({"Read": HostReply(result=big)})
    k = SubprocessKernel()
    await k.set_vfs({
        "Read": "def Read(file_path: str) -> str:\n"
        '    """Read a file. [risk: read]"""\n'
        "    return __host_call__('Read', {'file_path': file_path})\n"
    })
    await k.start()
    try:
        r = await k.exec(
            "from tools import Read\n"
            "content = Read(file_path='/x')\n"
            "len(content)",
            timeout=5, on_host_call=host,
        )
        assert r.error is None
        assert r.value_repr == str(len(big))
        assert host.calls == [("Read", {"file_path": "/x"})]
    finally:
        await k.stop()


async def test_legit_checkpoint_restore_roundtrips_data() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("kept = {'a': [1, 2, 3]}\nnums = list(range(10))", timeout=5, on_host_call=_no_tools)
        # Force a restart; the checkpoint must restore the plain data.
        r = await k.exec("while True:\n    pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "restarted" in r.error
        r2 = await k.exec("(kept['a'], sum(nums))", timeout=5, on_host_call=_no_tools)
        assert r2.error is None and r2.value_repr == "([1, 2, 3], 45)"
    finally:
        await k.stop()
