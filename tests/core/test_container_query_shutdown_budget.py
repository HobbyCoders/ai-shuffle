"""Use the actual Python supervisor's larger stop budget, not the desktop default."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.core.test_query_shutdown_budget import run_query_shutdown_probe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX container supervisor")
@pytest.mark.parametrize("mode", ["finite-15", "over-budget"])
def test_container_query_uses_the_real_supervisor_budget(tmp_path, mode):
    run_query_shutdown_probe(tmp_path, mode, 30, "/api/v1/query", container_supervised=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX container supervisor")
def test_start_app_passes_the_same_budget_used_for_stopping(monkeypatch):
    from docker import container_runtime as runtime

    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    env = {"SHUFFLE_SUPERVISOR_STOP_SECONDS": "10", "EXAMPLE": "unchanged"}
    runtime.start_app(["python", "-m", "app.main"], None, env)
    assert popen.call_args.kwargs["env"]["SHUFFLE_SUPERVISOR_STOP_SECONDS"] == str(runtime.APP_STOP_SECONDS)
    assert env["SHUFFLE_SUPERVISOR_STOP_SECONDS"] == "10"  # Never mutate the launch template.
    stops = Mock()
    monkeypatch.setattr(runtime, "stop_group", stops)
    database = SimpleNamespace(close=Mock())
    runtime.shutdown("app", "chrome", database)
    assert stops.call_args_list[0].args == ("app", runtime.APP_STOP_SECONDS)
    database.close.assert_called_once()
