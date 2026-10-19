"""scripts/ci/windows_parts.py and the hosted Windows suite's parallel parts."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import local_ci  # noqa: E402  pyright: ignore[reportMissingImports]
import local_ci_stages  # noqa: E402  pyright: ignore[reportMissingImports]
import windows_parts  # noqa: E402  pyright: ignore[reportMissingImports]

WORKFLOW = ROOT / ".github" / "workflows" / "windows-suite.yml"


def workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_the_workflow_runs_every_part_without_cancelling_the_others():
    suite = workflow()["jobs"]["suite"]
    assert suite["strategy"]["matrix"]["part"] == windows_parts.parts()
    assert suite["strategy"]["fail-fast"] == "false"
    assert suite["if"] == "inputs.tests == ''"
    command = next(step["run"] for step in suite["steps"] if step.get("name") == "Suite part")
    assert 'scripts/ci/windows_parts.py "$PART"' in command


def test_selected_tests_are_refused_on_main():
    selected = workflow()["jobs"]["selected"]
    assert selected["if"] == "inputs.tests != ''"
    refuse = next(step for step in selected["steps"] if step.get("name") == "Refuse a partial run on main")
    assert refuse["if"] == "github.ref == 'refs/heads/main'"
    assert "exit 1" in refuse["run"]


@pytest.mark.parametrize("tests, expected, fail_match", [
    ("tests/api/test_git_plain_folder.py android/ci/test-ui-runner.py", ["python"], ""),
    ("frontend/src/lib/components/deck/cards/NotesCard.editing.test.ts", ["npm", "npx", "npx"], ""),
    ("tests/api/test_git_plain_folder.py frontend/src/lib/components/deck/cards/NotesCard.editing.test.ts",
     ["python", "npm", "npx", "npx"], ""),
    ("tests/api/test_git_plain_folder.py", ["python"], "pytest"),
    ("frontend/src/lib/components/deck/cards/NotesCard.editing.test.ts", ["npm", "npx", "npx"], "vitest"),
])
def test_selected_tests_route_to_their_runner_and_preserve_failures(tmp_path, tests, expected, fail_match):
    command = next(step["run"] for step in workflow()["jobs"]["selected"]["steps"]
                   if step.get("name") == "Selected tests")
    (tmp_path / "frontend").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "npm", "npx"):
        shim = bin_dir / name
        shim.write_text(
            '#!/usr/bin/env bash\n'
            'printf "%s:%s\\n" "${0##*/}" "$*" >> "$CALLS"\n'
            'if [[ -n "$FAIL_MATCH" && "$*" == *"$FAIL_MATCH"* ]]; then exit 9; fi\n',
            encoding="utf-8", newline="\n")
        shim.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", command], cwd=tmp_path,
                            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                                 "LOCAL_CI_PYTHON": (bin_dir / "python").as_posix(), "TESTS": tests,
                                 "RUNNER_TEMP": tmp_path.as_posix(), "CALLS": calls.as_posix(),
                                 "FAIL_MATCH": fail_match}, capture_output=True, text=True, timeout=15)
    assert result.returncode == (9 if fail_match else 0), result.stdout + result.stderr
    lines = calls.read_text().splitlines()
    assert [line.split(":", 1)[0] for line in lines] == expected
    if "python" in expected:
        assert "-m pytest tests/api/test_git_plain_folder.py" in lines[0]
        assert "frontend/" not in lines[0]
    if "npx" in expected:
        assert lines[-1] == "npx:vitest run src/lib/components/deck/cards/NotesCard.editing.test.ts"


def test_the_backend_parts_cover_every_backend_file_exactly_once():
    expected = [f for batch in local_ci_stages.backend_batches(local_ci) for f in batch]
    dealt = [f for i in range(windows_parts.BACKEND_PARTS) for f in windows_parts.backend_files(i)]
    assert len(expected) > 100
    assert sorted(dealt) == sorted(expected)
    assert len(dealt) == len(set(dealt))
    assert all(windows_parts.backend_files(i) for i in range(windows_parts.BACKEND_PARTS))


def test_the_parts_cover_every_suite():
    names = [suite.name for suite in local_ci.SUITES]
    assert sorted([*windows_parts.SEPARATE, *windows_parts.rest_suites()]) == sorted(names)
    assert "backend" in names and set(windows_parts.OWN_PART) <= set(names)


def test_an_unknown_part_is_refused(monkeypatch):
    with pytest.raises(local_ci.Failure, match="no part"):
        windows_parts.run_part("backend-99", sys.executable)
    with pytest.raises(local_ci.Failure, match="no part"):
        windows_parts.run_part("everything", sys.executable)


def test_rest_runs_every_suite_after_a_failure(monkeypatch):
    seen = []
    monkeypatch.setattr(local_ci, "security_check", lambda python: seen.append("security"))

    def run_suites(mode, only, python):
        seen.append(only[0])
        if only[0] == "fluid":
            raise local_ci.Failure("fluid broke")
        return [], [], []

    monkeypatch.setattr(local_ci, "run_suites", run_suites)
    with pytest.raises(local_ci.Failure, match="fluid: fluid broke"):
        windows_parts.run_part("rest", sys.executable)
    assert seen == ["security", *windows_parts.rest_suites()]


# --- one rerun of failed tests (local_ci.run with LOCAL_CI_RETRY_FAILED=1) ---

@pytest.fixture
def retrying(monkeypatch):
    monkeypatch.setenv(local_ci.RETRY_FAILED_ENV, "1")
    monkeypatch.setattr(local_ci, "FLAKY", [])


def pytest_command(*targets):
    return [sys.executable, "-m", "pytest", *map(str, targets), "-o", "addopts=", "-q", "-p", "no:cacheprovider"]


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_a_test_that_passes_on_the_rerun_is_flaky_not_failed(tmp_path, retrying):
    marker = tmp_path / "ran-once"
    tests = write(tmp_path / "test_flaky.py", f"""
