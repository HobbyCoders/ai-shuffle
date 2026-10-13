"""Shell resolution for the Bash/Monitor tools (fluid.tools.shell)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from fluid.context.environment import environment_block
from fluid.tools import shell as shell_mod
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.registry import ToolCtx
from fluid.tools.shell import ShellInfo, describe, resolve_shell


@pytest.fixture(autouse=True)
def _fresh_cache():
    resolve_shell.cache_clear()
    yield
    resolve_shell.cache_clear()


@pytest.fixture()
def windows_env(monkeypatch: pytest.MonkeyPatch):
    """Pretend to be Windows with no Git install anywhere."""
    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod.os, "name", "nt")
    for var in ("FLUID_SHELL", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)


def test_override_path_wins(windows_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = tmp_path / "bash.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("FLUID_SHELL", str(fake))
    info = resolve_shell()
    assert info.name == "git-bash"
    assert info.argv_prefix == (str(fake), "-c")
    assert str(fake) in describe(info)
    assert "POSIX" in describe(info)


def test_system32_bash_is_skipped(windows_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    wsl_dir = tmp_path / "Windows" / "System32"
    wsl_dir.mkdir(parents=True)
    wsl = wsl_dir / "bash.exe"
    wsl.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: str(wsl))
    info = resolve_shell()
    assert info.name == "cmd"


def test_falls_back_to_cmd(windows_env):
    info = resolve_shell()
    assert info.name == "cmd"
    assert info.argv_prefix == (r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c")
    assert describe(info).startswith("cmd.exe")
    assert "cmd syntax" in describe(info)


def test_path_bash_is_used_when_program_files_missing(
    windows_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: str(git_bash))
    assert resolve_shell().name == "git-bash"


def test_resolve_is_cached(windows_env):
    assert resolve_shell() is resolve_shell()


def test_environment_block_names_resolved_shell(tmp_path: Path):
    info = resolve_shell()
    block = environment_block(tmp_path)
    line = next(ln for ln in block.splitlines() if ln.startswith("Shell:"))
    assert line == f"Shell: {describe(info)}"
    assert info.path in line


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash lookup is Windows-only")
def test_this_machine_resolves_git_bash_when_installed():
    info = resolve_shell()
    assert info.name in {"git-bash", "cmd"}
    assert os.path.isfile(info.path) or info.name == "cmd"


@pytest.mark.asyncio
async def test_bash_tool_runs_posix_syntax(tmp_path: Path):
    info = resolve_shell()
    if not info.posix:
        pytest.skip("no POSIX shell available on this machine")
    out = await bash_handler(
        {"command": "echo $0 && printf 'a\\nb\\n' | wc -l"}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    lines = [ln.strip() for ln in out.text.strip().splitlines()]
    assert lines[-1] == "2"


@pytest.mark.asyncio
async def test_bash_tool_mkdir_p_and_cwd(tmp_path: Path):
    if not resolve_shell().posix:
        pytest.skip("no POSIX shell available on this machine")
    out = await bash_handler(
        {"command": "mkdir -p notes && printf 'x' > notes/a.txt && cat notes/a.txt"},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text
    assert (tmp_path / "notes" / "a.txt").read_text() == "x"


def test_describe_posix_shape():
    info = ShellInfo(name="posix", path="/bin/sh", argv_prefix=("/bin/sh", "-c"))
    assert describe(info) == "/bin/sh (the Bash tool uses POSIX shell syntax)"
