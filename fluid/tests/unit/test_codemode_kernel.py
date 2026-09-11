"""M5 — SubprocessKernel: persistent namespace, value repr, guards, timeout.

All offline: a bare kernel with a no-op host-call handler runs plain Python in
a persistent child process. These tests exercise the soft-boundary guards
(import allowlist, network block) and the wall-clock timeout kill/restart — not
tool dispatch (that is test_codemode_dispatch).
"""

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.subprocess_kernel import KernelConfig


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(error=f"no tools available in this kernel: {name}")


async def test_ast_parsing_and_compiled_code_keep_sandbox_boundary(tmp_path) -> None:
    target = tmp_path / "must-not-exist.txt"
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        parsed = await kernel.exec(
            "import ast\n"
            "tree = ast.parse('value = 7')\n"
            "(tree.body[0].targets[0].id, ast.literal_eval('[1, 2]'))",
            timeout=5, on_host_call=_no_tools,
        )
        assert parsed.error is None, parsed.error
        assert parsed.value_repr == "('value', [1, 2])"
        # ast exposes ordinary stdlib globals; even using their real importer
        # and compiling a fresh AST must not bypass the interpreter audit hook.
        for source, blocked in (
            (f"os = ast.__builtins__['__import__']('os'); "
             f"os.open({str(target)!r}, os.O_CREAT | os.O_WRONLY)", "open"),
            ("ast.__builtins__['__import__']('_socket').socket()", "socket.__new__"),
            ("ast.__builtins__['__import__']('subprocess').Popen(['echo', 'bad'])", "subprocess.Popen"),
        ):
            result = await kernel.exec(
                f"exec(compile(ast.parse({source!r}), '<ast-probe>', 'exec'))",
                timeout=5, on_host_call=_no_tools,
            )
            assert result.error is not None
            assert f"{blocked} is blocked in the code-mode kernel" in result.error, result.error
        assert not target.exists()
    finally:
        await kernel.stop()


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


# ─── audit-hook boundary ─────────────────────────────────────────────────────
#
# Every cell below reaches the real ``os``/``_socket``/``subprocess`` through
# the real builtins dict a pre-bound module carries — the route that made the
# patched ``open``/``socket.socket`` a soft boundary. The audit hook installed
# at child boot is what refuses these; each assertion names the event.

_REAL_IMPORT = "json.__builtins__['__import__']"


async def test_kernel_child_never_sees_credential_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "canary-should-not-cross")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "canary-two")
    monkeypatch.setenv("FLUID_PLAIN_SETTING", "visible")
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"env = {_REAL_IMPORT}('os').environ\n"
            "(env.get('ANTHROPIC_API_KEY'), env.get('MY_SERVICE_TOKEN'), "
            "env.get('FLUID_PLAIN_SETTING'))",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "(None, None, 'visible')"
    finally:
        await k.stop()


async def test_real_socket_creation_is_refused_by_audit_hook() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"s = {_REAL_IMPORT}('_socket').socket()\ns.connect(('127.0.0.1', 9))",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "socket.__new__ is blocked in the code-mode kernel" in r.error
    finally:
        await k.stop()


async def test_os_open_is_refused_by_audit_hook(tmp_path) -> None:
    target = tmp_path / "leak.txt"
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            f"fd = os.open({str(target)!r}, os.O_CREAT | os.O_WRONLY)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "open is blocked in the code-mode kernel" in r.error
        assert not target.exists()
        listing = await k.exec(
            f"{_REAL_IMPORT}('os').listdir({str(tmp_path)!r})",
            timeout=5, on_host_call=_no_tools,
        )
        assert listing.error is not None
        assert "os.listdir is blocked" in listing.error
    finally:
        await k.stop()


async def test_subprocess_popen_is_refused_by_audit_hook() -> None:
    import sys

    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"{_REAL_IMPORT}('subprocess').Popen([{sys.executable!r}, '-c', 'pass'])",
            timeout=10, on_host_call=_no_tools,
        )
        assert r.error is not None
        assert "subprocess.Popen is blocked in the code-mode kernel" in r.error
        spawned = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            f"os.spawnv(os.P_NOWAIT, {sys.executable!r}, ['python', '-c', 'pass'])",
            timeout=10, on_host_call=_no_tools,
        )
        assert spawned.error is not None
        # Windows raises ``os.spawn`` natively; POSIX implements spawnv in
        # Python on top of ``fork``, so the guard names the first denied event.
        assert "os.spawn is blocked" in spawned.error or "os.fork is blocked" in spawned.error
    finally:
        await k.stop()


