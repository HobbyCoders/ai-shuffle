"""Audited harness contracts are capability-aware and executable."""
import re
import sys
import types

import pytest

from app.core.agent_provider_models import parent_subagent_guidance
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions


def test_read_only_code_mode_omits_unavailable_workflows():
    text = ha._code_mode_section(frozenset({"Read"}))
    for absent in ("Task", "Monitor", "Grep(", "SessionSearch", "Skill", "save_skill"):
        assert absent not in text
    assert "MissingFile" in text and "independent calls" in text
    assert "permission-gated" in text and "validation barriers abort" in text
    assert len(text) < len(ha.CODE_MODE_SECTION)


def test_optional_workflow_blocks_require_their_tools():
    text = ha._code_mode_section(frozenset({"Monitor"}))
    assert "Monitor" in text and "Task" not in text
    text = ha._code_mode_section(frozenset({"Task"}))
    assert 'Task(mode="background")' in text and "Monitor" not in text
    assert "end your turn" in text


def test_parent_guidance_obeys_profile_surface():
    text = parent_subagent_guidance(task_available=False, monitor_available=True,
                                   swarm_available=False, browser_available=False)
    assert "Task" not in text and "Swarm" not in text
    assert "Monitor" in text and "browser_navigate" not in text
    assert "Parking is not a final assignment report" in text
    assert not parent_subagent_guidance(task_available=False, monitor_available=False,
                                       swarm_available=False, browser_available=False)


def test_worker_without_task_cannot_advertise_nested_delegation(monkeypatch):
    from app.core import task_dispatch as td
    monkeypatch.setattr(td, "_is_worker_session", lambda sid: True)
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 1))
    text = td.subagent_system_prompt("worker", task_available=False)
    assert "Further delegation is unavailable" in text
    assert "Task roster" not in text
    assert "Top-level session orchestration is unavailable" in text


def test_write_without_browser_is_a_gap_not_a_bypass(tmp_path):
    options = SessionOptions(model="fake", cwd=str(tmp_path))
    options.tool_allowlist = ["Read", "Write"]
    compiled = ha.HarnessAdapter._compile_prompt_sections("AGENT", None, options, native=True)
    assert "browser verification surface is unavailable" in compiled.text
    assert "browser_navigate" not in compiled.text
    assert "watched it work in the running app" in compiled.text
    assert "drive the producing path too" in compiled.text
    assert "instead of calling it verified" in compiled.text


@pytest.mark.parametrize("prefix", ["", "mcp__browser__"])
def test_browser_contract_accepts_leaf_or_wire_names(prefix):
    tools = frozenset(prefix + name for name in (
        "browser_navigate", "browser_snapshot", "browser_eval", "browser_act",
        "browser_screenshot", "browser_resize",
    ))
    assert ha._verification_section(tools) == ha.VERIFICATION_BLOCK


def test_partial_browser_surface_does_not_prescribe_missing_interactions():
    text = ha._verification_section(frozenset({"browser_navigate", "browser_snapshot", "browser_eval"}))
    assert "browser_act" not in text and "browser_screenshot" not in text
    assert "unavailable" in text and "do not claim it verified" in text


@pytest.mark.parametrize("paths", [
    ["/repo/src/a.py", "/repo/src/b.py"],
    [r"C:\repo\src\a.py", r"C:\repo\src\b.py"],
])
def test_advertised_search_example_parses_deduplicates_and_windows(monkeypatch, capsys, paths):
    calls = []
    def fake_read(**kwargs):
        calls.append(kwargs)
        return "evidence"
    def fake_grep(**kwargs):
        return "searched via rg under /repo/src\n" + "\n".join([
            paths[0] + ":12:usage=1", paths[0] + ":80:usage=2", paths[1] + ":2:usage=3",
        ])
    for name, fn in (("Read", fake_read), ("Grep", fake_grep)):
        module = types.ModuleType("tools." + name)
        setattr(module, name, fn)
        monkeypatch.setitem(sys.modules, "tools." + name, module)
    code = re.search(r"```python\n(.*?)```", ha._CODE_MODE_SEARCH_EXAMPLE, re.S).group(1)
    exec(code, {})
    assert calls == [
        {"file_path": paths[0], "offset": 4, "limit": 40},
        {"file_path": paths[1], "offset": 1, "limit": 40},
    ]


