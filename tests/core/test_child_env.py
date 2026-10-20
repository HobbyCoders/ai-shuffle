"""Worker interpreter settings belong to the worker, not project subprocesses."""
import json
import subprocess
import sys

import pytest

from app.core.hook_executor import hook_base_env
from app.core.process_tree import child_env
from app.core.terminals import shell_environment


@pytest.mark.parametrize("surface", ["monitor", "terminal", "hook"])
def test_child_env_surfaces_drop_worker_python_settings(monkeypatch, tmp_path, surface):
    names = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    for name in names:
        monkeypatch.setenv(name, "worker-runtime-must-not-leak")
    # Monitor shells use process_tree.child_env; terminals have a stricter
    # credential policy; plugin hooks use hook_base_env.
    env = {"monitor": child_env, "terminal": lambda: shell_environment("bash", "/bin/bash"),
           "hook": hook_base_env}[surface]()
    probe = "import os, json; print(json.dumps({k: os.environ.get(k) for k in " + repr(names) + "}))"
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict.fromkeys(names)


def test_child_env_git_clone_does_not_reintroduce_worker_python_env(monkeypatch, tmp_path):
    from unittest.mock import Mock
    from app.core.git_service import GitService

    names = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    for name in names:
        monkeypatch.setenv(name, "worker-runtime")
    service = GitService()
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "test refusal"))
    monkeypatch.setattr(service, "_run_git", run)
    service.clone_repo(str(tmp_path), "https://example.com/project.git")
    assert not names & run.call_args.kwargs["env"].keys()


def test_child_env_monitor_explicit_project_overlay_survives(monkeypatch):
    names = {"PYTHONPATH": "project-python", "PYTHONHOME": "project-home", "VIRTUAL_ENV": "project-venv"}
    for name in names:
        monkeypatch.setenv(name, "worker-runtime")
    assert names.items() <= child_env(names).items()
