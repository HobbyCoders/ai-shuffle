"""Startup latency belongs to the fake CLI, before its init/session-id frame."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from scripts.devloop import fake_claude
from scripts.devloop.fake_claude import cli


@pytest.mark.parametrize(("value", "seconds"), [("3000", 3), ("0", 0), ("-1", 0), ("bad", 0)])
def test_startup_delay_precedes_the_init_frame(monkeypatch, value, seconds):
    events = []
    monkeypatch.setenv("FAKE_CLAUDE_STARTUP_MS", value)
    monkeypatch.setattr(cli.time, "sleep", lambda duration: events.append(("sleep", duration)))
    monkeypatch.setattr(cli, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.script.Fake, "init_frame", lambda self: events.append(("init", None)) or {})
    fake = object.__new__(cli.DevClaude)
    fake.model, fake.mcp, fake.disallowed = cli.MODEL, {}, []
    frame = fake.init_frame()
    assert events == ([("sleep", seconds)] if seconds else []) + [("init", None)]
    assert frame["model"] == cli.MODEL


def test_launcher_knob_reaches_the_fake_process_without_delaying_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVLOOP_CLAUDE_STARTUP_MS", "300")
    shim = fake_claude.install(tmp_path) / "claude"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    auth = subprocess.run([str(shim), "auth", "status"], env=env, capture_output=True,
                          text=True, timeout=10, check=True)
    assert json.loads(auth.stdout)["loggedIn"] is True
    started = time.monotonic()
    run = subprocess.run([str(shim), "-p", "--input-format", "stream-json",
                          "--output-format", "stream-json"], input="", env=env,
                         capture_output=True, text=True, timeout=10, check=True)
    assert time.monotonic() - started >= 0.3
    frames = [json.loads(line) for line in run.stdout.splitlines()]
    assert frames[0]["subtype"] == "init" and frames[0]["session_id"]
    log = [json.loads(line) for line in fake_claude.log_path(tmp_path).read_text().splitlines()]
    assert [row["milliseconds"] for row in log if row["kind"] == "startup_delay"] == [300]


@pytest.mark.parametrize(("value", "plan"), [("ultra", "ultra"), ("Max", "max"), ("", "fake"), ("x y", "fake")])
def test_launcher_plan_knob_reaches_the_cli_status_probe(tmp_path, monkeypatch, value, plan):
    from app.core import claude_cli_status

    monkeypatch.setenv("DEVLOOP_CLAUDE_PLAN", value)
    shim = fake_claude.install(tmp_path) / "claude"
    status = claude_cli_status.probe(resolver=lambda: str(shim))
    assert status.signed_in is True and status.plan == plan
