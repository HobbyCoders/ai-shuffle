"""M5 — SubprocessKernel: persistent namespace, value repr, guards, timeout.

All offline: a bare kernel with a no-op host-call handler runs plain Python in
a persistent child process. These tests exercise the soft-boundary guards
(import allowlist, network block) and the wall-clock timeout kill/restart — not
tool dispatch (that is test_codemode_dispatch).
"""

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply


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


async def test_await_at_cell_level_gets_a_hint() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("rows = await Grep('x')", timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert "'await' outside function" in r.error
        assert "Hint: tools are synchronous functions — call them without await." in r.error
    finally:
        await k.stop()


async def test_unexpected_keyword_argument_gets_a_hint() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "def Grep(pattern):\n"
            "    return pattern\n"
            "Grep('x', output_mode='content')\n",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "unexpected keyword argument" in r.error
        assert 'run read_tool_docs("Grep") to see it.' in r.error
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
    from fluid.codemode.kernel import Kernel

    assert isinstance(SubprocessKernel(), Kernel)


def test_host_reply_defaults_to_no_images() -> None:
    """Images are an opt-in host-side extra; the kernel never sees them."""
    reply = HostReply(result="ok")
    assert reply.images == ()


# ── import forms: the package namespace exposes each tool's callable ────────

_ECHO_STUB = {
    "Echo": (
        "def Echo(text: str) -> str:\n"
        '    """Echo text back."""\n'
        "    return __host_call__('Echo', {'text': text})\n"
    ),
    "Upper": (
        "def Upper(text: str) -> str:\n"
        "    return __host_call__('Upper', {'text': text})\n"
    ),
}


async def _echo_host(name: str, kwargs: dict) -> HostReply:
    text = kwargs.get("text", "")
    return HostReply(result=text.upper() if name == "Upper" else text)


@pytest.mark.parametrize("code", [
    # package form — what models try first
    "from tools import Echo, Upper\nUpper(Echo('hi'))",
    # bare package import, attribute access
    "import tools\ntools.Upper(tools.Echo('hi'))",
    # explicit submodule import: the package attribute is still the callable
    "import tools.Echo\nimport tools.Upper\ntools.Upper(tools.Echo('hi'))",
    # long form (the original spelling) keeps working
    "from tools.Echo import Echo\nfrom tools.Upper import Upper\nUpper(Echo('hi'))",
    # star import
    "from tools import *\nUpper(Echo('hi'))",
])
async def test_tool_import_forms_all_resolve_to_the_callable(code: str) -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB))
        r = await k.exec(code, timeout=10, on_host_call=_echo_host)
        assert r.error is None, r.error
        assert r.value_repr == "'HI'"
        assert r.host_calls == 2
    finally:
        await k.stop()


async def test_tool_package_attribute_is_function_and_module_path_keeps_docs() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB))
        r = await k.exec(
            "import tools\n"
            "from tools import Echo as fn\n"
            "from tools.Echo import Echo\n"
            "(fn is tools.Echo, fn.Echo is Echo, fn.__doc__, callable(fn), tools.Echo('x'))",
            timeout=10, on_host_call=_echo_host,
        )
        assert r.error is None, r.error
        assert r.value_repr == "(True, True, 'Echo text back.', True, 'x')"
    finally:
        await k.stop()


async def test_unknown_tool_in_package_form_points_at_discovery() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB))
        r = await k.exec("from tools import Nope", timeout=10, on_host_call=_echo_host)
        assert r.error is not None
        assert "ImportError" in r.error
        # The old hint named list_tools/search_tools, which are top-level tool
        # calls: a model that followed it reached for tools.list_tools() and
        # burned a dozen calls. Name what the kernel actually offers.
        assert "Hint: no such tool in the kernel VFS" in r.error
        assert "dir(tools)" in r.error
        assert "separate top-level tool calls" in r.error
    finally:
        await k.stop()


async def test_skills_package_exposes_callables_and_plain_modules() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB), skills={
            "greet": '"""Greet."""\ndef greet(n):\n    return "hello " + n\n',
            "consts": '"""Constants only."""\nANSWER = 42\n',
        })
        r = await k.exec(
            "from skills import greet, consts\n"
            "import skills\n"
            "import skills.greet as mod\n"
            "(greet('bob'), skills.greet('al'), consts.ANSWER, skills.consts.ANSWER, mod.__doc__)",
            timeout=10, on_host_call=_echo_host,
        )
        assert r.error is None, r.error
        assert r.value_repr == "('hello bob', 'hello al', 42, 42, 'Greet.')"
    finally:
        await k.stop()


# ── traceback trimming: the model sees only its own frames ──────────────────

async def test_traceback_starts_at_the_cell() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        code = "def boom():\n    return 1 / 0\n\nx = 1\nboom()\n"
        r = await k.exec(code, timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert r.error.startswith("Traceback (most recent call last):")
        assert "_runner" not in r.error and "eval(compile" not in r.error
        assert r.error.count('File "<cell>"') == 2  # the call site and boom()
        assert "ZeroDivisionError" in r.error
        # same when the failing statement is the trailing expression (eval path)
        r2 = await k.exec("y = 2\n{}['missing']", timeout=5, on_host_call=_no_tools)
        assert r2.error is not None
        assert "_runner" not in r2.error and "eval(compile" not in r2.error
        assert 'File "<cell>", line 2' in r2.error and "KeyError" in r2.error
    finally:
        await k.stop()


async def test_syntax_error_has_no_runner_frames() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec("def (:\n", timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert "SyntaxError" in r.error
        assert "Traceback" not in r.error and "_runner" not in r.error
    finally:
        await k.stop()


async def test_bash_nonzero_exit_is_data_in_code_mode(tmp_path) -> None:
    """A failing command returns its output with an exit-code line; it must not
    abort the cell the way a real tool error does."""
    import sys

    from fluid.tools.builtins.bash import handler

    from fluid.tools.registry import ToolCtx

    out = await handler({"command": f'"{sys.executable}" -c "import sys; print(\'no\'); sys.exit(3)"'}, ToolCtx(cwd=str(tmp_path)))
    assert out.is_error and out.soft_error
    assert "[exit code 3]" in out.text and "no" in out.text
