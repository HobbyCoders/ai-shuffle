"""Regression coverage for boot-smoke failure evidence (no server required)."""

import shutil
from pathlib import Path

import pytest

from tests.smoke.test_app_boot import _problem_lines, _preserve_logs

pytest_plugins = ["pytester"]
REPO = Path(__file__).resolve().parents[2]


def test_problem_lines_include_traceback_body_and_preceding_record():
    text = "\n".join([
        "INFO: stopping",
        "ERROR: background task failed",
        "Traceback (most recent call last):",
        '  File "worker.py", line 12, in stop',
        "    await task",
        "RuntimeError: shutdown race",
        "INFO: stopped",
    ])
    problems = _problem_lines(text)
    assert any("Traceback (most recent call last):" in block and
               "RuntimeError: shutdown race" in block and
               "ERROR: background task failed" in block for block in problems)


def test_problem_context_is_bounded_and_keeps_later_problems():
    text = "\n".join(["Traceback (most recent call last):"] +
                     [f"  frame {i}" for i in range(100)] + ["ERROR: later failure"])
    problems = _problem_lines(text)
    assert max(len(block.splitlines()) for block in problems) <= 40
    assert any("ERROR: later failure" in block for block in problems)


def test_allowlisted_lines_do_not_create_problems():
    assert _problem_lines("UserWarning: Duplicate Operation ID retired_billing_test") == []


def test_logs_survive_pruning_and_refresh_after_shutdown(tmp_path):
    root = tmp_path / "pytest-root"
    logs = root / "data" / "logs" / "run-one"
    logs.mkdir(parents=True)
    (root / "console.log").write_text("console before shutdown")
    (logs / "app.log").write_text("traceback body")
    (logs / "fatal.log").write_text("fatal evidence")
    destination = tmp_path / "durable" / "failed-run"
    _preserve_logs(root, destination)
    (root / "console.log").write_text("console after shutdown")
    _preserve_logs(root, destination)
    shutil.rmtree(root)
    assert (destination / "console.log").read_text() == "console after shutdown"
    assert (destination / "data/logs/run-one/app.log").read_text() == "traceback body"
    assert (destination / "data/logs/run-one/fatal.log").read_text() == "fatal evidence"


def test_missing_logs_during_failed_startup_are_ok(tmp_path):
    root = tmp_path / "startup"
    root.mkdir()
    (root / "console.log").write_text("early import failure")
    destination = tmp_path / "durable"
    _preserve_logs(root, destination)
    assert (destination / "console.log").read_text() == "early import failure"


@pytest.mark.parametrize("phase", ["setup", "call", "teardown", "pass"])
def test_failure_report_keeps_logs_and_prints_path(pytester, monkeypatch, phase):
    # Exercise pytest's actual report lifecycle, including failed fixture setup.
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    destination = pytester.path / "durable-artifacts"
    pytester.makeconftest(f"""
import pytest
from pathlib import Path
from tests.smoke.conftest import pytest_runtest_makereport
from tests.smoke.test_app_boot import _preserve_logs

@pytest.fixture(scope="module")
def server(request):
    root = Path("prunable-root")
    root.mkdir()
    (root / "console.log").write_text("shutdown evidence")
    def preserve():
        destination = Path({str(destination)!r})
        _preserve_logs(root, destination)
        return destination
    request.node._boot_smoke_preserve_failure = preserve
    if {phase!r} == "setup":
        pytest.fail("intentional setup failure")
    yield
    if {phase!r} == "teardown":
        pytest.fail("intentional teardown failure")
""")
    pytester.makepyfile(f"""
def test_server(server):
    assert {phase!r} != "call", "intentional call failure"
""")
    result = pytester.runpytest_subprocess("-q", "-p", "no:randomly")
    if phase == "pass":
        result.assert_outcomes(passed=1)
        assert not destination.exists()
    else:
        if phase == "call":
            result.assert_outcomes(failed=1)
        elif phase == "setup":
            result.assert_outcomes(errors=1)
        else:
            result.assert_outcomes(passed=1, errors=1)
        assert f"Failed boot logs preserved at: {destination}" in result.stdout.str()
        shutil.rmtree(pytester.path / "prunable-root")
        assert (destination / "console.log").read_text() == "shutdown evidence"


def test_real_fixture_preserves_failed_startup(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    artifact_parent = pytester.path / "kept"
    monkeypatch.setenv("SMOKE_ARTIFACT_DIR", str(artifact_parent))
    pytester.makeconftest("""
from tests.smoke.conftest import pytest_runtest_makereport
""")
    pytester.makepyfile("""
from unittest.mock import Mock
import pytest
from tests.smoke import test_app_boot as boot
from tests.smoke.test_app_boot import server

@pytest.fixture(scope="module", autouse=True)
def fake_startup():
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(boot, "_pg16_bindir", lambda: "/unused")
        monkeypatch.setattr(boot, "_load_container_runtime", lambda: Mock())
        def crash(*args, **kwargs):
            raise RuntimeError("intentional startup crash")
        monkeypatch.setattr(boot.subprocess, "Popen", crash)
        yield

def test_boot(server):
    pass
""")
    result = pytester.runpytest_subprocess("-q", "-p", "no:randomly")
    result.assert_outcomes(errors=1)
    assert "intentional startup crash" in result.stdout.str()
    destinations = list(artifact_parent.iterdir())
    assert len(destinations) == 1
    assert f"Failed boot logs preserved at: {destinations[0]}" in result.stdout.str()
    assert (destinations[0] / "console.log").is_file()
    assert not (destinations[0] / "empty.env").exists()
    assert not (destinations[0] / "home").exists()
