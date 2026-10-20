"""Live runtime facts and real provisioning receipts, without running repo code."""
import json
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("template", [
    "/opt/app/application-updates/versions/{revision}/venv/bin/{name}",
    r"C:\App\application-updates\versions\{revision}\venv\Scripts\{name}.exe",
])
@pytest.mark.parametrize("pytest_found, cli_found", [(True, False), (False, True), (False, False)])
def test_harvested_pytest_fact_is_stable_across_updates(
    tmp_path, monkeypatch, template, pytest_found, cli_found,
):
    from fluid.codemode.skills import SkillStore

    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda _: object() if pytest_found else None)
    store = SkillStore(tmp_path / "store")
    texts = []
    for revision in ("a" * 40, "B" * 40):
        executable = template.format(revision=revision, name="python")
        cli = template.format(revision=revision, name="pytest") if cli_found else None
        monkeypatch.setattr(runtime.sys, "executable", executable)
        monkeypatch.setattr(runtime.shutil, "which", lambda _: cli)
        harvested = runtime.harvest_environment_facts(str(tmp_path))
        text = dict(harvested)["env.pytest"]
        texts.append(text)
        expected_exe = template.format(revision="<current>", name="python")
        if pytest_found:
            assert text == f"pytest is importable from the worker Python {expected_exe}."
        elif cli_found:
            expected_cli = template.format(revision="<current>", name="pytest")
            assert text == (
                f"pytest is not importable from the worker Python {expected_exe}"
                f"; a pytest executable is on PATH at {expected_cli}."
            )
        else:
            assert text == (
                f"pytest is not importable from the worker Python {expected_exe}"
                "; run tests with a project venv or install pytest there first."
            )
        runtime.write_harvested_facts(store, "project:x", harvested)
    assert texts[0] == texts[1]
    fact = next(m for m in store.memories() if m.key == "env.pytest")
    assert fact.revision == 1
    assert fact.provenance == "tool" and not fact.protected


@pytest.mark.parametrize("executable, cli", [
    ("/usr/local/bin/python3", "/usr/local/bin/pytest"),
    (r"C:\App\venv\Scripts\python.exe", r"C:\Tools\pytest.exe"),
    ("/opt/versions/not-a-revision/bin/python", "/opt/versions/abc/bin/pytest"),
])
@pytest.mark.parametrize("pytest_found", [True, False])
def test_harvested_pytest_fact_keeps_non_versioned_paths(
    tmp_path, monkeypatch, executable, cli, pytest_found,
):
    monkeypatch.setattr(runtime.sys, "executable", executable)
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda _: object() if pytest_found else None)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: cli)
    text = dict(runtime.harvest_environment_facts(str(tmp_path)))["env.pytest"]
    if pytest_found:
        assert text == f"pytest is importable from the worker Python {executable}."
    else:
        assert text == (
            f"pytest is not importable from the worker Python {executable}"
            f"; a pytest executable is on PATH at {cli}."
        )


def test_not_a_repo_never_writes_receipt(tmp_path):
    runtime.record_provisioning(str(tmp_path), {"errors": []})
    assert list(tmp_path.iterdir()) == []


def test_static_environment_does_not_change_when_dependencies_appear(tmp_path):
    from fluid.context.environment import environment_block
    (tmp_path / "package.json").write_text("{}")
    before = environment_block(tmp_path, include_volatile=False)
    (tmp_path / "node_modules").mkdir()
    assert environment_block(tmp_path, include_volatile=False) == before
