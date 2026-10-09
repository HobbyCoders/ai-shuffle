"""BRIEF section 5 verification: the three compliance integration tests (C14).

1. A fresh install with no credentials refuses every agent run with a clear
   setup message and never starts a CLI.
2. A subscription-mode run goes through the stock ``claude`` CLI found on PATH,
   and the worker process never opens or stats the CLI's own credential file
   (``$CLAUDE_CONFIG_DIR/.credentials.json``); no Anthropic API key or token
   reaches the CLI, even when a key is stored in Settings or set on the host.
3. An API-key (BYOK) run hands the CLI exactly the user's stored key and no
   other Anthropic credential.
4. An explicit worker gateway token selects the CLI's built-in gateway auth
   even when subscription/API-key mode was previously saved (#1862); these
   credentials never reach app-owned child environments.

Tests 2 and 3 run a real turn through the real pump
(``query_engine._stream_to_websocket_impl``), the fluid harness adapter and the
delegated loop, against a fake ``claude`` executable put first on PATH (on
Windows, where an extension-less file cannot be found on PATH, the adapter
runs the fake directly). The fake records its argv and environment, then speaks the stream-json protocol
(``fluid/tests/fixtures/fake_claude.py``). The worker's file access is audited
with a Python audit hook (``open``, directory listings) plus wrappers around
``os.stat``, ``os.lstat`` and ``os.access`` for the duration of the turn.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

import pytest

from app.core import claude_auth_mode, claude_cli_status, credential_gate
from app.db import database

pytestmark = pytest.mark.real_agent_gate

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "fluid" / "tests" / "fixtures" / "fake_claude.py"

USER_KEY = "sk-ant-api03-users-own-key-for-this-test-7Q2Z"
HOST_KEY = "sk-ant-api03-host-env-key-must-not-leak-0000"
HOST_TOKEN = "host-auth-token-must-not-leak"
HOST_OAUTH = "host-oauth-token-must-not-leak"

CREDENTIAL_NAME = re.compile(
    r"(?:_SECRET|_TOKEN|_PASSWORD|_PASSWD|_API_KEY|_PRIVATE_KEY|_ACCESS_KEY|_CREDENTIALS)$", re.I
)
ANTHROPIC_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


# ---------------------------------------------------------------------------
# File-access audit of the worker process
# ---------------------------------------------------------------------------

class _Audit:
    """Paths the worker process opened, listed or stat'ed while active."""

    def __init__(self) -> None:
        self.active = False
        self.paths: list[str] = []
        self.lock = threading.Lock()

    def record(self, value) -> None:
        if not self.active:
            return
        try:
            path = os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else None
        except TypeError:
            path = None
        if path:
            with self.lock:
                self.paths.append(path)

    def touched(self, target: Path) -> list[str]:
        # Resolving paths stats them: pause recording while this runs.
        was, self.active = self.active, False
        try:
            wanted = {str(target), os.path.realpath(target)}
            return [p for p in list(self.paths) if p in wanted or os.path.realpath(p) in wanted
                    or p.endswith(".credentials.json")]
        finally:
            self.active = was


_AUDIT = _Audit()
_HOOKED = False


def _hook(event: str, args: tuple) -> None:
    if not _AUDIT.active:
        return
    if event in ("open", "os.listdir", "os.scandir", "os.chmod", "os.remove", "os.rename",
                 "shutil.copyfile", "os.utime") and args:
        _AUDIT.record(args[0])


@pytest.fixture
def file_audit(monkeypatch):
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_hook)
        _HOOKED = True

    def _wrap(fn):
        def wrapper(path, *a, **k):
            _AUDIT.record(path)
            return fn(path, *a, **k)
        return wrapper

    monkeypatch.setattr(os, "stat", _wrap(os.stat))
    monkeypatch.setattr(os, "lstat", _wrap(os.lstat))
    monkeypatch.setattr(os, "access", _wrap(os.access))
    _AUDIT.paths.clear()
    _AUDIT.active = True
    try:
        yield _AUDIT
    finally:
        _AUDIT.active = False


# ---------------------------------------------------------------------------
# A fake stock CLI on PATH
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    return _install_fake_cli(tmp_path, monkeypatch)