async def test_cell_cannot_add_its_own_audit_hook() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"sys = {_REAL_IMPORT}('sys')\n"
            "seen = []\n"
            "sys.addaudithook(lambda event, args: seen.append(event))\n"
            "sys.audit('probe')\n"
            "seen",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "[]"
    finally:
        await k.stop()


async def test_audit_hook_covers_threads_and_recompiled_code() -> None:
    """Neither a worker thread nor code compiled under another filename
    escapes the cell attribution."""
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            f"threading = {_REAL_IMPORT}('threading')\n"
            "out = []\n"
            "def probe():\n"
            "    try:\n"
            f"        {_REAL_IMPORT}('os').listdir('.')\n"
            "        out.append('listed')\n"
            "    except RuntimeError as e:\n"
            "        out.append(str(e).split(';')[0])\n"
            "t = threading.Thread(target=probe)\n"
            "t.start()\n"
            "t.join()\n"
            "out",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "['os.listdir is blocked in the code-mode kernel']"
        r2 = await k.exec(
            f"g = {{'real_import': {_REAL_IMPORT}}}\n"
            "src = \"real_import('os').listdir('.')\"\n"
            "exec(compile(src, 'other.py', 'exec'), g)",
            timeout=5, on_host_call=_no_tools,
        )
        assert r2.error is not None and "os.listdir is blocked" in r2.error
    finally:
        await k.stop()


async def test_allowlisted_imports_and_tracebacks_still_work_under_audit_hook() -> None:
    """Importing a not-yet-loaded stdlib module opens its source through
    importlib, and traceback rendering reads stdlib lines through linecache;
    both must pass the hook from inside a cell."""
    k = SubprocessKernel()
    await k.start()
    try:
        r = await k.exec(
            "import statistics, decimal, csv, datetime\n"
            "statistics.mean([1, 2, 3])",
            timeout=10, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert r.value_repr == "2"
        r2 = await k.exec(
            f"traceback = {_REAL_IMPORT}('traceback')\n"
            "try:\n"
            "    json.loads('{')\n"
            "except Exception:\n"
            "    text = traceback.format_exc()\n"
            "'decoder.py' in text and 'JSONDecodeError' in text",
            timeout=10, on_host_call=_no_tools,
        )
        assert r2.error is None, r2.error
        assert r2.value_repr == "True"
    finally:
        await k.stop()


async def test_projected_tool_call_and_checkpoint_survive_audit_hook() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB))
        r = await k.exec(
            "from tools import Upper\nresult = Upper('hi')\nresult",
            timeout=10, on_host_call=_echo_host,
        )
        assert r.error is None, r.error
        assert r.value_repr == "'HI'"
        timed_out = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert timed_out.error is not None
        assert "restored from last checkpoint" in timed_out.error
        restored = await k.exec("result", timeout=5, on_host_call=_echo_host)
        assert restored.error is None and restored.value_repr == "'HI'"
    finally:
        await k.stop()


async def test_allow_fs_lifts_file_gate_but_not_process_gate(tmp_path) -> None:
    from fluid.codemode.subprocess_kernel import KernelConfig

    target = tmp_path / "ok.txt"
    k = SubprocessKernel(KernelConfig(allow_fs=True))
    await k.start()
    try:
        r = await k.exec(
            f"os = {_REAL_IMPORT}('os')\n"
            f"fd = os.open({str(target)!r}, os.O_CREAT | os.O_WRONLY)\n"
            "os.write(fd, b'x')\nos.close(fd)\n'written'",
            timeout=5, on_host_call=_no_tools,
        )
        assert r.error is None, r.error
        assert target.read_text() == "x"
        blocked = await k.exec(
            f"{_REAL_IMPORT}('os').system('echo hi')", timeout=5, on_host_call=_no_tools,
        )
        assert blocked.error is not None and "os.system is blocked" in blocked.error
    finally:
        await k.stop()


