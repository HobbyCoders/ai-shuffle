"""
Tests for bash-routed hook command spawning (issue #572).

DB/plugin hook commands are written in bash syntax. On Windows,
asyncio.create_subprocess_shell runs cmd.exe, which mangles bash quoting so a
blocking guard's JSON stdout never parses and the guard silently fails OPEN.
spawn_hook_shell must route through Git Bash on Windows (falling back to the
platform shell only when no bash exists), and an unparseable decision from a
DB PreToolUse hook must be loudly logged instead of a silent debug fallthrough.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.core import hook_executor
from app.core.hook_executor import _resolve_windows_bash, spawn_hook_shell
from app.core.hook_service import _map_db_pretooluse_decision


@pytest.fixture(autouse=True)
def clear_bash_cache():
    """_resolve_windows_bash is lru_cached; isolate discovery between tests."""
    _resolve_windows_bash.cache_clear()
    yield
    _resolve_windows_bash.cache_clear()


def _make_fake_git_install(tmp_path: Path) -> Path:
    """Create a fake Git-for-Windows layout: Git/cmd/git.exe + Git/bin/bash.exe."""
    git_root = tmp_path / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir(parents=True)
    (git_root / "cmd" / "git.exe").write_text("")
    (git_root / "bin" / "bash.exe").write_text("")
    return git_root


class TestResolveWindowsBash:
    """Discovery order: env override → PATH (skipping WSL) → git-derived → known dirs."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in (
            "AI_SHUFFLE_HOOK_BASH",
            "CLAUDE_CODE_GIT_BASH_PATH",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "LOCALAPPDATA",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_env_override_wins(self, monkeypatch, tmp_path):
        fake_bash = tmp_path / "mybash.exe"
        fake_bash.write_text("")
        monkeypatch.setenv("AI_SHUFFLE_HOOK_BASH", str(fake_bash))
        monkeypatch.setattr(hook_executor.shutil, "which", lambda _: "/somewhere/else/bash")
        assert _resolve_windows_bash() == str(fake_bash)

    def test_claude_code_git_bash_path_honored(self, monkeypatch, tmp_path):
        fake_bash = tmp_path / "bash.exe"
        fake_bash.write_text("")
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(fake_bash))
        monkeypatch.setattr(hook_executor.shutil, "which", lambda _: None)
        assert _resolve_windows_bash() == str(fake_bash)

    def test_missing_override_file_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AI_SHUFFLE_HOOK_BASH", str(tmp_path / "nope.exe"))
        monkeypatch.setattr(hook_executor.shutil, "which", lambda _: None)
        assert _resolve_windows_bash() is None

    def test_path_bash_used_when_not_wsl(self, monkeypatch, tmp_path):
        fake_bash = tmp_path / "Git" / "usr" / "bin" / "bash.exe"
        fake_bash.parent.mkdir(parents=True)
        fake_bash.write_text("")
        monkeypatch.setattr(
            hook_executor.shutil,
            "which",
            lambda name: str(fake_bash) if name == "bash" else None,
        )
        assert _resolve_windows_bash() == str(fake_bash)

    @pytest.mark.parametrize(
        "wsl_path",
        [
            r"C:\Windows\System32\bash.exe",
            r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe",
        ],
    )
    def test_wsl_launchers_on_path_are_skipped(self, monkeypatch, wsl_path):
        monkeypatch.setattr(
            hook_executor.shutil,
            "which",
            lambda name: wsl_path if name == "bash" else None,
        )
        assert _resolve_windows_bash() is None

    def test_derived_from_git_exe(self, monkeypatch, tmp_path):
        git_root = _make_fake_git_install(tmp_path)
        monkeypatch.setattr(
            hook_executor.shutil,
            "which",
            lambda name: str(git_root / "cmd" / "git.exe") if name == "git" else None,
        )
        assert _resolve_windows_bash() == str(git_root / "bin" / "bash.exe")

    def test_program_files_fallback(self, monkeypatch, tmp_path):
        git_root = _make_fake_git_install(tmp_path)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        monkeypatch.setattr(hook_executor.shutil, "which", lambda _: None)
        assert _resolve_windows_bash() == str(git_root / "bin" / "bash.exe")

    def test_none_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(hook_executor.shutil, "which", lambda _: None)
        assert _resolve_windows_bash() is None


