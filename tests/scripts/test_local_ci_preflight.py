"""Fail before security scans, suites, or GitHub statuses on a bad Python."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import local_ci


@pytest.mark.parametrize("module", local_ci.DEV_MODULES)
def test_missing_dev_import_refuses_before_status(monkeypatch, capsys, module):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    calls = []
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, json.dumps(["cannot import " + module]), ""))
    monkeypatch.setattr(local_ci, "attest", lambda *a: calls.append("status"))
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: calls.append("security"))
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: calls.append("suites"))
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    assert local_ci.main(["full", "--attest"]) == 1
    assert calls == []
    output = capsys.readouterr().err
    assert module in output and "requirements.txt + requirements-dev.txt" in output


@pytest.mark.parametrize("entry", ["../outside", "/outside"])
def test_external_pythonpath_refused(monkeypatch, entry):
    monkeypatch.setenv("PYTHONPATH", entry)
    with pytest.raises(local_ci.Failure, match="PYTHONPATH points outside"):
        local_ci.python_preflight(sys.executable)


@pytest.mark.parametrize("kind", ["clean", "missing", "docker", "claude_agent_sdk"])
def test_real_interpreter_probe(tmp_path, monkeypatch, kind):
    # Real imports, isolated from whatever packages are installed on the test host.
    (tmp_path / "docker").mkdir()
    (tmp_path / "required_for_test.py").write_text("")
    if kind == "docker":
        outside = tmp_path.parent / (tmp_path.name + "-external")
        outside.mkdir()
        (outside / "docker.py").write_text("")
        # sitecustomize models a site-packages package shadowing a namespace.
        (tmp_path / "sitecustomize.py").write_text(f"import sys; sys.path.append({str(outside)!r})")
    elif kind == "claude_agent_sdk":
        (tmp_path / "claude_agent_sdk.py").write_text("")
    elif kind == "missing":
        (tmp_path / "required_for_test.py").write_text("raise ImportError('broken dependency')")
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.setattr(local_ci, "DEV_MODULES", ("required_for_test",))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    if kind == "clean":
        local_ci.python_preflight(sys.executable)
    else:
        expected = {"missing": "cannot import required_for_test", "docker": "docker resolves outside",
                    "claude_agent_sdk": "claude_agent_sdk is importable"}[kind]
        with pytest.raises(local_ci.Failure, match=expected):
            local_ci.python_preflight(sys.executable)
