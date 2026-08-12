"""M5 — SubprocessKernel: persistent namespace, value repr, guards, timeout.

All offline: a bare kernel with a no-op host-call handler runs plain Python in
a persistent child process. These tests exercise the soft-boundary guards
(import allowlist, network block) and the wall-clock timeout kill/restart — not
tool dispatch (that is test_codemode_dispatch).
"""

import pytest

from loom.codemode import SubprocessKernel
from loom.codemode.kernel import HostReply


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


async def test_namespace_persists_across_exec() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r1 = await k.exec("x = 40 + 2", timeout=5, on_host_call=_no_tools)
        assert r1.error is None
        r2 = await k.exec("x", timeout=5, on_host_call=_no_tools)
        assert r2.value_repr == "42"
    finally:
        await k.stop()


async def test_value_repr_and_stdout() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("print('hi')\n6 * 7", timeout=5, on_host_call=_no_tools)
        assert r.stdout == "hi\n"
        assert r.value_repr == "42"
        # a statement (no trailing expression) yields an empty value_repr
        r2 = await k.exec("y = 1", timeout=5, on_host_call=_no_tools)
        assert r2.value_repr == ""
    finally:
        await k.stop()


async def test_exception_returns_traceback() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("1 / 0", timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert "ZeroDivisionError" in r.error
        assert "Traceback" in r.error
    finally:
        await k.stop()


async def test_disallowed_import_blocked() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("import os", timeout=5, on_host_call=_no_tools)
        assert r.error is not None and "blocked" in r.error
        # an allowlisted module still imports fine
        ok = await k.exec("import math\nmath.factorial(5)", timeout=5, on_host_call=_no_tools)
        assert ok.error is None and ok.value_repr == "120"
    finally:
        await k.stop()


async def test_network_blocked_by_default() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("import socket", timeout=5, on_host_call=_no_tools)
        # socket is off the allowlist, so even the import is refused — no network.
        assert r.error is not None and "blocked" in r.error
    finally:
        await k.stop()


async def test_timeout_kills_and_recovers() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "exceeded" in r.error
        # kernel auto-restarts; a fresh exec works (namespace was lost, as noted)
        r2 = await k.exec("2 + 3", timeout=5, on_host_call=_no_tools)
        assert r2.value_repr == "5"
    finally:
        await k.stop()


async def test_kernel_satisfies_protocol() -> None:
    from loom.codemode.kernel import Kernel

    assert isinstance(SubprocessKernel(), Kernel)
