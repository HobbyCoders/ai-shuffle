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
        records.write_text(json.dumps({git(root, "rev-parse", "HEAD^{tree}"): {"security_gate": local_ci.SECURITY_GATE_VERSION}}))
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
    # Git trees can contain this path even where the filesystem cannot
    # (Windows forbids double quotes). Keep the exact unusual-name coverage.
    blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=tmp_path,
                                   input=b"\0" + b"ghp_" + b"A" * 36).strip()
    entries = subprocess.check_output(["git", "ls-tree", "-z", base], cwd=tmp_path)
    entries += b'100644 blob ' + blob + b'\tquoted " name.txt\0'
    tree = subprocess.check_output(["git", "mktree", "-z"], cwd=tmp_path, input=entries).decode().strip()
    tip = git(tmp_path, "commit-tree", tree, "-p", base, "-m", "Binary unusual-name fixture")
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
    monkeypatch.setattr(local_ci, "security_check", lambda python, strict, **kwargs: calls.append(("security", strict, kwargs["audit"])))
    monkeypatch.setattr(local_ci, "run_suites", lambda *args: (calls.append(("suites", args[1])) or ([], [], [])))
    args = argparse.Namespace(command="check", attest=False, only=["backend"], strict_audit=True)
    assert local_ci.cmd_check(args) == 0
    assert calls == [("security", True, True), ("suites", ["backend"])]


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




@pytest.mark.parametrize("setting,rule_id", [
    ('CLOUDFLARE_API_TOKEN="' + 'a' * 40 + '"', "cloudflare-api-token"),
    ('"cloudflare_api_token": "' + 'Z_9-' * 10 + '"', "cloudflare-api-token"),
    ('123456789:' + 'a' * 35, "telegram-bot-token"),
    ('123456789:' + 'a' * 34 + '-', "telegram-bot-token"),
    ('M' + 'a' * 23 + '.' + 'b' * 6 + '.' + 'c' * 38, "discord-bot-token"),
    ('xapp-1-A123456789-1234567890-' + 'd' * 32, "slack-app-token"),
    ('whsec_' + 'e' * 32, "stripe-webhook-secret"),
    ('postgresql://' + 'worker:synthetic-password@host/db', "postgres-password-url"),
])
def test_additional_provider_shapes(setting, rule_id):
    assert rule_id in {rid for rid, _ in secret_scan.findings(secret_scan.load_rules(ROOT), "new.py", setting)}


@pytest.mark.parametrize("text", [
    'CHECKSUM="' + 'a' * 40 + '"',
    'CLOUDFLARE_API_TOKEN="${TOKEN}"',
    'CLOUDFLARE_API_TOKEN="short-placeholder"',
    '123456789:short-placeholder',
    'v1.uuid.123.hex',
    'eyJ' + 'a' * 25 + '.' + 'b' * 40 + '.' + 'c' * 38,
    'xapp-not-a-token', 'whsec_short',
    'postgresql://' + 'user@host/db',
    'postgresql://' + 'user:${PASSWORD}@host/db',
    'postgresql://' + 'user:***@host/db',
    'postgresql://' + '{user}:{password}@host/db',
])
def test_provider_context_and_placeholders_do_not_match(text):
    assert not list(secret_scan.findings(secret_scan.load_rules(ROOT), "new.py", text))



@pytest.mark.parametrize("password", [
    "mix$ed!", "mix*ed!", "$mixed!", "*mixed!", "mixed$", "mixed*",
    "mix$ed*123!", "${PASSWORD}literal", "literal${PASSWORD}",
    "***literal", "literal***", "<password>literal", "literal<password>",
    "it's-mixed!", "mix{ed}!", "mix[ed]!",
])
def test_postgres_password_symbols_are_scanned(password):
    text = 'postgresql://' + f'user:{password}@host/db'
    assert ('postgres-password-url', 1) in list(secret_scan.findings(secret_scan.load_rules(ROOT), 'new.py', text))


@pytest.mark.parametrize("password", [
    "${PASSWORD}", "$PASSWORD", "***", "********", "<password>",
    "{password}", "[REDACTED]", "[redacted]",
])
def test_postgres_only_whole_password_placeholders_are_ignored(password):
    text = 'postgresql://' + f'user:{password}@host/db'
    assert not list(secret_scan.findings(secret_scan.load_rules(ROOT), 'new.py', text))



@pytest.mark.parametrize("password", ["synthetic$p4ss!", "synthetic*p4ss!"])
def test_committed_postgres_password_symbols_block(password, capsys):
    with tempfile.TemporaryDirectory(prefix="postgres-secret-symbols-") as directory:
        root = Path(directory)
        init(root)
        base = commit(root)
        value = 'postgresql://' + f'user:{password}@host/db'
        (root / 'runtime.py').write_text(value)
        tip = commit(root)
        assert secret_scan.scan(root, local=tip, remote=base) == 1
        output = capsys.readouterr().out
        assert 'postgres-password-url' in output
        assert value not in output and password not in output


def test_postgres_fixture_allowlist_does_not_hide_new_password_or_path():
    rules = secret_scan.load_rules(ROOT)
    path = 'tests/api/test_health_ready.py'
    value = 'postgresql://' + 'worker:private-test-password@localhost/db'
    assert not list(secret_scan.findings(rules, path, value))
    assert list(secret_scan.findings(rules, 'new.py', value))
    assert list(secret_scan.findings(rules, path, value.replace('private-test-password', 'different-password')))


