"""Offline security gate contracts, including the real shell hook on scratch commits."""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import dependency_audit
import local_ci
import secret_scan


@pytest.mark.parametrize("token", [
    "AKIA" + "Q7W8E9R0T1Y2U3I4",
    "ASIA" + "Q7W8E9R0T1Y2U3I4",
    "ghp_" + "a" * 36,
    "github_pat_" + "a" * 82,
    "sk-ant-api03-" + "a" * 40,
    "sk-ant-oat01-" + "a" * 40,
    "sk-proj-" + "a" * 40,
    "sk-or-v1-" + "a" * 64,
    "sk-" + "a" * 48,
    "AIza" + "a" * 35,
    "xoxb-" + "1234567890-" * 3,
    "sk_live_" + "a" * 24,
    "-----BEGIN " + "PRIVATE KEY-----\n" + "a" * 64,
])
def test_high_signal_rules_also_apply_to_tests(token):
    rules = secret_scan.load_rules(ROOT)
    assert list(secret_scan.findings(rules, "tests/new_fixture.py", token))


def test_allowlist_requires_both_exact_fixture_and_path():
    rules = secret_scan.load_rules(ROOT)
    path = "tests/core/test_claude_cloud_auth.py"
    fake = "AKIA" + "FAKEFAKEFAKE0000"
    assert not list(secret_scan.findings(rules, path, fake))
    assert list(secret_scan.findings(rules, "app/new.py", fake))
    assert list(secret_scan.findings(rules, path, "AKIA" + "Q7W8E9R0T1Y2U3I4"))
    for placeholder in ("sk-test", "AKIA-OWNER-SECRET", "v1.uuid.123.hex"):
        assert not list(secret_scan.findings(rules, "docs/example.md", placeholder))


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE).decode().strip()


def init(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Security gate test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(root / "disabled-hooks"))
    shutil.copyfile(ROOT / ".gitleaks.toml", root / ".gitleaks.toml")


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-qm", "Scratch fixture\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>")
    return git(root, "rev-parse", "HEAD")


def test_tree_scope_and_redacted_output(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    commit(tmp_path)
    token = "AKIA" + "Q7W8E9R0T1Y2U3I4"
    (tmp_path / "ignored.txt").write_text(token)
    assert secret_scan.scan(tmp_path) == 0
    (tmp_path / "untracked.txt").write_text(token)
    assert secret_scan.scan(tmp_path) == 1
    (tmp_path / "untracked.txt").unlink()
    git(tmp_path, "add", "-f", "ignored.txt")
    assert secret_scan.scan(tmp_path) == 1
    output = capsys.readouterr().out
    assert token not in output
    assert "aws-access-key" in output and "value redacted" in output


@pytest.mark.parametrize("remote_exists", [False, True])
def test_actual_hook_blocks_scratch_history_even_with_cached_clean_tree(remote_exists):
    # No secret commit is made in the developer's checkout, and the temporary
    # repository (including its object database) is dropped on exit. No push.
    with tempfile.TemporaryDirectory(prefix="secret-hook-proof-") as directory:
        root = Path(directory)
        init(root)
        (root / "scripts").mkdir()
        for name in ("local_ci.py", "secret_scan.py"):
            shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
        (root / "hook").write_bytes((ROOT / ".githooks/pre-push").read_bytes())
        base = commit(root)
        git(root, "update-ref", "refs/remotes/origin/main", base)
        token = "AKIA" + "Q7W8E9R0T1Y2U3I4"
        (root / "planted.txt").write_text(token)
        commit(root)
        (root / "planted.txt").unlink()
        tip = commit(root)
        assert secret_scan.scan(root) == 0
        records = root / ".git/local-ci/passes.json"
        records.parent.mkdir()
        records.write_text(json.dumps({git(root, "rev-parse", "HEAD^{tree}"): {"security_gate": 1}}))
        for ref in ("refs/heads/topic", "refs/tags/scratch"):
            refs = f"refs/heads/topic {tip} {ref} {base if remote_exists else '0' * 40}\n"
            result = subprocess.run(["sh", str(root / "hook")], cwd=root, input=refs,
                                    env={**os.environ, "LOCAL_CI_PYTHON": sys.executable},
                                    text=True, capture_output=True, check=False)
            assert result.returncode != 0
            assert "aws-access-key" in result.stdout
            assert token not in result.stdout + result.stderr


def test_commit_scan_handles_binary_and_unusual_names(tmp_path):
    init(tmp_path)
    base = commit(tmp_path)
    (tmp_path / 'quoted " name.txt').write_bytes(b"\0" + b"ghp_" + b"A" * 36)
    tip = commit(tmp_path)
    assert secret_scan.scan(tmp_path, tip, base) == 1
    assert secret_scan.scan(tmp_path, revision=tip) == 1


def test_invalid_config_fails_closed(tmp_path):
    (tmp_path / ".gitleaks.toml").write_text('[extend]\nuseDefault = true\n')
    with pytest.raises(ValueError):
        secret_scan.load_rules(tmp_path)


def test_old_pass_cannot_skip_security_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(local_ci, "records_path", lambda: tmp_path / "passes.json")
    local_ci.save_record("tree", {"at": 1})
    monkeypatch.setattr(local_ci, "git", lambda *args: "head")
    monkeypatch.setattr(local_ci, "tree_of", lambda *args: "tree")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("refs/heads/a head refs/heads/a remote\n"))
    calls = []
    monkeypatch.setattr(local_ci, "scan_push", lambda *args: calls.append("scan"))
    monkeypatch.setattr(local_ci, "cmd_check", lambda args: calls.append("check") or 0)
    assert local_ci.cmd_pre_push(argparse.Namespace()) == 0
    assert calls == ["scan", "check"]


