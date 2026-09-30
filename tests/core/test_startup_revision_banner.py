"""The startup log lines name the source revision the worker runs.

The app version spans many commits, so a stale build can only be told apart
by its revision. In-process only: the subprocess tests in test_file_logging.py
are flaky and are not needed here.
"""

from __future__ import annotations

import logging

import pytest

from app.core import file_logging


@pytest.fixture
def no_revision_env(monkeypatch):
    monkeypatch.delenv("SHUFFLE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)


def _banner(caplog) -> str:
    with caplog.at_level(logging.INFO, logger=file_logging.logger.name):
        file_logging.log_startup_banner()
    [line] = [r.getMessage() for r in caplog.records if "AI Shuffle starting" in r.getMessage()]
    return line


def test_the_banner_names_the_desktop_revision(no_revision_env, monkeypatch, caplog):
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "5944e547487b59d843b9e7fe1c3c8a7c23de4ccb")
    monkeypatch.setenv("GIT_SHA", "not-this-one")

    line = _banner(caplog)

    assert "revision=5944e547487b59d843b9e7fe1c3c8a7c23de4ccb" in line
    assert "not-this-one" not in line


def test_the_banner_falls_back_to_the_image_sha(no_revision_env, monkeypatch, caplog):
    monkeypatch.setenv("GIT_SHA", "dcf7aadef")

    assert "revision=dcf7aadef" in _banner(caplog)


def test_the_banner_says_unknown_without_either_variable(no_revision_env, caplog):
    assert "revision=unknown" in _banner(caplog)


def test_a_blank_variable_does_not_count(no_revision_env, monkeypatch):
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "  ")

    assert file_logging.source_revision() == "unknown"