async def test_advertised_search_example_drives_real_grep_and_read(tmp_path):
    from fluid.codemode import HostBridge, Projection, SubprocessKernel
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins import grep, read
    from fluid.tools.registry import ToolCtx, ToolRegistry
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("usage = 1\nusage = 2\n")
    registry = ToolRegistry()
    reads = []
    async def tracked_read(input, ctx):
        reads.append(input["file_path"])
        return await read.handler(input, ctx)
    registry.register_builtin(read.SPEC, tracked_read)
    registry.register_builtin(grep.SPEC, grep.handler)
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        code = re.search(r"```python\n(.*?)```", ha._CODE_MODE_SEARCH_EXAMPLE, re.S).group(1)
        result = await kernel.exec(code.replace("/repo/src", str(tmp_path)), timeout=8,
                                   on_host_call=bridge.on_host_call)
        assert result.error is None, result.error
        assert len(reads) == len(set(reads)) == 2
        assert "usage = 1" in result.stdout
    finally:
        await kernel.stop()

def test_node_bootstrap_distinguishes_mobile_and_lockless_packages(tmp_path):
    from app.core import environment as env
    (tmp_path / "app").mkdir()
    (tmp_path / "app/preview.py").write_text("")
    (tmp_path / "fluid").mkdir()
    for name in ("frontend", "mobile", "custom"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "package.json").write_text("{}")
    (tmp_path / "frontend/package-lock.json").write_text("{}")
    # Custom is at the package root, a supported package directory.
    (tmp_path / "package.json").write_text("{}")
    facts = "\n".join(env._node_dependency_facts(tmp_path))
    assert "MISSING in frontend" in facts and "npm ci" in facts
    assert "Mobile: dependencies MISSING" in facts and "npm run deps" in facts
    assert "do not run npm ci in mobile" in facts and "SAME checkout" in facts
    assert "no lockfile — use the package's documented setup" in facts


@pytest.mark.parametrize("returncode", [0, 1])
def test_pytest_console_script_is_verified_not_confused_with_interpreter(tmp_path, monkeypatch, returncode):
    import shutil
    import subprocess
    from app.core import environment as env
    calls = []
    monkeypatch.setattr(env, "pytest_interpreter", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda name: "/test/bin/pytest" if name == "pytest" else None)
    def probe(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(subprocess, "run", probe)
    facts = "\n".join(env._python_facts(tmp_path, True))
    assert calls == [["/test/bin/pytest", "--version"]]
    if returncode == 0:
        assert "verified pytest executable `/test/bin/pytest`" in facts
        assert "no install is needed" in facts
        assert "-m pytest" not in facts  # Console script is not an interpreter.
    else:
        assert "no interpreter or executable with pytest was verified" in facts
        assert "project venv" in facts


def test_adapter_snapshot_uses_real_store_and_current_checkout(tmp_path, monkeypatch):
    from app.core import fluid_memory
    from fluid.codemode.skills import SkillStore
    store = SkillStore(tmp_path / "memory")
    old = store.save_memory("fix/old has .venv installed.", provenance="session")
    current = store.save_memory("fix/current has .venv installed.", provenance="session")
    routed = []
    def route(sid, cwd):
        routed.append((sid, cwd))
        return store, None
    monkeypatch.setattr(fluid_memory, "session_memory", route)
    adapter = object.__new__(ha.HarnessAdapter)
    adapter._options = SessionOptions(model="fake", cwd=str(tmp_path / "fix-current"))
    adapter._resolve_app_session_id = lambda: "owner-scoped-session"
    snapshot = adapter._memory_snapshot()
    assert old.text not in snapshot and current.text in snapshot
    assert routed == [("owner-scoped-session", adapter._options.cwd)]
    assert len(store.memories()) == 2
