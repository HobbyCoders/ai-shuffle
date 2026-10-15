"""Real Bash/Monitor workflow smoke, without claiming a service-uid boundary.

The root fixture separately proves the uid split. This smoke also runs as an
ordinary Linux user, so missing root privileges cannot hide workflow regressions.
"""
import asyncio
import os
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from fluid.tools import agent_runner
from fluid.tools.builtins.bash import handler
from fluid.tools.registry import ToolCtx


@pytest.mark.skipif(sys.platform != "linux", reason="Docker runner requires Linux")
@pytest.mark.parametrize("tool", ["bash", "monitor"])
@pytest.mark.asyncio
async def test_isolated_agent_project_workflows(tmp_path, monkeypatch, tool):
    if os.getuid() == 0 or set(os.getgroups()) - {os.getgid()}:
        pytest.skip("runner requires a non-root account without supplementary groups")
    for command in ("git", "npm", "node"):
        if not shutil.which(command):
            pytest.skip(f"workflow smoke requires {command}")
    from app.core import process_ledger
    from app.mcp import builtin_tools

    monkeypatch.setattr(process_ledger, "record_process", lambda *a, **kw: False)
    monkeypatch.setenv(agent_runner.FLAG, "on")
    monkeypatch.setenv(agent_runner.ROOTS, str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    script = Path(__file__).resolve().parents[2] / "scripts/ci/agent_isolation_workload.py"
    command = shlex.join([sys.executable, str(script)])
    if tool == "bash":
        outcome = await handler({"command": command}, ToolCtx(cwd=str(tmp_path)))
        assert not outcome.is_error, outcome.text
        output = outcome.text
    else:
        task = await builtin_tools._spawn_background(command, str(tmp_path), None)
        try:
            await asyncio.wait_for(task._reader_task, 60)
            assert task.process.returncode == 0
            output = "\n".join(task.lines)
        finally:
            builtin_tools._background_tasks.pop(task.task_id, None)
    assert "ISOLATION_WORKFLOW_OK" in output, output


@pytest.mark.parametrize("failure", [None, "missing-file", "readable-directory", "db-unavailable", "db-authenticated"])
def test_image_probe_does_not_mistake_missing_resources_for_denial(failure):
    """Test the acceptance assertions, not the OS/database boundary itself."""
    import builtins
    import io
    import json
    from types import SimpleNamespace
    from scripts.ci.agent_isolation_probe import CHILD

    accesses = []
    connections = []

    def denied(path, *args):
        accesses.append(path)
        if failure == "missing-file":
            raise FileNotFoundError(path)
        if failure == "readable-directory" and path.endswith("postgres"):
            return []
        raise PermissionError(path)

    def connect(**kwargs):
        connections.append(kwargs)
        if failure == "db-unavailable":
            raise RuntimeError("Connection refused")
        if failure == "db-authenticated":
            return SimpleNamespace(close=lambda: None)
        raise RuntimeError("no password supplied" if len(connections) == 1 else "Permission denied")

    info = {"host": "127.0.0.1", "port": 5432, "user": "fixture", "database": "fixture", "socket": "/tmp/fixture"}
    modules = {"json": json, "os": SimpleNamespace(listdir=denied),
               "sys": SimpleNamespace(argv=["probe", json.dumps(info)]),
               "psycopg2": SimpleNamespace(connect=connect, OperationalError=RuntimeError)}
    output = io.StringIO()
    namespace = {"__builtins__": {**vars(builtins), "open": denied,
                 "__import__": lambda name, *a: modules[name],
                 "print": lambda *a: print(*a, file=output)}}
    if failure is None:
        exec(compile(CHILD, "isolation-child", "exec"), namespace)
        assert len(accesses) == 6 and len(set(accesses)) == 6
        assert len(connections) == 2
        assert all("password" not in options and options["passfile"] == "/dev/null" for options in connections)
        assert "TCP_AUTH_AND_ADMIN_SOCKET_DENIED" in output.getvalue()
    else:
        with pytest.raises((AssertionError, FileNotFoundError)):
            exec(compile(CHILD, "isolation-child", "exec"), namespace)
        assert "TCP_AUTH_AND_ADMIN_SOCKET_DENIED" not in output.getvalue()