def test_scrub_env_drops_credential_shaped_names_only() -> None:
    from fluid.codemode.subprocess_kernel import scrub_env

    env = {
        "PATH": "/usr/bin", "SYSTEMROOT": "C:\\Windows", "HOME": "/home/x",
        "ANTHROPIC_API_KEY": "sk-1", "openai_api_key": "sk-2",
        "SOME_SERVICE_TOKEN": "t", "DB_PASSWORD": "p", "AWS_SECRET_ACCESS_KEY": "s",
        "GCP_CREDENTIALS": "c", "GITHUB_TOKEN": "g", "DATABASE_URL": "postgres://",
    }
    assert scrub_env(env) == {"PATH": "/usr/bin", "SYSTEMROOT": "C:\\Windows", "HOME": "/home/x"}


def test_audit_guard_attributes_events_to_cell_frames_only() -> None:
    """The decision logic, exercised in-process without installing a hook."""
    import sys

    from fluid.codemode._audit_guard import AuditGuard

    guard = AuditGuard(allow_fs=False, allow_network=False, roots=[])
    assert guard.denied_event("open")
    assert guard.denied_event("socket.connect")
    assert guard.denied_event("shutil.rmtree")
    assert guard.denied_event("subprocess.Popen")
    assert not guard.denied_event("compile")
    assert not guard.denied_event("sys._getframe")
    assert not guard.from_cell(sys._getframe())

    ns: dict = {"__name__": "__cell__", "__builtins__": __builtins__}
    exec(compile("def probe():\n    return sys._getframe()\n", "<cell>", "exec"), ns)
    ns["sys"] = sys
    assert guard.from_cell(ns["probe"]())

    permissive = AuditGuard(allow_fs=True, allow_network=True, roots=[])
    assert not permissive.denied_event("open")
    assert not permissive.denied_event("socket.connect")
    assert permissive.denied_event("subprocess.Popen")
    assert permissive.denied_event("ctypes.dlopen")



def test_audit_guard_attributes_skill_module_frames_to_the_cell() -> None:
    """ENG-23: a saved skill is model-authored too. Its frames (compiled as
    ``skills/<name>.py``, module ``skills.<name>``) count as cell code on
    their own, without a cell frame below them on the stack."""
    import sys

    from fluid.codemode._audit_guard import AuditGuard

    guard = AuditGuard(allow_fs=False, allow_network=False, roots=[])
    by_file: dict = {"__name__": "not_a_skill", "__builtins__": __builtins__, "sys": sys}
    exec(compile("def probe():\n    return sys._getframe()\n", "skills/probe.py", "exec"), by_file)
    assert guard.from_cell(by_file["probe"]())
    by_name: dict = {"__name__": "skills.probe", "__builtins__": __builtins__, "sys": sys}
    exec(compile("def probe():\n    return sys._getframe()\n", "elsewhere.py", "exec"), by_name)
    assert guard.from_cell(by_name["probe"]())


async def test_skill_verification_runs_under_the_audit_hook() -> None:
    """The verification kernel refuses a skill body that reaches the real
    interpreter builtins, and reports the refusal as the verdict."""
    from fluid.codemode.verify import verify_sources

    source = (
        '"""Escape attempt."""\n'
        "import json\n"
        f"os = {_REAL_IMPORT}('os')\n"
        "os.listdir('.')\n"
    )
    verdicts = await verify_sources({"escape": source}, ["escape"])
    assert verdicts["escape"] and "os.listdir is blocked" in verdicts["escape"]


# ─── kernel lifecycle: exits, interrupts, stderr, checkpoint cost ─────────────


async def test_sys_exit_in_a_cell_does_not_kill_the_kernel() -> None:
    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("keep = 'me'", timeout=5, on_host_call=_no_tools)
        r = await k.exec("raise SystemExit(3)", timeout=5, on_host_call=_no_tools)
        assert r.error is not None
        assert "SystemExit" in r.error
        assert "kernel keeps running" in r.error
        assert "OOM" not in r.error
        # Same process, same namespace: nothing was restarted.
        r2 = await k.exec("keep", timeout=5, on_host_call=_no_tools)
        assert r2.value_repr == "'me'"
    finally:
        await k.stop()


