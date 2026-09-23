"""The in-app terminal that runs the stock CLI's `claude auth login`."""

import os
import stat
import sys
import time

import pytest

from app.core import claude_cli_login as cl

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY is Unix only")

# Stands in for the CLI: prints a link (inside a terminal hyperlink escape, as
# the CLI does), waits for a line, reports whether it was echoed back.
FAKE_CLI = r'''#!/bin/sh
if [ "$1 $2" != "auth login" ]; then echo "unexpected: $*"; exit 9; fi
printf 'Opening browser to sign in\n'
printf 'If the browser did not open, visit: \033]8;;https://claude.com/cai/oauth/authorize?code=true&state=abc\007https://claude.com/cai/oauth/authorize?code=true&state=abc\033]8;;\007\n'
printf 'Paste code here if prompted > '
read code
if [ "$code" = "good#state" ]; then echo "Login successful."; exit 0; fi
echo "Login failed: bad code"; exit 1
'''


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _until(term, pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    state = term.read(0)
    while not pred(state) and time.monotonic() < deadline:
        time.sleep(0.05)
        state = term.read(0)
    return state


def test_clean_terminal_text():
    raw = "\x1b[1mBold\x1b[0m \x1b]8;;https://x.test\x07link\x1b]8;;\x07\r\nnext\rline"
    assert cl.clean_terminal_text(raw) == "Bold link\nnext\nline"


def test_login_success_invalidates_probe(fake_cli, monkeypatch):
    invalidated = []
    monkeypatch.setattr(cl.claude_cli_status, "invalidate", lambda: invalidated.append(1))
    monkeypatch.delenv("SHUFFLE_DESKTOP", raising=False)
    term = cl.LoginTerminal()
    first = term.start(binary=fake_cli)
    assert first["running"] and first["supported"]
    state = _until(term, lambda s: s["url"] and "Paste code" in s["output"])
    assert state["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    assert "\x1b" not in state["output"]

    # A second start while it runs returns the same login.
    assert term.start(binary=fake_cli)["running"]

    term.send("good#state")
    state = _until(term, lambda s: not s["running"])
    assert state["succeeded"] is True and state["exit_code"] == 0
    assert "Login successful." in state["output"]
    # Echo is off: the code typed in never appears in the kept output.
    assert "good#state" not in state["output"]
    assert invalidated


def test_login_failure_and_offsets(fake_cli, monkeypatch):
    monkeypatch.delenv("SHUFFLE_DESKTOP", raising=False)
    term = cl.LoginTerminal()
    term.start(binary=fake_cli)
    state = _until(term, lambda s: "Paste code" in s["output"])
    offset = state["offset"]
    term.send("nope")
    state = _until(term, lambda s: not s["running"])
    tail = term.read(offset)
    assert tail["output"].strip() == "Login failed: bad code"
    assert state["succeeded"] is False


def test_input_rules(fake_cli, monkeypatch):
    monkeypatch.delenv("SHUFFLE_DESKTOP", raising=False)
    term = cl.LoginTerminal()
    with pytest.raises(RuntimeError):
        term.send("x")
    term.start(binary=fake_cli)
    with pytest.raises(ValueError):
        term.send("x" * (cl.MAX_INPUT_CHARS + 1))
    state = term.cancel()
    assert state["active"] is False and state["output"] == ""


def test_not_offered_on_desktop(monkeypatch, fake_cli):
    monkeypatch.setenv("SHUFFLE_DESKTOP", "1")
    assert cl.terminal_supported() is False
    with pytest.raises(RuntimeError, match="claude auth login"):
        cl.LoginTerminal().start(binary=fake_cli)


def test_missing_cli_raises_setup_error(monkeypatch):
    monkeypatch.delenv("SHUFFLE_DESKTOP", raising=False)
    with pytest.raises(RuntimeError):
        cl.LoginTerminal().start()


def test_login_environment_has_no_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    env = cl._login_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["TERM"] == "dumb"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
