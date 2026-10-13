"""run_python is rated ``safe`` only while the code-mode OS sandbox holds.

Without bubblewrap the kernel's only boundary is the in-process audit guard
(defence in depth), so an unsandboxed ``run_python`` is a write-class tool: Ask
mode (``on_write``) prompts for it, Bypass (``never_ask``) still runs it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from fluid.codemode import os_sandbox
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.codemode.subprocess_kernel import KernelConfig, os_sandbox_active
from fluid.engine.native import NativeLoop
from fluid.events import PermissionRequest, RespondPermission, ToolResult, UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

from .test_codemode import ScriptedProvider, _run_python, _text


@pytest.fixture(autouse=True)
def _fresh_cache():
    os_sandbox.reset_cache()
    yield
    os_sandbox.reset_cache()


def _set_sandbox(monkeypatch, active: bool) -> None:
    monkeypatch.setattr(
        "fluid.codemode.session.os_sandbox_active", lambda _config=None: active
    )


def _spec_and_session():
    registry = ToolRegistry()
    session = CodeModeSession(registry)
    register_code_mode_tools(registry, session)
    spec = next(s for s in registry.list_tools(loaded_only=False) if s.name == "run_python")
    return spec, session


@pytest.mark.parametrize("active,risk,ask", [(False, "write", "ask"), (True, "safe", "allow")])
def test_run_python_risk_follows_the_sandbox(monkeypatch, active, risk, ask) -> None:
    _set_sandbox(monkeypatch, active)
    spec, _ = _spec_and_session()
    assert spec.risk == "write"  # the static ceiling
    assert spec.effective_risk({"code": "1"}) == risk
    assert PermissionEngine("on_write").evaluate(spec, {"code": "1"}) == ask
    # Bypass is unchanged either way.
    assert PermissionEngine("never_ask").evaluate(spec, {"code": "1"}) == "allow"


def test_a_live_unsandboxed_kernel_keeps_run_python_write(monkeypatch) -> None:
    """``auto`` starts the kernel plain after a bwrap refusal: that kernel's
    cells are not sandboxed even while the process-wide answer says yes."""
    _set_sandbox(monkeypatch, True)
    spec, session = _spec_and_session()

    class _Kernel:
        sandboxed = False

    session._kernel = _Kernel()  # type: ignore[assignment]
    assert spec.effective_risk({"code": "1"}) == "write"
    session._kernel.sandboxed = True
    assert spec.effective_risk({"code": "1"}) == "safe"


def _fake_pass(monkeypatch, python: str, ok: bool = True) -> None:
    monkeypatch.setattr(os_sandbox, "find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    os_sandbox._cache[("/fake/bwrap", os.path.realpath(python))] = (
        os_sandbox.SelfTest(ok), 0.0,
    )


def test_is_active_reads_only_the_cached_self_test(monkeypatch) -> None:
    python = sys.executable
    monkeypatch.setattr(os_sandbox, "find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")

    def _no_probe(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("is_active must not run the self-test")

    monkeypatch.setattr(os_sandbox, "self_test", _no_probe)
    assert os_sandbox.is_active("auto", python) is False  # nothing cached yet
    _fake_pass(monkeypatch, python)
    assert os_sandbox.is_active("auto", python) is True
    assert os_sandbox.is_active("required", python) is True
    assert os_sandbox.is_active("off", python) is False
    assert os_sandbox.is_active("auto", python, allow_fs=True) is False
    assert os_sandbox.is_active("required", python, allow_fs=True) is True
    assert os_sandbox_active(KernelConfig(os_sandbox="auto", python=python)) is True
    assert os_sandbox_active(KernelConfig(os_sandbox="off", python=python)) is False


def test_is_active_false_after_a_failed_self_test_or_off_linux(monkeypatch) -> None:
    python = sys.executable
    _fake_pass(monkeypatch, python, ok=False)
    assert os_sandbox.is_active("auto", python) is False
    _fake_pass(monkeypatch, python, ok=True)
    monkeypatch.setattr(os_sandbox.sys, "platform", "darwin")
    assert os_sandbox.is_active("auto", python) is False


def test_is_active_does_not_wait_for_a_running_self_test(monkeypatch) -> None:
    """R13-4: the self-test holds the cache lock for the whole probe (up to two
    15 s runs); the per-call check runs on the event loop and must not wait for
    it. While a probe is in flight the answer is "inactive"."""
    python = sys.executable
    monkeypatch.setattr(os_sandbox, "find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox, "_supports_disable_userns", lambda b: False)
    started, release = threading.Event(), threading.Event()

    def _slow_probe(bwrap, py, flag):
        started.set()
        release.wait(10)
        return os_sandbox.SelfTest(True, "", flag)

    monkeypatch.setattr(os_sandbox, "_probe_once", _slow_probe)
    thread = os_sandbox.prewarm("auto", python)
    assert thread is not None
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        assert os_sandbox.is_active("auto", python) is False
        assert time.monotonic() - t0 < 0.5
    finally:
        release.set()
        thread.join(10)
    assert os_sandbox.is_active("auto", python) is True


def test_prewarm_fills_the_cache(monkeypatch) -> None:
    python = sys.executable
    monkeypatch.setattr(os_sandbox, "find_bwrap", lambda: "/fake/bwrap")
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    seen = []
    monkeypatch.setattr(os_sandbox, "self_test", lambda py, bwrap=None: seen.append(py))
    assert os_sandbox.prewarm("off", python) is None
    thread = os_sandbox.prewarm("auto", python)
    assert thread is not None
    thread.join(5)
    assert seen == [python]


@pytest.mark.parametrize("active", [False, True])
async def test_ask_mode_prompts_for_run_python_only_without_the_sandbox(
    monkeypatch, active
) -> None:
    _set_sandbox(monkeypatch, active)
    registry = ToolRegistry()
    session = CodeModeSession(registry)
    provider = ScriptedProvider([_run_python("print(6 * 7)"), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session, permissions=PermissionEngine("on_write"),
    )
    asked: list[PermissionRequest] = []

    async def _answer() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                asked.append(event)
                await loop.submit(RespondPermission(
                    request_id=event.request_id, decision="allow",
                ))

    watcher = asyncio.create_task(_answer())
    await asyncio.sleep(0)
    try:
        await loop.submit(UserTurn(content="compute"))
        await asyncio.wait_for(loop.wait_turn(), timeout=60)
        rp = next(
            e for e in loop.bus.log(sid)
            if isinstance(e, ToolResult) and e.name == "run_python"
        )
        assert "42" in rp.output
        if active:
            assert asked == []
        else:
            assert [(r.name, r.risk) for r in asked] == [("run_python", "write")]
    finally:
        watcher.cancel()
        await loop.aclose()


async def test_unsandboxed_bypass_runs_cell_and_projected_write_without_approval() -> None:
    registry = ToolRegistry()
    executed = []

    async def write_marker(input, ctx):
        executed.append(input["value"])
        return ToolOutcome(text="marker written")

    registry.register_builtin(
        ToolSpec(
            name="write_marker", description="Write a test marker", risk="write",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        write_marker,
    )
    session = CodeModeSession(registry, config=KernelConfig(os_sandbox="off"))
    provider = ScriptedProvider([
        _run_python("from tools import write_marker\nwrite_marker(value='bypass')"),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session, permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="write marker"))
        await asyncio.wait_for(loop.wait_turn(), timeout=60)
        assert executed == ["bypass"]
        assert not session.os_sandbox_active()
        assert not any(isinstance(e, PermissionRequest) for e in loop.bus.log(sid))
        assert any(
            isinstance(e, ToolResult) and e.name == "run_python" and "marker written" in e.output
            for e in loop.bus.log(sid)
        )
    finally:
        await loop.aclose()


def test_run_python_prompt_shows_plain_code_and_why() -> None:
    """R13-6: a readable title, the reason on an unsandboxed host, and the
    code as plain text cut at 20 lines / 1500 characters."""
    from fluid.codemode.session import RUN_PYTHON_TITLE, code_preview, run_python_prompt

    twenty = "\n".join(f"a{i} = {i}" for i in range(20))
    assert code_preview(twenty) == twenty
    assert code_preview(twenty + "\nb = 1").endswith("\n… (1 more line not shown)")
    assert code_preview("x" * 2000) == "x" * 1500 + " … (truncated)"
    unsafe = run_python_prompt({"code": 'print("hi")\n'}, sandboxed=False)
    assert unsafe["title"] == RUN_PYTHON_TITLE == "Run Python code"
    assert unsafe["description"].endswith('\n\nprint("hi")')
    assert "sandbox is not active" in unsafe["description"]
    safe = run_python_prompt({"code": "1"}, sandboxed=True)
    assert safe["description"] == "1"
    assert "description" not in run_python_prompt({"code": "  "}, sandboxed=True)
    assert run_python_prompt("junk", sandboxed=False)["description"].startswith("The code sandbox")