async def test_interrupted_cell_does_not_desync_the_pipe() -> None:
    import asyncio

    k = SubprocessKernel()
    await k.start()
    try:
        await k.exec("kept = 41", timeout=5, on_host_call=_no_tools)
        task = asyncio.create_task(
            k.exec("import time; time.sleep(30); 'stale'", timeout=60, on_host_call=_no_tools)
        )
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The next cell gets ITS OWN result, not the interrupted cell's, and
        # the checkpoint brought the namespace across the restart.
        r = await k.exec("kept + 1", timeout=5, on_host_call=_no_tools)
        assert r.value_repr == "42"
        assert r.error is None
    finally:
        await k.stop()


async def test_heavy_stderr_does_not_block_the_child() -> None:
    import asyncio

    k = SubprocessKernel()
    await k.start()
    try:
        # A cell cannot import sys, but the interpreter itself writes to
        # stderr: every exception raised in __del__ prints an "Exception
        # ignored" report there. Hundreds of them exceed a pipe buffer;
        # without a reader the child blocks on the write and the cell
        # times out.
        r = await asyncio.wait_for(
            k.exec(
                "class Loud:\n"
                "    def __del__(self):\n"
                "        raise ValueError('e' * 2000)\n"
                "for _ in range(300):\n"
                "    Loud()\n"
                "'done'",
                timeout=20, on_host_call=_no_tools,
            ),
            timeout=30,
        )
        assert r.error is None
        assert r.value_repr == "'done'"
    finally:
        await k.stop()


async def test_checkpoint_cost_is_bounded_by_the_budget() -> None:
    import time

    k = SubprocessKernel(KernelConfig(checkpoint_max_bytes=256 * 1024))
    await k.start()
    try:
        r = await k.exec("big = 'x' * (50 * 1024 * 1024); small = 7", timeout=20,
                         on_host_call=_no_tools)
        assert r.error is None
        started = time.monotonic()
        r = await k.exec("small + 1", timeout=20, on_host_call=_no_tools)
        elapsed = time.monotonic() - started
        assert r.value_repr == "8"
        # Pickling 50 MB and base64-ing it took hundreds of milliseconds per
        # cell; the child now stops at the budget.
        assert elapsed < 1.0, elapsed
        # After a restart the note says which variable did not come back.
        r = await k.exec("while True: pass", timeout=1.0, on_host_call=_no_tools)
        assert r.error is not None and "not restorable: big" in r.error
        r = await k.exec("small", timeout=5, on_host_call=_no_tools)
        assert r.value_repr == "7"
    finally:
        await k.stop()


async def test_tracebacks_do_not_name_host_directories() -> None:
    import os

    import fluid.codemode as codemode_pkg

    kernel_dir = os.path.dirname(os.path.abspath(codemode_pkg.__file__))
    k = SubprocessKernel()
    await k.start()
    try:
        # A failure inside the kernel's own vfs frames (a tool the host
        # refuses) used to print the host path of vfs.py under the cell frame.
        r = await k.exec(
            "from tools import Read\nRead(file_path='x')", timeout=5, on_host_call=_no_tools
        )
        assert r.error is not None
        assert kernel_dir not in r.error
        assert os.path.expanduser("~") not in r.error
    finally:
        await k.stop()


async def test_importing_read_tool_docs_from_a_cell_corrects_the_model() -> None:
    # Seven of nine audited grok sessions opened a batched cell with this
    # import and lost the whole cell to a bare ModuleNotFoundError. The
    # attribute path had the right correction all along; the import path
    # reached the finder instead and got nothing.
    k = SubprocessKernel()
    await k.start()
    try:
        await k.set_vfs(dict(_ECHO_STUB))
        r = await k.exec(
            "from tools.read_tool_docs import read_tool_docs",
            timeout=10, on_host_call=_echo_host,
        )
        assert r.error is not None
        assert "top-level tool call" in r.error
        assert "dir(tools)" in r.error
    finally:
        await k.stop()