def test_check_security_steps_run_even_with_only(monkeypatch):
    calls = []
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "git", lambda *args: "head")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: False)
    monkeypatch.setattr(local_ci, "security_check", lambda python, strict: calls.append(("security", strict)))
    monkeypatch.setattr(local_ci, "run_suites", lambda *args: (calls.append(("suites", args[1])) or ([], [], [])))
    args = argparse.Namespace(command="check", attest=False, only=["backend"], strict_audit=True)
    assert local_ci.cmd_check(args) == 0
    assert calls == [("security", True), ("suites", ["backend"])]


@pytest.mark.parametrize("strict,expected", [(False, 0), (True, 1)])
@pytest.mark.parametrize("unavailable", [False, True])
def test_dependency_warnings_and_strict_failure(tmp_path, monkeypatch, capsys, strict, expected, unavailable):
    monkeypatch.setattr(dependency_audit, "ROOT", tmp_path)
    monkeypatch.setattr(dependency_audit, "audit_python", lambda: Path(sys.executable))
    (tmp_path / "requirements.txt").write_text("example==1\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package-lock.json").write_text("{}")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if unavailable:
            raise subprocess.TimeoutExpired(argv, 1)
        data = ({"dependencies": [{"name": "example", "vulns": [{"id": "TEST"}]}]} if "pip_audit" in argv else
                {"metadata": {"vulnerabilities": {"high": 1}}, "vulnerabilities": {"example": {"severity": "high"}}})
        return subprocess.CompletedProcess(argv, 1, json.dumps(data), "")

    monkeypatch.setattr(dependency_audit, "command", run)
    assert dependency_audit.audit(strict) == expected
    assert len(calls) == 2
    assert "--audit-level=high" in calls[1] and "--package-lock-only" in calls[1]
    output = capsys.readouterr().out
    assert "Python requirements.txt:" in output and "npm frontend:" in output
    assert ("UNAVAILABLE" if unavailable else "severity unknown") in output


def test_npm_moderate_does_not_fail_high_threshold():
    _, failed = dependency_audit.npm_summary({
        "metadata": {"vulnerabilities": {"moderate": 1}},
        "vulnerabilities": {"example": {"severity": "moderate"}},
    })
    assert not failed


def test_python_skipped_dependency_is_not_clean():
    _, failed = dependency_audit.pip_summary({"dependencies": [{"name": "local", "skip_reason": "local project"}]})
    assert failed