@pytest.mark.parametrize("legacy", [False, True])
def test_actual_hook_refuses_missing_or_pre_security_checkout(legacy):
    with tempfile.TemporaryDirectory(prefix="secret-hook-old-checkout-") as directory:
        root = Path(directory)
        init(root)
        (root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/secret_scan.py', root / 'scripts/secret_scan.py')
        (root / 'hook').write_bytes((ROOT / '.githooks/pre-push').read_bytes())
        if legacy:
            # A pre-security gate can accept a cached test pass without scanning.
            # The installed hook must reject it without executing its code.
            (root / 'scripts/local_ci.py').write_text('print("LEGACY_GATE_RAN")\nraise SystemExit(0)\n')
        token = 'AKIA' + 'Q7W8E9R0T1Y2U3I4'
        (root / 'fixture.txt').write_text(token)
        tip = commit(root)
        result = subprocess.run(['sh', str(root / 'hook')], cwd=root,
                                input=f'refs/heads/a {tip} refs/heads/a {"0" * 40}\n',
                                text=True, capture_output=True, timeout=10, check=False,
                                env={**os.environ, 'LOCAL_CI_PYTHON': sys.executable})
        assert result.returncode == 1
        assert 'checkout lacks the required security gate' in result.stderr
        assert 'LEGACY_GATE_RAN' not in result.stdout
        assert token not in result.stdout + result.stderr


def test_uncached_push_never_invokes_network_audits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_ci, 'load_records', dict)
    monkeypatch.setattr(local_ci, 'git', lambda *args: 'head')
    monkeypatch.setattr(local_ci, 'tree_of', lambda *args: 'tree')
    monkeypatch.setattr(local_ci, 'clean_checkout', lambda: True)
    monkeypatch.setattr(local_ci, 'choose_python', lambda: sys.executable)
    monkeypatch.setattr(local_ci, 'save_record', lambda *args: None)
    monkeypatch.setattr(local_ci, 'run_suites', lambda *args: ([], [], []))
    calls = []
    def run(argv, **kwargs):
        assert argv[1] == 'scripts/secret_scan.py', 'automatic push attempted network audit'
        assert kwargs['timeout'] == 120
        calls.append(argv)
    monkeypatch.setattr(local_ci, 'run', run)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('refs/heads/a head refs/heads/a remote\n'))
    assert local_ci.cmd_pre_push(argparse.Namespace()) == 0
    assert len(calls) == 2  # outgoing history and whole working tree, no pip/npm
    assert 'Dependency audit: not run on automatic push' in capsys.readouterr().out


@pytest.mark.parametrize('context', [False, True])
def test_filename_and_control_characters_are_redacted(tmp_path, capsys, context):
    init(tmp_path)
    token = 'a' * 40 if context else 'ghp_' + 'a' * 36
    text = f'CLOUDFLARE_API_TOKEN="{token}"' if context else token
    # Windows disallows control chars in filenames: test escaped metadata itself
    # on every platform and exercise a real secret-bearing filename separately.
    (tmp_path / (token + '.txt')).write_text(text)
    assert secret_scan.scan(tmp_path) == 1
    assert token not in capsys.readouterr().out
    rendered = secret_scan.safe_metadata(secret_scan.load_rules(ROOT), token + '\n\r\x1b\u202e.txt', text)
    assert token not in rendered
    assert json.loads(rendered) == '[REDACTED]\n\r\x1b\u202e.txt'
    assert '\n' not in rendered and '\r' not in rendered and '\x1b' not in rendered
    assert r'\u202e' in rendered


def test_git_timeout_fails_closed(monkeypatch, capsys):
    def hung(argv, **kwargs):
        assert kwargs['timeout'] == 15
        raise subprocess.TimeoutExpired(argv, 15)
    monkeypatch.setattr(secret_scan.subprocess, 'run', hung)
    monkeypatch.setattr(sys, 'argv', ['secret_scan.py'])
    assert secret_scan.main() == 1
    assert 'push blocked' in capsys.readouterr().out


def test_actual_hook_kills_hung_scanner():
    with tempfile.TemporaryDirectory(prefix='secret-hook-timeout-') as directory:
        root = Path(directory)
        init(root)
        (root / 'scripts').mkdir()
        gate = (ROOT / 'scripts/local_ci.py').read_text().replace('timeout=120', 'timeout=0.1')
        (root / 'scripts/local_ci.py').write_text(gate)
        (root / 'scripts/secret_scan.py').write_text('import time\ntime.sleep(60)\n')
        (root / 'hook').write_bytes((ROOT / '.githooks/pre-push').read_bytes())
        tip = commit(root)
        result = subprocess.run(['sh', str(root / 'hook')], cwd=root,
                                input=f'refs/heads/a {tip} refs/heads/a {"0" * 40}\n',
                                text=True, capture_output=True, timeout=10, check=False,
                                env={**os.environ, 'LOCAL_CI_PYTHON': sys.executable})
        assert result.returncode == 1
        assert 'timed out; push blocked' in result.stderr


def test_python_skipped_dependency_is_not_clean():
    _, failed = dependency_audit.pip_summary({"dependencies": [{"name": "local", "skip_reason": "local project"}]})
    assert failed
