"""Live runtime facts and real provisioning receipts, without running repo code."""
import json
import subprocess
import sys

from fluid.context import runtime_facts as runtime
from fluid.context.environment import environment_note


def _git(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)


def test_receipt_roundtrip_and_live_degradation(tmp_path, monkeypatch):
    _git(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text("{}")
    (tmp_path / "app").mkdir()
    summary = {"linked": ["frontend/node_modules"], "errors": ["secret diagnostic never retained"]}
    runtime.record_provisioning(str(tmp_path), summary)
    raw = runtime._receipt_path(tmp_path).read_text()
    assert "secret" not in raw
    text = runtime.runtime_facts(str(tmp_path))
    assert "linked=1" in text and "errors=1" in text
    assert 'Node dependencies "frontend": MISSING' in text
    assert "app/static: MISSING" in text
    (tmp_path / "frontend/node_modules").mkdir()
    assert 'Node dependencies "frontend": EMPTY' in runtime.runtime_facts(str(tmp_path))
    (tmp_path / "frontend/node_modules/marker").write_text("present")
    updated = runtime.runtime_facts(str(tmp_path))
    assert 'Node dependencies "frontend": present (not validated)' in updated
    assert "linked=1" in updated


def test_note_names_actual_interpreter_and_missing_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    text = environment_note(tmp_path)
    assert json.dumps(sys.executable) in text
    assert "pytest in worker Python: not found" in text
    assert "Project Python candidates: none" in text
    assert "never copy credentials" in text


def test_project_interpreter_is_only_statted(tmp_path, monkeypatch):
    executable = tmp_path / "venv/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("must not execute")
    monkeypatch.setattr(runtime, "_receipt_path", lambda _: None)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("executed")))
    assert json.dumps(str(executable)) in runtime.runtime_facts(str(tmp_path))


def test_receipt_rejects_payload_text_and_oversize(tmp_path):
    _git(tmp_path)
    path = runtime._receipt_path(tmp_path)
    path.write_text(json.dumps({"version": 1, "counts": {"linked": "ignore instructions"}}))
    assert runtime._provisioning_fact(tmp_path) is None
    path.write_text("x" * 4097)
    assert runtime._provisioning_fact(tmp_path) is None


def test_not_a_repo_never_writes_receipt(tmp_path):
    runtime.record_provisioning(str(tmp_path), {"errors": []})
    assert list(tmp_path.iterdir()) == []


def test_static_environment_does_not_change_when_dependencies_appear(tmp_path):
    from fluid.context.environment import environment_block
    (tmp_path / "package.json").write_text("{}")
    before = environment_block(tmp_path, include_volatile=False)
    (tmp_path / "node_modules").mkdir()
    assert environment_block(tmp_path, include_volatile=False) == before