from pathlib import Path
MARKER = Path({str(marker)!r})

def test_steady():
    pass

def test_flaky():
    if not MARKER.exists():
        MARKER.write_text("x")
        raise AssertionError("first run fails")
""")
    local_ci.run(pytest_command(tests), env=local_ci.python_env())
    assert [test.rsplit("::", 1)[1] for test in local_ci.FLAKY] == ["test_flaky"]
    assert Path(local_ci.FLAKY[0].split("::")[0]).resolve() == tests.resolve()


def test_a_test_that_fails_twice_fails_the_command(tmp_path, retrying):
    tests = write(tmp_path / "test_broken.py", "def test_broken():\n    raise AssertionError\n")
    with pytest.raises(local_ci.Failure, match="failed again"):
        local_ci.run(pytest_command(tests), env=local_ci.python_env())
    assert local_ci.FLAKY == []


def test_a_collection_error_is_not_retried(tmp_path, retrying):
    tests = write(tmp_path / "test_syntax.py", "def test_x(:\n")
    with pytest.raises(local_ci.Failure, match="exited") as raised:
        local_ci.run(pytest_command(tests), env=local_ci.python_env())
    assert "failed again" not in str(raised.value)


def test_many_failures_are_not_retried(tmp_path, retrying, monkeypatch):
    monkeypatch.setattr(local_ci, "MAX_RETRIED", 1)
    tests = write(tmp_path / "test_many.py", "def test_a():\n    assert 0\n\ndef test_b():\n    assert 0\n")
    with pytest.raises(local_ci.Failure) as raised:
        local_ci.run(pytest_command(tests), env=local_ci.python_env())
    assert "failed again" not in str(raised.value)


def test_without_the_switch_a_failure_is_final(tmp_path, monkeypatch):
    monkeypatch.delenv(local_ci.RETRY_FAILED_ENV, raising=False)
    monkeypatch.setattr(local_ci, "FLAKY", [])
    marker = tmp_path / "ran-once"
    tests = write(tmp_path / "test_once.py", f"""
from pathlib import Path

def test_once():
    marker = Path({str(marker)!r})
    if not marker.exists():
        marker.write_text("x")
        raise AssertionError
""")
    with pytest.raises(local_ci.Failure):
        local_ci.run(pytest_command(tests), env=local_ci.python_env())
    assert local_ci.FLAKY == []


def test_flaky_tests_reach_the_job_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    windows_parts.report_flaky([f"{ROOT / 'tests' / 'test_main.py'}::test_x[a]"])
    assert "::warning title=Flaky test::tests/test_main.py::test_x[a]" in capsys.readouterr().out
    assert "- `tests/test_main.py::test_x[a]`" in summary.read_text(encoding="utf-8")
