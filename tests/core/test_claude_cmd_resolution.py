"""Which ``claude`` the delegated leg runs (``_resolve_claude_cmd``).

Every case builds its own PATH out of tmp dirs and never runs what it finds.
The Windows branch is forced with ``windows=True`` so it is covered on every OS.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

import app.core.provider.harness_adapter as ha
from fluid.engine.delegated import ClaudeCliMissingError


def _touch(path, *, executable: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a real CLI\n", encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def path_of(monkeypatch):
    def set_path(*dirs) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))
    monkeypatch.setattr(ha, "_WARNED_CLAUDE_SHIMS", set())
    return set_path


def _shim_warnings(caplog) -> list:
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "batch shim" in r.getMessage()]


def test_the_exe_wins_over_a_cmd_shim_in_the_same_dir(tmp_path, path_of, caplog):
    bin_dir = tmp_path / "npm"
    _touch(bin_dir / "claude.cmd")
    exe = _touch(bin_dir / "claude.exe")
    path_of(bin_dir)

    with caplog.at_level(logging.WARNING, logger=ha.logger.name):
        assert ha._resolve_claude_cmd(windows=True) == [str(exe)]
    assert _shim_warnings(caplog) == []


def test_an_exe_later_on_path_wins_over_an_earlier_npm_shim(tmp_path, path_of, caplog):
    # The npm install puts %APPDATA%\npm (claude.cmd) ahead of the native
    # installer's ~/.local/bin (claude.exe). The shim would cut the multi-line
    # appended prompt at its first newline, so the exe must still win.
    _touch(tmp_path / "npm" / "claude.cmd")
    exe = _touch(tmp_path / "local-bin" / "claude.exe")
    path_of(tmp_path / "npm", tmp_path / "local-bin")

    with caplog.at_level(logging.WARNING, logger=ha.logger.name):
        assert ha._resolve_claude_cmd(windows=True) == [str(exe)]
    assert _shim_warnings(caplog) == []


def test_the_first_shim_on_path_is_used_when_there_is_no_exe(tmp_path, path_of):
    first = _touch(tmp_path / "npm" / "claude.cmd")
    _touch(tmp_path / "other" / "claude.bat")
    path_of(tmp_path / "npm", tmp_path / "other")

    assert ha._resolve_claude_cmd(windows=True) == [str(first)]


def test_a_lone_cmd_shim_is_used_with_one_warning(tmp_path, path_of, caplog):
    bin_dir = tmp_path / "npm"
    shim = _touch(bin_dir / "claude.cmd")
    path_of(bin_dir)

    with caplog.at_level(logging.WARNING, logger=ha.logger.name):
        assert ha._resolve_claude_cmd(windows=True) == [str(shim)]
        assert ha._resolve_claude_cmd(windows=True) == [str(shim)]
    [warning] = _shim_warnings(caplog)
    assert str(shim) in warning.getMessage()
    assert "native" in warning.getMessage()


def test_a_bat_shim_counts_as_a_shim(tmp_path, path_of, caplog):
    shim = _touch(tmp_path / "bin" / "claude.bat")
    path_of(tmp_path / "bin")

    with caplog.at_level(logging.WARNING, logger=ha.logger.name):
        assert ha._resolve_claude_cmd(windows=True) == [str(shim)]
    assert len(_shim_warnings(caplog)) == 1


def test_path_order_still_decides_between_dirs(tmp_path, path_of):
    first = _touch(tmp_path / "first" / "claude.exe")
    _touch(tmp_path / "second" / "claude.exe")
    path_of(tmp_path / "empty", tmp_path / "first", tmp_path / "second")
    (tmp_path / "empty").mkdir()

    assert ha._resolve_claude_cmd(windows=True) == [str(first)]


def test_windows_ignores_an_extension_less_file(tmp_path, path_of):
    _touch(tmp_path / "shim" / "claude", executable=True)
    exe = _touch(tmp_path / "native" / "claude.exe")
    path_of(tmp_path / "shim", tmp_path / "native")

    assert ha._resolve_claude_cmd(windows=True) == [str(exe)]


def test_posix_takes_the_extension_less_binary(tmp_path, path_of):
    _touch(tmp_path / "bin" / "claude.cmd")
    binary = _touch(tmp_path / "bin" / "claude", executable=True)
    path_of(tmp_path / "bin")

    assert ha._resolve_claude_cmd(windows=False) == [str(binary)]


@pytest.mark.skipif(os.name == "nt", reason="Windows has no execute bit")
def test_posix_skips_a_file_that_is_not_executable(tmp_path, path_of):
    _touch(tmp_path / "plain" / "claude")
    binary = _touch(tmp_path / "bin" / "claude", executable=True)
    path_of(tmp_path / "plain", tmp_path / "bin")

    assert ha._resolve_claude_cmd(windows=False) == [str(binary)]


@pytest.mark.parametrize("windows, name", [(True, "claude.exe"), (False, "claude")])
def test_a_vendored_copy_is_still_skipped(tmp_path, path_of, windows, name):
    vendored = tmp_path / "venv" / "site-packages" / "claude_agent_sdk" / "_bundled"
    _touch(vendored / name, executable=True)
    real = _touch(tmp_path / "bin" / name, executable=True)
    path_of(vendored, tmp_path / "bin")

    assert ha._resolve_claude_cmd(windows=windows) == [str(real)]


@pytest.mark.parametrize("windows", [True, False])
def test_nothing_found_raises_the_setup_error(tmp_path, path_of, windows):
    (tmp_path / "empty").mkdir()
    path_of(tmp_path / "empty", tmp_path / "missing")

    with pytest.raises(ClaudeCliMissingError):
        ha._resolve_claude_cmd(windows=windows)
