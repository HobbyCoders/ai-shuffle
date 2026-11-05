"""Exercise the real audit subprocess boundary without contacting PyPI."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import dependency_audit
import local_ci
import pip_audit_retry as audit

READ_TIMEOUT = (
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Read timed out. (read timeout=15)\n"
)
CLEAN = '{"dependencies": [{"name": "example", "vulns": []}]}'
FINDINGS = '{"dependencies": [{"name": "example", "vulns": [{"id": "TEST-1"}]}]}'
PRODUCER = """import json
import os
import sys
import time
from pathlib import Path

state = Path(os.environ["AUDIT_SCENARIO"])
data = json.loads(state.read_text())
index = data["attempts"]
data["attempts"] += 1
data.setdefault("starts", []).append(time.monotonic())
data.setdefault("argv", []).append(sys.argv[1:])
state.write_text(json.dumps(data))
step = data["steps"][min(index, len(data["steps"]) - 1)]
print(step.get("stdout", ""), end="", flush=True)
print(step.get("stderr", ""), end="", file=sys.stderr, flush=True)
if step.get("child_sleep"):
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({step['child_sleep']})"])
    data["child_pid"] = child.pid
    state.write_text(json.dumps(data))
time.sleep(step.get("sleep", 0))
sys.exit(step.get("code", 0))
"""


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    # The child resolves -m pip_audit to this isolated scripted transport.
    # subprocess creation, exit codes, capture, deadlines and sleeps stay real.
    (tmp_path / "pip_audit.py").write_text(PRODUCER, encoding="utf-8")
    state = tmp_path / "scenario.json"
    monkeypatch.setenv("AUDIT_SCENARIO", str(state))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    def prepare(steps):
        state.write_text(json.dumps({"attempts": 0, "steps": steps}), encoding="utf-8")
        return state

    return prepare


def invoke(scenario, steps, arguments=("--skip-editable",)):
    state = scenario(steps)
    started = time.monotonic()
    result = audit.run_audit(arguments, python=sys.executable, cwd=state.parent)
    return result, json.loads(state.read_text()), time.monotonic() - started


def test_header_read_timeout_recovers_through_real_subprocess(scenario, monkeypatch):
    monkeypatch.setattr(audit, "BACKOFF", (0.02, 0.04))
    result, data, _ = invoke(scenario, [
        {"stderr": READ_TIMEOUT, "code": 1}, {"stdout": CLEAN},
    ], ("-r", "requirements-voice.lock", "--format=json"))
    assert result.returncode == 0 and result.stdout == CLEAN
    assert READ_TIMEOUT in result.stderr and "retry 2/3" in result.stderr
    assert data["attempts"] == 2
    assert data["starts"][1] - data["starts"][0] >= 0.02
    assert all(args == ["-r", "requirements-voice.lock", "--format=json",
                        "--timeout=30", "--progress-spinner=off"] for args in data["argv"])


@pytest.mark.parametrize("path", [
    "scripts/pip_audit_retry.py", "scripts/verification_evidence.py",
    "scripts/dependency_audit.py", "tests/scripts/test_pip_audit_retry.py",
])
def test_local_workflow_suite_watches_audit_policy_and_runs_its_tests(monkeypatch, path):
    assert "workflows" in {suite.name for suite in local_ci.affected_suites([path])}
    commands = []
    monkeypatch.setattr(local_ci, "has_module", lambda *args: True)
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: commands.append(argv))
    local_ci.workflows(local_ci.Context(mode="changed", base="base", files=[path], python=sys.executable))
    [pytest_command] = [argv for argv in commands if argv[1:3] == ["-m", "pytest"]]
    assert "tests/scripts/test_pip_audit_retry.py" in pytest_command


def test_cli_exhausts_exact_production_attempts_and_backoff(scenario):
    state = scenario([{"stderr": READ_TIMEOUT, "code": 1}])
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(ROOT / "scripts/pip_audit_retry.py"), "--skip-editable"],
                            cwd=state.parent, capture_output=True, text=True, timeout=15)
    elapsed = time.monotonic() - started
    data = json.loads(state.read_text())
    assert result.returncode == 1 and result.stdout == ""
    assert data["attempts"] == 3
    gaps = [b - a for a, b in zip(data["starts"], data["starts"][1:])]
    assert gaps[0] >= 2 and gaps[1] >= 4
    assert 6 <= elapsed < 15
    assert result.stderr.count(READ_TIMEOUT) == 3
    assert "retry 2/3 in 2s" in result.stderr and "retry 3/3 in 4s" in result.stderr
    print(f"production exhaustion: attempts={data['attempts']}, gaps={gaps}, elapsed={elapsed:.3f}s")


def test_cli_recovery_preserves_stdout_and_stderr(scenario):
    state = scenario([{"stderr": READ_TIMEOUT, "code": 1},
                      {"stdout": "No known vulnerabilities found\n", "stderr": "final diagnostic\n"}])
    result = subprocess.run([sys.executable, str(ROOT / "scripts/pip_audit_retry.py"),
                             "-r", "requirements-voice.lock"],
                            cwd=state.parent, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout == "No known vulnerabilities found\n"
    assert READ_TIMEOUT in result.stderr and "final diagnostic\n" in result.stderr
    assert json.loads(state.read_text())["attempts"] == 2


@pytest.mark.parametrize("stdout", [FINDINGS, "example 1.0 TEST-1 2.0\n"])
def test_findings_fail_without_retry(scenario, stdout):
    result, data, _ = invoke(scenario, [
        {"stdout": stdout, "stderr": "Found 1 known vulnerability in 1 package\n", "code": 1},
        {"stdout": CLEAN},
    ])
    assert result.returncode == 1 and result.stdout == stdout
    assert "Found 1" in result.stderr and data["attempts"] == 1


def test_findings_after_transport_failure_still_fail(scenario, monkeypatch):
    monkeypatch.setattr(audit, "BACKOFF", (0.01, 0.02))
    result, data, _ = invoke(scenario, [
        {"stderr": READ_TIMEOUT, "code": 1}, {"stdout": FINDINGS, "code": 1}, {"stdout": CLEAN},
    ])
    assert result.returncode == 1 and result.stdout == FINDINGS
    assert data["attempts"] == 2 and READ_TIMEOUT in result.stderr


@pytest.mark.parametrize("code,stdout,stderr", [
    (2, "", "unrecognized arguments: --bad\n"),
    (1, "", "ERROR: Could not find a version that satisfies the requirement missing\n"),
    (1, "", "requests.exceptions.SSLError: certificate verify failed\n"),
    (1, "", "requests.exceptions.HTTPError: 403 Client Error: Forbidden\n"),
    (1, "", "requests.exceptions.ConnectionError: connection refused\n"),
    (1, "", READ_TIMEOUT.replace("pypi.org", "registry.example")),
    (1, "", READ_TIMEOUT.replace("pypi.org", "pypi.org.example")),
    (1, "", READ_TIMEOUT + "ValueError: another failure\n"),
    (2, "", READ_TIMEOUT),
    (1, FINDINGS, READ_TIMEOUT),
    (1, CLEAN, READ_TIMEOUT),
    (1, "", "Found 1 known vulnerability\n" + READ_TIMEOUT),
])
def test_unknown_or_mixed_errors_are_not_retried(scenario, code, stdout, stderr):
    result, data, _ = invoke(scenario, [{"code": code, "stdout": stdout, "stderr": stderr}])
    assert result.returncode == code and result.stdout == stdout and result.stderr == stderr
    assert data["attempts"] == 1


def test_process_deadline_fails_closed_and_keeps_partial_output(scenario, monkeypatch):
    # Leave ample room for interpreter startup on a busy hosted runner.
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 3)
    result, data, elapsed = invoke(scenario, [
        {"stdout": "partial stdout\n", "stderr": "partial stderr\n", "sleep": 10},
    ])
    assert result.returncode == 124 and data["attempts"] == 1
    assert result.stdout == "partial stdout\n" and "partial stderr\n" in result.stderr
    assert "deadline exceeded" in result.stderr and elapsed < 8
    print(f"process deadline: attempts={data['attempts']}, elapsed={elapsed:.3f}s, budget=3s")


def assert_resolver_descendant_stopped(status):
    # A killed grandchild can briefly remain a zombie until init reaps it.
    for _ in range(50):
        try:
            if status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
        except (FileNotFoundError, ProcessLookupError):
            break
        time.sleep(0.01)
    else:
        pytest.fail("resolver descendant survived the audit deadline")


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_resolver_exit_during_proc_read_is_already_stopped(monkeypatch, error):
    def exited(*args, **kwargs):
        raise error("process exited during the stat read")

    monkeypatch.setattr(Path, "read_text", exited)
    assert_resolver_descendant_stopped(Path("/proc/fixture/stat"))


def test_resolver_descendant_survival_still_fails(tmp_path, monkeypatch):
    status = tmp_path / "stat"
    status.write_text("123 (resolver) S 1 2 3")
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(pytest.fail.Exception, match="resolver descendant survived the audit deadline"):
        assert_resolver_descendant_stopped(status)
    assert sleeps == [0.01] * 50


def test_resolver_descendant_zombie_is_stopped(tmp_path):
    status = tmp_path / "stat"
    status.write_text("123 (resolver) Z 1 2 3")
    assert_resolver_descendant_stopped(status)


def test_process_deadline_cleans_up_resolver_descendants(scenario, monkeypatch):
    # Leave ample room for interpreter startup on a busy hosted runner.
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 3)
    result, data, elapsed = invoke(scenario, [
        {"stdout": "resolver starting\n", "stderr": "resolver diagnostic\n", "child_sleep": 10, "sleep": 10},
    ])
    assert result.returncode == 124 and data["attempts"] == 1 and data["child_pid"]
    assert result.stdout == "resolver starting\n" and "resolver diagnostic\n" in result.stderr
    assert elapsed < 8
    if sys.platform == "linux":
        assert_resolver_descendant_stopped(Path(f"/proc/{data['child_pid']}/stat"))
    print(f"resolver-tree deadline: attempts={data['attempts']}, elapsed={elapsed:.3f}s, budget=3s")


def test_deadline_is_shared_across_subprocesses_and_backoff(scenario, monkeypatch):
    # Both real interpreter startups and the backoff share this roomy budget.
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 3)
    monkeypatch.setattr(audit, "BACKOFF", (0.1, 0.2))
    timeouts = []
    run_once = audit._run_once

    def record_timeout(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return run_once(*args, **kwargs)

    monkeypatch.setattr(audit, "_run_once", record_timeout)
    result, data, elapsed = invoke(scenario, [
        {"stderr": READ_TIMEOUT, "code": 1, "sleep": 0.1},
        {"stderr": "second attempt output\n", "sleep": 10},
    ])
    assert result.returncode == 124 and data["attempts"] == 2
    assert READ_TIMEOUT in result.stderr and "second attempt output" in result.stderr
    assert data["starts"][1] - data["starts"][0] >= 0.2
    assert len(timeouts) == 2
    assert timeouts[1] <= audit.TOTAL_TIMEOUT - audit.BACKOFF[0]
    assert elapsed < 8
    print(f"shared deadline: attempts={data['attempts']}, elapsed={elapsed:.3f}s, budget=3s")


def test_insufficient_retry_budget_returns_failure_without_sleep(scenario, monkeypatch):
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 3)
    monkeypatch.setattr(audit, "BACKOFF", (4, 8))
    result, data, elapsed = invoke(scenario, [{"stderr": READ_TIMEOUT, "code": 1}])
    assert result.returncode == 1 and data["attempts"] == 1
    assert "insufficient retry budget" in result.stderr and elapsed < 8


def test_expired_budget_does_not_launch_a_subprocess(scenario, monkeypatch):
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 0)
    result, data, _ = invoke(scenario, [{"stdout": CLEAN}])
    assert result.returncode == 124 and data["attempts"] == 0


def test_missing_executable_is_failure_not_retry(scenario):
    state = scenario([{"stdout": CLEAN}])
    result = audit.run_audit([], python=state.parent / "missing-python", cwd=state.parent)
    assert result.returncode == 127 and "missing-python" in result.stderr
    assert json.loads(state.read_text())["attempts"] == 0


@pytest.mark.parametrize("steps,expected,summary,attempts", [
    ([{"stderr": READ_TIMEOUT, "code": 1}, {"stdout": CLEAN}], 0, "0 advisories", 2),
    ([{"stderr": READ_TIMEOUT, "code": 1}], 1, "UNAVAILABLE", 3),
    ([{"stdout": FINDINGS, "code": 1}], 1, "1 advisories", 1),
    ([{"code": 2, "stderr": "resolver failed\n"}], 1, "UNAVAILABLE", 1),
    ([{"stdout": CLEAN, "stderr": READ_TIMEOUT, "code": 1}], 1, "UNAVAILABLE", 1),
])
def test_strict_local_audit_uses_real_retrying_subprocess(scenario, monkeypatch, capsys,
                                                         steps, expected, summary, attempts):
    state = scenario(steps)
    monkeypatch.setattr(audit, "BACKOFF", (0.01, 0.02))
    monkeypatch.setattr(dependency_audit, "ROOT", state.parent)
    monkeypatch.setattr(dependency_audit, "audit_python", lambda: Path(sys.executable))
    (state.parent / "requirements.txt").write_text("example==1\n")
    assert dependency_audit.audit(strict=True) == expected
    output = capsys.readouterr().out
    assert summary in output
    # Existing local reports intentionally redact raw registry diagnostics.
    assert READ_TIMEOUT not in output
    assert json.loads(state.read_text())["attempts"] == attempts
