"""The startup log lines name the source revision the worker runs.

The app version spans many commits, so a stale build can only be told apart
by its revision. In-process only: the subprocess tests in test_file_logging.py
are flaky and are not needed here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core import file_logging

CFCA = "cfca3e670c88d12e71487ca4dd49019c677aa4f6"


@pytest.fixture
def no_revision_env(monkeypatch, tmp_path):
    """No revision variable, and code that sits in a tmp tree with no stamp.

    Returns the tmp ``server`` dir standing in for the one that holds ``app``.
    """
    monkeypatch.delenv("SHUFFLE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    server = tmp_path / "worker" / "server"
    server.mkdir(parents=True)
    monkeypatch.setattr(file_logging, "_APP_ROOT", server)
    return server


def _banner(caplog) -> str:
    with caplog.at_level(logging.INFO, logger=file_logging.logger.name):
        file_logging.log_startup_banner()
    [line] = [r.getMessage() for r in caplog.records if "AI Shuffle starting" in r.getMessage()]
    return line


def _stamp_desktop_build(server: Path, content: str) -> None:
    (server.parent / "desktop-build.json").write_text(content, encoding="utf-8")


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


def test_the_packaged_desktop_baseline_names_its_stamped_revision(no_revision_env, caplog):
    # installed-entry.ts starts the bundled worker without SHUFFLE_SOURCE_REVISION,
    # so resources/worker/desktop-build.json is the only record of the build.
    _stamp_desktop_build(no_revision_env, json.dumps({"revision": CFCA, "sequence": 1789097118000}))

    assert f"revision={CFCA}" in _banner(caplog)


def test_the_variable_still_wins_over_the_stamp(no_revision_env, monkeypatch):
    # A staged source build is launched with the variable; the stamp next to
    # the installed baseline would name the wrong build.
    _stamp_desktop_build(no_revision_env, json.dumps({"revision": CFCA}))
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "5944e547487b59d843b9e7fe1c3c8a7c23de4ccb")

    assert file_logging.source_revision() == "5944e547487b59d843b9e7fe1c3c8a7c23de4ccb"


def test_the_container_sha_file_is_read_when_the_variable_is_missing(no_revision_env):
    (no_revision_env / "GIT_SHA").write_text("dcf7aadef\n", encoding="utf-8")

    assert file_logging.source_revision() == "dcf7aadef"


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps(["a list"]),
    json.dumps({"sequence": 1}),
    json.dumps({"revision": "not a sha; rm -rf /"}),
    "",
])
def test_a_malformed_stamp_is_ignored(no_revision_env, content):
    _stamp_desktop_build(no_revision_env, content)

    assert file_logging.source_revision() == "unknown"


def test_a_missing_stamp_dir_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("SHUFFLE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)

    assert file_logging.source_revision(tmp_path / "gone" / "server") == "unknown"


def test_linux_macos_packaged_revision_reaches_the_banner(no_revision_env, caplog):
    (no_revision_env.parent / "build-source.json").write_text(json.dumps({
        "version": "0.9.1", "sourceCommit": CFCA, "buildUrl": "https://example.com/build",
    }), encoding="utf-8")

    assert f"revision={CFCA}" in _banner(caplog)


@pytest.mark.parametrize("preferred", ["SHUFFLE_SOURCE_REVISION", "GIT_SHA", "desktop-build", "sha-file"])
def test_build_source_is_the_last_fallback(no_revision_env, monkeypatch, preferred):
    (no_revision_env.parent / "build-source.json").write_text(json.dumps({"sourceCommit": CFCA}))
    revision = "a" * 40
    if preferred in ("SHUFFLE_SOURCE_REVISION", "GIT_SHA"):
        monkeypatch.setenv(preferred, revision)
    elif preferred == "desktop-build":
        _stamp_desktop_build(no_revision_env, json.dumps({"revision": revision}))
        (no_revision_env / "GIT_SHA").write_text("b" * 40)
    else:
        (no_revision_env / "GIT_SHA").write_text(revision)

    assert file_logging.source_revision() == revision


@pytest.mark.parametrize("content", [
    b"{not json", b"[]", b"null", b'{}', b'{"revision": "cfca3e6"}',
    b'{"sourceCommit": "not-a-sha"}', b'{"sourceCommit": "abcdef"}',
    json.dumps({"sourceCommit": "a" * 65}).encode(), b"", b"\xff",
])
def test_a_malformed_build_source_is_ignored(no_revision_env, content):
    (no_revision_env.parent / "build-source.json").write_bytes(content)

    assert file_logging.source_revision() == "unknown"


@pytest.mark.parametrize("content", ["{not json", json.dumps({"revision": "bad"})])
def test_malformed_earlier_stamps_allow_build_source_fallback(no_revision_env, content):
    _stamp_desktop_build(no_revision_env, content)
    (no_revision_env / "GIT_SHA").write_text("not-a-sha")
    (no_revision_env.parent / "build-source.json").write_text(json.dumps({"sourceCommit": CFCA}))

    assert file_logging.source_revision() == CFCA


def test_build_source_is_read_only_from_the_worker_root(no_revision_env):
    content = json.dumps({"sourceCommit": CFCA})
    (no_revision_env / "build-source.json").write_text(content)
    (no_revision_env.parent.parent / "build-source.json").write_text(content)

    assert file_logging.source_revision() == "unknown"


def test_the_default_root_is_the_dir_that_holds_the_app_package():
    # server/app/core/file_logging.py maps to server/, so a packaged install
    # reads resources/worker/desktop-build.json.
    here = Path(file_logging.__file__).resolve()
    assert file_logging._APP_ROOT == here.parents[2]
    assert (file_logging._APP_ROOT / "app" / "core" / here.name).is_file()
