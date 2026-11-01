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
                 version="2.1.280 (Claude Code)", json_flag=True, status_err="", text_out=None, text_code=0):
        self.calls = []
        self.status_out, self.status_code, self.status_err = status_out, status_code, status_err
        self.auth_help, self.version, self.json_flag = auth_help, version, json_flag
        # ``auth status --text``: None answers like the plain command.
        self.text_out, self.text_code = text_out, text_code

    def __call__(self, argv):
        self.calls.append(argv[1:])
        if argv[1:] == ["--version"]:
            return _done(argv, self.version + "\n")
        if argv[1:] == ["auth", "--help"]:
            return _done(argv, self.auth_help)
        if argv[1:] == ["auth", "status", "--text"] and self.text_out is not None:
            if isinstance(self.text_out, BaseException):
                raise self.text_out
            return _done(argv, self.text_out, "", self.text_code)
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


def test_probe_home_asks_about_that_folder_only(monkeypatch):
    """A saved sign-in's identity comes from `claude auth status` in its own folder."""
    cli = FakeCli(status_out=json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                         "email": "second@example.com", "subscriptionType": "max"}))
    folders = []

    def run(argv, env=None):
        folders.append((env or {}).get("CLAUDE_CONFIG_DIR", "<active>"))
        return cli(argv)
    monkeypatch.setattr(cs, "_run", run)
    cs._auth_support.clear()
    cs.invalidate()
    status = cs.probe_home({"CLAUDE_CONFIG_DIR": "/data/claude-logins/0123456789abcdef"},
                           resolver=lambda: "/home/appuser/.local/bin/claude")
    assert status.signed_in is True and status.email == "second@example.com" and status.plan == "max"
    assert set(folders) == {"/data/claude-logins/0123456789abcdef"}
    assert cs.peek() is None  # the shared cache keeps describing the active sign-in


def test_probe_environment_follows_the_active_sign_in(monkeypatch):
    from app.core import claude_subscriptions
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude_subscriptions, "active_home_env",
                        lambda: {"CLAUDE_CONFIG_DIR": "/data/claude-logins/0123456789abcdef"})
    assert cs._probe_env()["CLAUDE_CONFIG_DIR"] == "/data/claude-logins/0123456789abcdef"
    # An explicit default folder leaves the worker's own setting alone.
    assert "CLAUDE_CONFIG_DIR" not in cs._probe_env({})


# ─── Plan tier from `claude auth status --text` ─────────────────────────────

_MAX_JSON = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com",
                        "subscriptionType": "max"})


def _text(login_method):
    return f"Login method: {login_method}\nOrganization: Me\nEmail: me@example.com\n"


@pytest.mark.parametrize("line,tier", [
    ("Login method: Claude Ultra account", "ultra"),
    ("Login method: Claude Max account", "max"),
    ("  login method:  claude PRO Account  ", "pro"),
    ("Login method: Claude account", None),
    ("Login method: API key", None),
    ("Plan: Claude Ultra account", None),
    ("", None),
])
def test_parse_text_tier(line, tier):
    assert cs.parse_text_tier(f"Claude Code status\n{line}\nEmail: me@example.com") == tier


def test_ultra_tier_replaces_the_json_max(fake):
    """JSON says "max" for Max and Ultra alike; the text form names Ultra."""
    cli = fake(status_out=_MAX_JSON, text_out=_text("Claude Ultra account"))
    status = _probe()
    assert ["auth", "status", "--text"] in cli.calls
    assert status.plan == "ultra"
    assert status.auth_method_label == "Claude account"  # the label stays the method's
    assert status.to_dict()["plan"] == "ultra"


def test_max_tier_stays_max(fake):
    fake(status_out=_MAX_JSON, text_out=_text("Claude Max account"))
    assert _probe().plan == "max"


@pytest.mark.parametrize("text_out,text_code", [
    ("Login method: Claude account\n", 0),                    # no tier in the line
    ("something new entirely", 0),                             # another shape
    ("Login method: Claude Ultra account\n", 2),              # the run failed
    (subprocess.TimeoutExpired(["claude"], 15), 0),           # it timed out
    (OSError("gone"), 0),
])
def test_unreadable_text_probe_keeps_the_json_plan(fake, text_out, text_code):
    fake(status_out=_MAX_JSON, text_out=text_out, text_code=text_code)
    status = _probe()
    assert status.plan == "max" and status.signed_in is True and status.error is None


@pytest.mark.parametrize("payload", [
    {"loggedIn": False, "authMethod": "none"},
    {"loggedIn": True, "authMethod": "oauth_token", "subscriptionType": "max"},
    {"loggedIn": True, "authMethod": "api_key"},
])
def test_text_probe_runs_only_for_a_claude_ai_sign_in(fake, payload):
    cli = fake(status_out=json.dumps(payload), text_out=_text("Claude Ultra account"),
               status_code=0 if payload["loggedIn"] else 1)
    status = _probe()
    assert ["auth", "status", "--text"] not in cli.calls
    assert status.plan != "ultra"


def test_text_only_cli_reads_the_tier(fake):
    fake(json_flag=False, status_out=_text("Claude Ultra account"))
    status = _probe()
    assert status.plan == "ultra" and status.auth_method_label == "Claude Ultra account"


def test_tier_shares_the_status_cache(fake, monkeypatch):
    """One probe per cache period: the text run does not add a probe per read."""
    cli = fake(status_out=_MAX_JSON, text_out=_text("Claude Ultra account"))
    monkeypatch.setattr(cs, "_default_resolver", lambda: "/home/appuser/.local/bin/claude")
    monkeypatch.setattr(cs, "_announce_account_change", lambda: None)
    cs.invalidate()
    try:
        assert cs.get_status().plan == "ultra"
        assert cs.get_status().plan == "ultra"
        assert cli.calls.count(["auth", "status", "--text"]) == 1
    finally:
        cs.invalidate()


def test_saved_sign_in_reads_its_tier(monkeypatch):
    cli = FakeCli(status_out=_MAX_JSON, text_out=_text("Claude Ultra account"))
    monkeypatch.setattr(cs, "_run", lambda argv, env=None: cli(argv))
    cs._auth_support.clear()
    status = cs.probe_home({"CLAUDE_CONFIG_DIR": "/data/claude-logins/0123456789abcdef"},
                           resolver=lambda: "/home/appuser/.local/bin/claude")
    assert status.plan == "ultra"
