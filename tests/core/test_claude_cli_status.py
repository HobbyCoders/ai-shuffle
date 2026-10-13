"""The CLI status probe: `claude auth status` parsed without reading any credential file."""

import json
import subprocess
import time

import pytest

from app.core import claude_cli_status as cs


def _done(argv, stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeCli:
    """Answers the three commands the probe runs."""

    def __init__(self, status_out="", status_code=0, auth_help="Commands:\n  login\n  logout\n  status",
                 version="2.1.280 (Claude Code)", json_flag=True, status_err=""):
        self.calls = []
        self.status_out, self.status_code, self.status_err = status_out, status_code, status_err
        self.auth_help, self.version, self.json_flag = auth_help, version, json_flag

    def __call__(self, argv):
        self.calls.append(argv[1:])
        if argv[1:] == ["--version"]:
            return _done(argv, self.version + "\n")
        if argv[1:] == ["auth", "--help"]:
            return _done(argv, self.auth_help)
        if argv[1:] == ["auth", "status", "--json"] and not self.json_flag:
            return _done(argv, "", "error: unknown option '--json'", 1)
        if argv[1:3] == ["auth", "status"]:
            return _done(argv, self.status_out, self.status_err, self.status_code)
        raise AssertionError(argv)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        cli = FakeCli(**kw)
        monkeypatch.setattr(cs, "_run", cli)
        cs._auth_support.clear()
        return cli
    return install


def _probe():
    return cs.probe(resolver=lambda: "/home/appuser/.local/bin/claude")


def test_signed_in_claude_account(fake):
    fake(status_out=json.dumps({
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
        "email": "me@example.com", "orgName": "Me", "subscriptionType": "max",
        "configDirectory": "/home/appuser/.claude", "somethingNew": [1, 2],
    }))
    status = _probe()
    assert status.installed and status.signed_in is True
    assert status.email == "me@example.com"
    assert status.plan == "max"
    assert status.org_name == "Me"
    assert status.auth_method == "claude.ai"
    assert status.auth_method_label == "Claude account"
    assert status.cli_version == "2.1.280"
    assert status.binary_path == "/home/appuser/.local/bin/claude"
    assert status.error is None


def test_not_signed_in_exit_code_one(fake):
    fake(status_out=json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}),
         status_code=1)
    status = _probe()
    assert status.installed and status.signed_in is False
    assert status.email is None and status.error is None


def test_key_in_environment_is_not_a_cli_login(fake):
    fake(status_out=json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}))
    assert _probe().signed_in is False


def test_banner_around_json_is_tolerated(fake):
    fake(status_out="Update available!\n" + json.dumps({"loggedIn": True, "authMethod": "oauth_token"}) + "\n")
    status = _probe()
    assert status.signed_in is True
    assert status.auth_method_label == "Claude account token"


def test_cli_without_json_flag_falls_back_to_text(fake):
    cli = fake(json_flag=False, status_out="Login method: Claude Max account\nEmail: me@example.com\n")
    status = _probe()
    assert ["auth", "status"] in cli.calls
    assert status.signed_in is True
    assert status.email == "me@example.com"
    assert status.auth_method_label == "Claude Max account"


def test_text_not_logged_in(fake):
    fake(json_flag=False, status_out="Not logged in. Run claude auth login to authenticate.", status_code=1)
    assert _probe().signed_in is False


def test_unreadable_answer_uses_exit_code(fake):
    fake(status_out="???", status_code=0)
    assert _probe().signed_in is True


def test_unreadable_answer_with_odd_exit_code_reports_error(fake):
    fake(json_flag=False, status_out="???", status_code=3)
    status = _probe()
    assert status.signed_in is None
    assert "could not read" in status.error


def test_cli_without_auth_command(fake):
    cli = fake(auth_help="Usage: claude [options]")
    status = _probe()
    assert status.installed and status.signed_in is None
    assert "auth status" in status.error
    assert not any(c[:2] == ["auth", "status"] for c in cli.calls)


def test_missing_binary():
    def missing():
        raise RuntimeError("Claude Code CLI not found. Install it with Anthropic's installer.")
    status = cs.probe(resolver=missing)
    assert status.installed is False and status.signed_in is None
    assert "not found" in status.error


def test_binary_that_cannot_start(monkeypatch):
    def boom(argv):
        raise PermissionError("denied")
    monkeypatch.setattr(cs, "_run", boom)
    status = _probe()
    assert status.installed is False
    assert "could not be started" in status.error


def test_timeout(fake, monkeypatch):
    cli = fake()
    real = cli.__call__

    def slow(argv):
        if argv[1:3] == ["auth", "status"]:
            raise subprocess.TimeoutExpired(argv, 15)
        return real(argv)
    monkeypatch.setattr(cs, "_run", slow)
    status = _probe()
    assert "did not answer" in status.error


def test_probe_environment_drops_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    env = cs._probe_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_probe_environment_keeps_the_hosts_claude_oauth_token(monkeypatch):
    """`claude auth status` is the stock CLI: it may see the subscription token
    it would run on (#2079), unlike every other child."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-oauth")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    env = cs._probe_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "host-oauth"
    assert "ADMIN_PASSWORD" not in env


def test_cache_positive_and_negative(monkeypatch):
    results = iter([cs.ClaudeCliStatus(installed=True, signed_in=False),
                    cs.ClaudeCliStatus(installed=True, signed_in=True),
                    cs.ClaudeCliStatus(installed=True, signed_in=False)])
    calls = []

    def fake_probe(resolver=None):
        calls.append(1)
        return next(results)
    monkeypatch.setattr(cs, "probe", fake_probe)
    clock = [1000.0]
    monkeypatch.setattr(cs.time, "monotonic", lambda: clock[0])

    assert cs.get_status().signed_in is False
    clock[0] += 1
    assert cs.get_status().signed_in is False           # negative answer cached briefly
    clock[0] += cs.NEGATIVE_TTL_SECONDS
    assert cs.get_status().signed_in is True             # re-probed
    clock[0] += cs.POSITIVE_TTL_SECONDS - 1
    assert cs.get_status().signed_in is True             # positive answer held longer
    assert cs.get_status(force=True).signed_in is False  # Recheck bypasses the cache
    assert len(calls) == 3
    cs.invalidate()
    assert cs.peek() is None


def test_probe_never_opens_credentials_file(fake, monkeypatch, tmp_path):
    """The probe learns everything from the CLI; it opens no file itself."""
    import builtins
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    fake(status_out=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    _probe()
    assert opened == []


def test_real_cli_if_present():
    """Smoke test against a real `claude` on PATH, when the machine has one."""
    import shutil
    binary = shutil.which("claude")
    if not binary:
        pytest.skip("no claude on PATH")
    started = time.monotonic()
    status = cs.probe(resolver=lambda: binary)
    assert time.monotonic() - started < 30
    assert status.installed is True
    assert status.cli_version
    assert status.signed_in in (True, False, None)