def _install_fake_cli(tmp_path, monkeypatch) -> dict:
    """Put a recording ``claude`` first on PATH, make it the only CLI the
    adapter can resolve, and give the CLI a config dir holding a credentials
    file the worker must never touch."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "cli-calls.jsonl"
    recorder = tmp_path / "record_and_run.py"
    recorder.write_text(
        "import json, os, runpy, sys\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
        f"sys.argv[0] = {str(FAKE)!r}\n"
        f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
    )
    # A shell wrapper, so a long interpreter path never hits the shebang limit.
    script = bin_dir / "claude"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{recorder}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    credentials = config_dir / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "cli-owned-token"}}))

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # On POSIX the adapter's own PATH search must land on the wrapper above.
    # On Windows ``shutil.which`` skips a file without a PATHEXT extension, so
    # the search would walk on to the machine's real ``claude.exe``; there the
    # recorder runs directly. Either way, any other result fails the test
    # instead of starting a real CLI.
    from app.core.provider import harness_adapter as ha

    real_resolve = ha._resolve_claude_cmd

    def resolve(**kwargs) -> list:
        if os.name == "nt":
            return [sys.executable, str(recorder)]
        found = real_resolve(**kwargs)
        assert found == [str(script)], f"resolved {found}, not the fake CLI"
        return found

    monkeypatch.setattr(ha, "_resolve_claude_cmd", resolve)
    assert ha._resolve_claude_cmd()[-1] in (str(script), str(recorder))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "a")
    # Start without ambient credentials or CLI routing selectors, so the fake
    # turn cannot inherit a real worker's cloud configuration.
    from fluid.tools.child_env import is_claude_auth

    for name in list(os.environ):
        if CREDENTIAL_NAME.search(name) or is_claude_auth(name):
            monkeypatch.delenv(name, raising=False)

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    return {"calls": calls, "credentials": credentials, "config_dir": config_dir,
            "script": script, "recorder": recorder}


def _cli_status(signed_in: bool):
    return claude_cli_status.ClaudeCliStatus(
        installed=True, signed_in=signed_in, auth_method="claude.ai" if signed_in else "none",
        email="owner@example.com" if signed_in else None, cli_version="2.0.0",
    )


def _seed(tmp_path, sid: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    database.create_profile("c14-profile", "C14", None, {"cwd": str(workspace)})
    database.create_session(session_id=sid, profile_id="c14-profile", title="c14")


async def _run_turn(sid: str, audit=None) -> list[dict]:
    """One real turn through the pump; the pooled CLI is closed afterwards."""
    from app.core import query_engine as qe

    frames: list[dict] = []
    try:
        async for frame in qe._stream_to_websocket_impl(prompt="hello", session_id=sid, profile_id="c14-profile"):
            frames.append(frame)
    finally:
        if audit is not None:
            audit.active = False
        pool = qe.get_session_pool()
        if pool.get(sid) is not None:
            await pool.remove(sid)
    return frames


def _turn_calls(calls: list[dict]) -> list[dict]:
    """CLI invocations that ran a stream-json session (not ``--version``)."""
    return [c for c in calls if "--version" not in c["argv"]]


# ---------------------------------------------------------------------------
# 1. Fresh install: blocked with a clear message
# ---------------------------------------------------------------------------

async def test_fresh_install_without_credentials_is_blocked_with_a_clear_message(
    mock_db, tmp_path, fake_cli, monkeypatch
):
    """Nothing configured: the CLI is installed but not signed in, no key is
    stored. Startup records the blocking setup state, the status says why, and
    a turn is refused with that message without ever starting the CLI for a
    session."""
    from app.core import startup

    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli_status(False))
    startup.check_prerequisites()
    assert claude_auth_mode.stored_mode() == "subscription"
    assert credential_gate.startup_state == "setup_required"
    assert credential_gate.agent_credentials_ready() == (False, credential_gate.NOT_SIGNED_IN_MESSAGE)

    _seed(tmp_path, "c14-fresh")
    frames = await _run_turn("c14-fresh")
    assert frames == [credential_gate.refusal_event(credential_gate.NOT_SIGNED_IN_MESSAGE)]
    assert "claude auth login" in frames[0]["message"]
    assert _turn_calls(fake_cli["calls"]()) == []


# ---------------------------------------------------------------------------
# 2. Subscription mode: stock CLI, no token file touched, no key handed over
# ---------------------------------------------------------------------------

async def test_subscription_run_touches_no_token_file_and_hands_over_no_key(
    mock_db, tmp_path, fake_cli, file_audit, monkeypatch
):
    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli_status(True))
    claude_auth_mode.set_mode("subscription")
    # Both credentials present: a stored key and host variables. The explicit
    # subscription choice must win and none of them may reach the CLI.
    database.set_system_setting("anthropic_api_key", USER_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", HOST_KEY)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    _seed(tmp_path, "c14-sub")

    frames = await _run_turn("c14-sub", audit=file_audit)

    assert [f for f in frames if f.get("type") == "error"] == []
    assert any(f.get("type") == "done" for f in frames), frames
    runs = _turn_calls(fake_cli["calls"]())
    assert len(runs) == 1, "the turn must run through the claude CLI found on PATH"
    child = runs[0]["env"]
    assert "ANTHROPIC_API_KEY" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert not {USER_KEY, HOST_KEY, HOST_TOKEN} & set(child.values())
    assert "--output-format" in runs[0]["argv"] and "stream-json" in runs[0]["argv"]

    assert file_audit.paths, "the audit hook recorded nothing; it is not wired"
    assert file_audit.touched(fake_cli["credentials"]) == []


async def test_subscription_host_oauth_token_reaches_only_the_cli(
    mock_db, tmp_path, fake_cli, file_audit, monkeypatch
):
    """#2079: a worker-set CLAUDE_CODE_OAUTH_TOKEN goes to the stock CLI's turn
    and to no other child (process tree, hooks, git)."""
    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli_status(True))
    claude_auth_mode.set_mode("subscription")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", HOST_OAUTH)
    _seed(tmp_path, "c14-sub-oauth")

    frames = await _run_turn("c14-sub-oauth", audit=file_audit)

    assert [f for f in frames if f.get("type") == "error"] == []
    runs = _turn_calls(fake_cli["calls"]())
    assert len(runs) == 1
    assert runs[0]["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == HOST_OAUTH
    from app.core.hook_executor import hook_base_env
    from app.core.process_tree import child_env
    from fluid.tools.child_env import scrub_child_env
    for env in (child_env(), hook_base_env(), scrub_child_env()):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and HOST_OAUTH not in env.values()


# ---------------------------------------------------------------------------
# 3. API key mode: the user's key and nothing else
# ---------------------------------------------------------------------------

async def test_byok_run_uses_only_the_users_key(mock_db, tmp_path, fake_cli, file_audit, monkeypatch):
    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli_status(True))
    claude_auth_mode.set_mode("api_key")
    database.set_system_setting("anthropic_api_key", USER_KEY)
    # OAuth cannot ride along. An explicit worker auth token instead selects
    # gateway mode (#1862), covered separately below.
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", HOST_OAUTH)
    _seed(tmp_path, "c14-byok")

    frames = await _run_turn("c14-byok", audit=file_audit)

    assert [f for f in frames if f.get("type") == "error"] == []
    assert any(f.get("type") == "done" for f in frames), frames
    runs = _turn_calls(fake_cli["calls"]())
    assert len(runs) == 1
    child = runs[0]["env"]
    assert child.get("ANTHROPIC_API_KEY") == USER_KEY
    credential_like = sorted(n for n in child if CREDENTIAL_NAME.search(n) or n in ANTHROPIC_CREDENTIALS)
    assert credential_like == ["ANTHROPIC_API_KEY"]
    assert not {HOST_TOKEN, HOST_OAUTH} & set(child.values())
    assert file_audit.touched(fake_cli["credentials"]) == []


@pytest.mark.parametrize("saved_mode", ["subscription", "api_key"])
async def test_explicit_worker_gateway_reaches_only_the_cli(
    mock_db, tmp_path, fake_cli, file_audit, monkeypatch, saved_mode,
):
    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli_status(False))
    claude_auth_mode.set_mode(saved_mode)
    database.set_system_setting("anthropic_api_key", USER_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fake-gateway.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", HOST_TOKEN)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", HOST_OAUTH)
    _seed(tmp_path, "c14-gateway")

    frames = await _run_turn("c14-gateway", audit=file_audit)

    assert not [f for f in frames if f.get("type") == "error"]
    assert any(f.get("type") == "done" for f in frames), frames
    runs = _turn_calls(fake_cli["calls"]())
    assert len(runs) == 1
    child = runs[0]["env"]
    assert child["ANTHROPIC_BASE_URL"] == "https://fake-gateway.invalid"
    assert child["ANTHROPIC_AUTH_TOKEN"] == HOST_TOKEN
    assert "ANTHROPIC_API_KEY" not in child
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
    assert USER_KEY not in child.values()
    assert file_audit.touched(fake_cli["credentials"]) == []
    from app.core.process_tree import child_env
    assert "ANTHROPIC_AUTH_TOKEN" not in child_env()
    assert "ANTHROPIC_BASE_URL" not in child_env()