class TestSpawnHookShell:
    """Windows routes through bash -c; POSIX and bash-less Windows use the shell."""

    @pytest.mark.asyncio
    async def test_windows_with_bash_uses_exec(self, monkeypatch):
        monkeypatch.setattr(hook_executor, "_IS_WINDOWS", True)
        monkeypatch.setattr(
            hook_executor, "_resolve_windows_bash", lambda: r"C:\Git\bin\bash.exe"
        )
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec, \
             patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_shell:
            await spawn_hook_shell(
                "echo '{}'", stdin=subprocess.PIPE, cwd="/work", env={"A": "1"}
            )
        mock_shell.assert_not_awaited()
        mock_exec.assert_awaited_once()
        args, kwargs = mock_exec.await_args
        assert args == (r"C:\Git\bin\bash.exe", "-c", "echo '{}'")
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["cwd"] == "/work"
        assert kwargs["env"] == {"A": "1"}

    @pytest.mark.asyncio
    async def test_windows_without_bash_falls_back_to_shell_loudly(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(hook_executor, "_IS_WINDOWS", True)
        monkeypatch.setattr(hook_executor, "_resolve_windows_bash", lambda: None)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec, \
             patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_shell, \
             caplog.at_level("WARNING", logger="app.core.hook_executor"):
            await spawn_hook_shell("echo hi")
        mock_exec.assert_not_awaited()
        mock_shell.assert_awaited_once()
        assert "No bash executable found" in caplog.text

    @pytest.mark.asyncio
    async def test_posix_uses_shell(self, monkeypatch):
        monkeypatch.setattr(hook_executor, "_IS_WINDOWS", False)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec, \
             patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_shell:
            await spawn_hook_shell("echo hi", cwd="/work")
        mock_exec.assert_not_awaited()
        mock_shell.assert_awaited_once()
        args, kwargs = mock_shell.await_args
        assert args == ("echo hi",)
        assert kwargs["cwd"] == "/work"


class TestUnparseableDecisionIsLoud:
    """A blocking hook whose stdout doesn't parse fails open — that must be a WARNING."""

    def test_unparseable_stdout_logs_warning_with_hook_id(self, caplog):
        with caplog.at_level("WARNING", logger="app.core.hook_service"):
            result = _map_db_pretooluse_decision(
                0, '{"decision": "block"', "", hook_id="hook-abc"
            )
        assert result is None
        assert "hook-abc" in caplog.text
        assert "FAILS OPEN" in caplog.text

    def test_valid_block_decision_does_not_warn(self, caplog):
        with caplog.at_level("WARNING", logger="app.core.hook_service"):
            result = _map_db_pretooluse_decision(
                0, '{"decision": "block", "reason": "nope"}', "", hook_id="hook-abc"
            )
        assert result == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "nope",
        }
        assert caplog.text == ""

    def test_empty_stdout_stays_silent(self, caplog):
        with caplog.at_level("WARNING", logger="app.core.hook_service"):
            result = _map_db_pretooluse_decision(0, "", "", hook_id="hook-abc")
        assert result is None
        assert caplog.text == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only repro of issue #572")
class TestWindowsEndToEnd:
    """The literal #572 repro: bash single-quoted JSON must round-trip on Windows."""

    @pytest.mark.asyncio
    async def test_bash_quoted_block_command_parses(self):
        if _resolve_windows_bash() is None:
            pytest.skip("no Git Bash installed on this machine")
        process = await spawn_hook_shell(
            "echo '{\"decision\": \"block\", \"reason\": \"e2e-572\"}'",
            stdin=subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(b"{}"), timeout=15)
        parsed = json.loads(stdout.decode().strip())
        assert parsed == {"decision": "block", "reason": "e2e-572"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group kill")
class TestTimeoutKillsTheWholeHook:
    """A timed-out hook's children must die with it, or they hold the pipes open.

    ``/bin/sh -c`` forks ``sleep`` rather than exec'ing it; killing only the
    shell used to leave the child holding stdout/stderr, so the call blocked
    for the child's whole lifetime instead of returning at the timeout.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("procfs_exit_race", [False, True])
    async def test_timeout_returns_promptly_and_kills_the_child(self, tmp_path, monkeypatch, procfs_exit_race):
        import time

        pid_file = tmp_path / "child.pid"
        started = time.monotonic()
        result = await hook_executor.execute_bash_hook(
            {"command": f"sleep 30 & echo $! > {pid_file}; wait", "timeout": 200},
            {"event": "test", "data": {}},
        )
        elapsed = time.monotonic() - started

        assert result["success"] is False
        assert "timed out" in result["error"]
        assert elapsed < 5, f"hook call blocked {elapsed:.1f}s past its 0.2s timeout"

        # Linux: the orphaned child must be dead (gone or a zombie awaiting reap).
        stat = Path(f"/proc/{int(pid_file.read_text().strip())}/stat")
        vanished_reads = []
        if procfs_exit_race:
            read_text = Path.read_text

            def read_with_exit_race(path, *args, **kwargs):
                if path != stat:
                    return read_text(path, *args, **kwargs)
                try:
                    text = read_text(path, *args, **kwargs)
                except FileNotFoundError:
                    text = None
                # Inject ESRCH only after the real child is gone/dead. A live
                # child still reaches the unchanged deadline assertion below.
                if text is None or text.rsplit(")", 1)[-1].split()[0] in ("Z", "X"):
                    vanished_reads.append(True)
                    raise ProcessLookupError(3, "No such process")
                return text

            monkeypatch.setattr(Path, "read_text", read_with_exit_race)

        def _alive() -> bool:
            try:
                return stat.read_text().rsplit(")", 1)[-1].split()[0] not in ("Z", "X")
            except (FileNotFoundError, ProcessLookupError):
                # procfs can lose the process after open but before read,
                # reporting ESRCH instead of ENOENT. Both mean it exited.
                return False

        if Path("/proc/self/stat").exists():
            deadline = time.monotonic() + 2
            while _alive():
                assert time.monotonic() < deadline, "backgrounded child survived the timeout"
                await asyncio.sleep(0.02)
            if procfs_exit_race:
                assert vanished_reads, "procfs exit-race regression was not exercised"
