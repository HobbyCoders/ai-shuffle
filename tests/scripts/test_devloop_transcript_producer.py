"""Explicit opt-in for dark production in the controlled disposable app environment."""
from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest

from scripts.devloop import __main__ as devloop


@pytest.mark.parametrize("opt_in", [False, True])
def test_producer_environment_is_explicit_and_does_not_inherit_worker_settings(
        tmp_path, monkeypatch, opt_in):
    monkeypatch.setenv("AI_SHUFFLE_TRANSCRIPT_PRODUCER", "1")
    args = SimpleNamespace(host=ipaddress.ip_address("127.0.0.1"), browser_host=None,
                           ttl_seconds=60, transcript_producer=opt_in)
    env = devloop.application_environment(args, tmp_path, 18880, 18881, extra={})

    assert env.get("AI_SHUFFLE_TRANSCRIPT_PRODUCER") == ("1" if opt_in else None)
    assert env["AI_SHUFFLE_PREVIEW"] == "1"
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["AI_SHUFFLE_ENV_FILE"] == str(tmp_path / "empty.env")


@pytest.mark.parametrize("opt_in", [False, True])
def test_producer_cli_defaults_off_even_if_the_host_is_opted_in(monkeypatch, opt_in):
    monkeypatch.setenv("AI_SHUFFLE_TRANSCRIPT_PRODUCER", "1")
    seen = []
    monkeypatch.setattr(devloop, "up", lambda parser, args: seen.append(args) or 0)

    assert devloop.main(["up", *(["--transcript-producer"] if opt_in else [])]) == 0
    assert seen[0].transcript_producer is opt_in
